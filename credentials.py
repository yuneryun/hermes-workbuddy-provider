"""WorkBuddy desktop credential reader.

Reads the access token the WorkBuddy / WorkBuddy AI desktop app already holds, so this
provider needs **no separate sign-in**: if the app is logged in, Hermes can use its models.

How the credential is protected (WorkBuddy 5.6+ at-rest scheme)
--------------------------------------------------------------
The app writes a JSON credential file under::

    %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\<variant>.info

Sensitive fields inside it are not plain strings but ``{"$wbEncrypted":1,"envelope":"<b64>"}``
wrappers. The envelope is a JSON document ``{suite,keyId,nonce,authTag,ciphertext}`` for
AES-256-GCM.

The GCM key is derived from a device secret the app exposes through its own Electron binary::

    <app>.exe -e 'process.stdout.write(String(process._linkedBinding(
        "electron_browser_workbuddy_storage").loggerGet()))'
    env: ELECTRON_RUN_AS_NODE=1
    -> {"version":1,"atRestSecretKey":"<base64 32 bytes>"}

    protectorKey = sha256(atRestSecretKey, "utf8")

Each field is sealed with an AAD that binds the framing, key id and suite (see
``_build_aad``). This mirrors WorkBuddy's own ``buildAuthenticatedContextAad``.

Nothing here is a workaround for a broken install: it is exactly how the official
``dsh-workbuddy-connect`` plugin reuses the desktop session.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Variant:
    """One WorkBuddy edition: where its credential lives and where its API is."""

    key: str
    label: str
    auth_filename: str
    app_names: tuple[str, ...]
    install_roots: tuple[str, ...]
    api_base: str
    origin: str
    expected_domain: str


CN = Variant(
    key="cn",
    label="WorkBuddy",
    auth_filename="workbuddy-desktop.info",
    app_names=("WorkBuddy.exe",),
    install_roots=("WorkBuddy",),
    api_base="https://copilot.tencent.com/v2",
    origin="https://www.codebuddy.cn",
    expected_domain="cn",
)

AI = Variant(
    key="ai",
    label="WorkBuddy AI",
    auth_filename="workbuddy-desktop-ai.info",
    app_names=("WorkBuddyAI.exe",),
    install_roots=("WorkBuddyAI",),
    api_base="https://www.workbuddy.ai/v2",
    origin="https://www.workbuddy.ai",
    expected_domain="global",
)

VARIANTS = (AI, CN)

# Both editions share this auth directory and differ only by filename.
_AUTH_RELATIVE = ("CodeBuddyExtension", "Data", "Public", "auth")

# The app's own at-rest key helper, transcribed from the shipping client.
_KEY_HELPER = (
    "process.stdout.write(String(process._linkedBinding("
    '"electron_browser_workbuddy_storage").loggerGet()))'
)


class WorkBuddyAuthError(RuntimeError):
    """Raised when the desktop credential cannot be located or opened."""


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #


def auth_file_candidates(variant: Variant) -> list[Path]:
    """Credential paths to probe, in order (current builds use LOCALAPPDATA)."""
    out: list[Path] = []
    for root, name in (
        (os.environ.get("LOCALAPPDATA"), variant.auth_filename),
        (os.environ.get("APPDATA"), variant.auth_filename),
    ):
        if root:
            out.append(Path(root).joinpath(*_AUTH_RELATIVE, name))
    return out


def auth_file(variant: Variant) -> Optional[Path]:
    for path in auth_file_candidates(variant):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def app_binary(variant: Variant) -> Optional[Path]:
    """Locate the installed WorkBuddy executable (needed for the at-rest key)."""
    override = os.environ.get("WORKBUDDY_APP_PATH")
    if override and Path(override).is_file():
        return Path(override)

    roots: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base))
    roots.extend(Path(p) for p in (r"C:\Program Files", r"C:\Program Files (x86)"))

    seen: set[str] = set()
    for root in roots:
        for install in variant.install_roots:
            for app in variant.app_names:
                candidate = root / install / app
                key = str(candidate).lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    if candidate.is_file():
                        return candidate
                except OSError:
                    continue
    return None


# --------------------------------------------------------------------------- #
# At-rest key + envelope decryption
# --------------------------------------------------------------------------- #


def _at_rest_secret(binary: Path, timeout: float = 25.0) -> str:
    """Ask the app binary for its at-rest secret."""
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    # The helper is a plain Node program; inherited proxy vars make some builds
    # fail before main() runs, so they are dropped for this one call.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "NODE_OPTIONS"):
        env.pop(var, None)
    try:
        proc = subprocess.run(
            [str(binary), "-e", _KEY_HELPER],
            capture_output=True, env=env, timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkBuddyAuthError(f"could not run the WorkBuddy key helper: {exc}") from exc

    raw = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise WorkBuddyAuthError(f"WorkBuddy key helper produced no payload ({err[:200]})")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkBuddyAuthError(f"unusable at-rest payload: {raw[:200]}") from exc

    secret = payload.get("atRestSecretKey")
    if not isinstance(secret, str) or not secret:
        raise WorkBuddyAuthError("at-rest payload carried no atRestSecretKey")
    return secret


def _protector_key(secret_b64: str) -> bytes:
    """``protectorKey = sha256(atRestSecretKey, utf8)``."""
    try:
        raw = base64.b64decode(secret_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same error
        raise WorkBuddyAuthError("atRestSecretKey was not valid base64") from exc
    if len(raw) != 32:
        raise WorkBuddyAuthError(f"atRestSecretKey must be 32 bytes, got {len(raw)}")
    return hashlib.sha256(secret_b64.encode("utf-8")).digest()


def _build_aad(key_id: str, suite: int) -> bytes:
    """The authenticated context for one field envelope (WBEV1 framing)."""
    def lp(value: str) -> bytes:
        data = value.encode("utf-8")
        return struct.pack(">I", len(data)) + data

    return b"".join((
        b"WB-AAD\x00",
        bytes([1]),
        lp("WBEV1"),
        lp("sym-v1"),
        struct.pack(">I", suite),
        lp(key_id),
        bytes([2]),
        bytes([0]),
        bytes([0]),
    ))


def _open_field(protector_key: bytes, wrapped: dict[str, Any]) -> Optional[str]:
    """Decrypt one ``{$wbEncrypted:1, envelope}`` field; ``None`` when it will not open."""
    if not isinstance(wrapped, dict) or wrapped.get("$wbEncrypted") != 1:
        return None
    envelope = wrapped.get("envelope")
    if not isinstance(envelope, str):
        return None
    try:
        meta = json.loads(base64.b64decode(envelope))
        if meta.get("suite") != 1:
            return None
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aes = AESGCM(protector_key)
        # AESGCM expects the tag appended to the ciphertext, with the AAD passed
        # separately -- the envelope keeps all three as distinct base64 fields.
        sealed = base64.b64decode(meta["ciphertext"]) + base64.b64decode(meta["authTag"])
        plain = aes.decrypt(
            base64.b64decode(meta["nonce"]),
            sealed,
            _build_aad(str(meta["keyId"]), int(meta["suite"])),
        )
        return plain.decode("utf-8")
    except Exception:  # noqa: BLE001 - wrong key / format is a normal probe outcome
        return None


# --------------------------------------------------------------------------- #
# Credential assembly
# --------------------------------------------------------------------------- #


@dataclass
class Credential:
    """A usable WorkBuddy credential."""

    variant: Variant
    access_token: str
    refresh_token: Optional[str]
    uid: str
    domain: str
    expires_at_ms: Optional[int]
    base_url: str
    origin: str
    auth_path: Path

    @property
    def bearer(self) -> str:
        return f"Bearer {self.access_token}"


def _decode_field(key: bytes, node: Any) -> Optional[str]:
    """Open one possibly-wrapped scalar (plain strings pass through)."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return _open_field(key, node)
    return None


def read_credential(variant: Variant) -> Optional[Credential]:
    """Read and decrypt one edition's credential, or ``None`` when unusable."""
    path = auth_file(variant)
    if path is None:
        return None

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    auth = document.get("auth") or {}
    account = document.get("account") or {}
    if not isinstance(auth, dict) or not isinstance(account, dict):
        return None

    binary = app_binary(variant)
    if binary is None:
        raise WorkBuddyAuthError(
            f"{variant.label} is not installed (no {variant.app_names[0]} found); "
            "install the desktop app and sign in"
        )

    protector_key = _protector_key(_at_rest_secret(binary))

    token = _decode_field(protector_key, auth.get("accessToken"))
    if not token:
        return None
    refresh = _decode_field(protector_key, auth.get("refreshToken"))

    domain = auth.get("domain") if isinstance(auth.get("domain"), str) else ""
    expires_at = auth.get("expiresAt")
    return Credential(
        variant=variant,
        access_token=token,
        refresh_token=refresh,
        uid=str(account.get("uid") or ""),
        domain=domain or variant.origin.split("//", 1)[-1],
        expires_at_ms=int(expires_at) if isinstance(expires_at, (int, float)) else None,
        base_url=variant.api_base,
        origin=variant.origin,
        auth_path=path,
    )


def active_variants() -> list[Variant]:
    """Editions that have a readable credential file, newest first."""
    found: list[tuple[float, Variant]] = []
    for variant in VARIANTS:
        path = auth_file(variant)
        if path is None:
            continue
        try:
            found.append((path.stat().st_mtime, variant))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [variant for _, variant in found]


def resolve_credential(prefer: Optional[str] = None) -> Credential:
    """Return a usable credential, preferring ``prefer`` ('ai'/'cn'), else newest."""
    order = list(VARIANTS)
    if prefer:
        order.sort(key=lambda v: 0 if v.key == prefer else 1)

    errors: list[str] = []
    for variant in order:
        if auth_file(variant) is None:
            continue
        try:
            cred = read_credential(variant)
        except WorkBuddyAuthError as exc:
            errors.append(f"{variant.label}: {exc}")
            continue
        if cred is not None:
            return cred

    if errors:
        raise WorkBuddyAuthError("; ".join(errors))
    raise WorkBuddyAuthError(
        "no signed-in WorkBuddy desktop app found. Open WorkBuddy (or WorkBuddy AI), "
        "sign in, then retry."
    )
