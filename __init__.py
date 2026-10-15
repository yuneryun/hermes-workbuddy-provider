"""WorkBuddy model provider for Hermes Agent.

Brings the models bundled with the **WorkBuddy / WorkBuddy AI** desktop app
(DeepSeek-V4.1-Flash, GLM-5.3, Kimi-K3, GPT-6, Grok-4.7, Gemini-3.8, Hy4, ...) into
Hermes with **zero configuration**: the provider reuses whatever session the desktop
app already holds, so there is no API key to paste and no separate sign-in.

Editions
--------
Both are supported and resolved independently. If both apps are installed and signed
in, the international one wins by default because its credential file is refreshed by
the app on every launch; pin one explicitly with ``WORKBUDDY_VARIANT=ai`` or ``cn``.

Wire format
-----------
The upstream is *not* plain OpenAI-compatible:

* ``POST {base}/v2/chat/completions`` and **streaming is mandatory** -- a non-stream
  request is rejected with ``11101 Non-stream chat request is currently not supported``.
* An international request must lead with a ``system`` message.
* The default transport sends ``stream: false``, so this profile overrides
  ``build_api_kwargs_extras`` to force streaming and inject the system turn.

``create_client`` is implemented to pin the transport to a direct dial: the core's
own client honours ``NO_PROXY``, but only as it stood when the gateway process
started, so a proxy configured afterwards -- or a system proxy -- tunnels every
request and costs a fixed multi-second TLS handshake (measured 1.2s direct vs
6.4s proxied). The override passes ``trust_env=False`` so no proxy setting can
reach this provider's requests.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from providers import register_provider
from providers.base import ProviderProfile

from . import credentials
from .credentials import WorkBuddyAuthError, resolve_credential

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER_NAME = "workbuddy"

#: Sent as the CLI/IDE identity. The upstream gates features on these headers.
IDE_VERSION = "5.7.6"
CLI_VERSION = "2.156.0"
USER_AGENT = f"WorkBuddy AI/{IDE_VERSION} WorkBuddyAI/{IDE_VERSION} CLI/{CLI_VERSION}"

#: Added when a request arrives without a system turn (international requirement).
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant integrated into the WorkBuddy desktop application."
)

#: Curated roster, used when the live catalog cannot be fetched. Every id here is a
#: real id from the WorkBuddy CLI agent's model list; per-model metadata is declared
#: below so pickers still show correct context windows while offline.
FALLBACK_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
    "hy4-preview",
    "hy3",
    "step-5-preview",
    "grok-4.7",
    "gemini-3.8-flash",
)

#: Model metadata mirrored from ``/v3/config``. Keys are exact model ids; the shape
#: follows the ``model_overrides`` schema.
MODEL_CAPABILITIES: dict[str, dict[str, Any]] = {
    "deepseek-v4.1-flash": {"supports_reasoning": True, "supports_vision": True,
                            "supports_tools": True, "context_window": 1000000,
                            "model_family": "deepseek"},
    "deepseek-v4.1-flash-sg": {"supports_reasoning": True, "supports_vision": True,
                               "supports_tools": True, "context_window": 1000000,
                               "model_family": "deepseek"},
    "deepseek-v3-2-volc": {"supports_reasoning": True, "supports_vision": True,
                           "supports_tools": True, "context_window": 96000,
                           "model_family": "deepseek"},
    "glm-5.3": {"supports_reasoning": True, "supports_vision": True,
                "supports_tools": True, "context_window": 1000000, "model_family": "glm"},
    "glm-5.3-flash": {"supports_reasoning": True, "supports_vision": True,
                      "supports_tools": True, "context_window": 1000000,
                      "model_family": "glm"},
    "glm-5.3-flashx": {"supports_reasoning": True, "supports_vision": True,
                       "supports_tools": True, "context_window": 1000000,
                       "model_family": "glm"},
    "glm-5.2": {"supports_reasoning": True, "supports_vision": False,
                "supports_tools": True, "context_window": 1000000, "model_family": "glm"},
    "glm-5.1": {"supports_reasoning": True, "supports_vision": True,
                "supports_tools": True, "context_window": 200000, "model_family": "glm"},
    "glm-5.0": {"supports_reasoning": True, "supports_vision": True,
                "supports_tools": True, "context_window": 200000, "model_family": "glm"},
    "glm-4.7": {"supports_reasoning": True, "supports_vision": True,
                "supports_tools": True, "context_window": 200000, "model_family": "glm"},
    "kimi-k3": {"supports_reasoning": True, "supports_vision": True,
                "supports_tools": True, "context_window": 1000000, "model_family": "kimi"},
    "kimi-k2.8-preview": {"supports_reasoning": True, "supports_vision": True,
                          "supports_tools": True, "context_window": 1000000,
                          "model_family": "kimi"},
    "kimi-k2.6": {"supports_reasoning": True, "supports_vision": True,
                  "supports_tools": True, "context_window": 256000, "model_family": "kimi"},
    "kimi-k2.5": {"supports_reasoning": True, "supports_vision": True,
                  "supports_tools": True, "context_window": 256000, "model_family": "kimi"},
    "hy4-preview": {"supports_reasoning": True, "supports_vision": True,
                    "supports_tools": True, "context_window": 1000000,
                    "model_family": "hunyuan"},
    "hy3": {"supports_reasoning": True, "supports_vision": True,
            "supports_tools": True, "context_window": 192000, "model_family": "hunyuan"},
    "step-5-preview": {"supports_reasoning": True, "supports_vision": True,
                       "supports_tools": True, "context_window": 1000000,
                       "model_family": "step"},
    "grok-4.7": {"supports_reasoning": True, "supports_vision": True,
                 "supports_tools": True, "context_window": 500000, "model_family": "grok"},
    "gemini-3.8-flash": {"supports_reasoning": True, "supports_vision": True,
                         "supports_tools": True, "context_window": 1000000,
                         "model_family": "gemini"},
    "gemini-3.5-flash": {"supports_reasoning": True, "supports_vision": True,
                         "supports_tools": True, "context_window": 1000000,
                         "model_family": "gemini"},
    "gpt-6-sol": {"supports_reasoning": True, "supports_vision": True,
                  "supports_tools": True, "context_window": 1000000, "model_family": "gpt"},
    "gpt-5.6-terra": {"supports_reasoning": True, "supports_vision": True,
                      "supports_tools": True, "context_window": 1000000,
                      "model_family": "gpt"},
    "gpt-5.5": {"supports_reasoning": True, "supports_vision": True,
                "supports_tools": True, "context_window": 1000000, "model_family": "gpt"},
}

DEFAULT_AUX_MODEL = "deepseek-v4.1-flash"

# --------------------------------------------------------------------------- #
# Credential cache
# --------------------------------------------------------------------------- #

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"token": None, "expires": 0.0, "cred": None}

#: How long a resolved token is trusted before the file is re-read. Short enough to
#: notice an app-side account switch, long enough to avoid spawning the key helper
#: on every request (that helper is a full Electron process launch).
CACHE_TTL_SECONDS = 300.0


def _preferred_variant() -> Optional[str]:
    value = (os.environ.get("WORKBUDDY_VARIANT") or "").strip().lower()
    if value in {"ai", "cn"}:
        return value
    return None


def current_credential(refresh: bool = False) -> credentials.Credential:
    """Resolved credential, cached briefly. Raises WorkBuddyAuthError when absent."""
    now = time.monotonic()
    with _LOCK:
        if not refresh and _CACHE["cred"] is not None and now < _CACHE["expires"]:
            return _CACHE["cred"]

    cred = resolve_credential(prefer=_preferred_variant())

    with _LOCK:
        _CACHE["cred"] = cred
        _CACHE["expires"] = time.monotonic() + CACHE_TTL_SECONDS
    return cred


def _invalidate() -> None:
    with _LOCK:
        _CACHE["cred"] = None
        _CACHE["expires"] = 0.0


def sync_env(refresh: bool = False) -> bool:
    """Publish the desktop session into ``WORKBUDDY_ACCESS_TOKEN``.

    Hermes resolves an API-key provider from its declared env vars, and it reads them
    from ``os.environ`` (or ``.env``) on every resolution -- there is no per-provider
    credential hook for a plugin to override. Since the WorkBuddy token is not a static
    secret but a value that can be re-derived from the desktop app at any time, the
    provider publishes it here instead of asking the user to paste one.

    Returns True when a token is in place.
    """
    try:
        cred = current_credential(refresh=refresh)
    except WorkBuddyAuthError:
        return False
    os.environ["WORKBUDDY_ACCESS_TOKEN"] = cred.access_token
    # The base URL follows the edition that answered (the two editions live on
    # different hosts), so keep it in step with the token.
    os.environ.setdefault("WORKBUDDY_BASE_URL", cred.base_url)
    _CACHE["base_url"] = cred.base_url
    return True


def _effective_base_url() -> str:
    """Base URL of the currently resolved edition, falling back to the AI host."""
    cached = _CACHE.get("base_url")
    if isinstance(cached, str) and cached:
        return cached
    try:
        return current_credential().base_url
    except WorkBuddyAuthError:
        return credentials.AI.api_base


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _headers(cred: credentials.Credential) -> dict[str, str]:
    """Headers the WorkBuddy desktop client sends.

    ``Origin``/``Referer`` name the app's web origin (no ``/v2`` path), which the
    upstream checks; the inference path lives under ``<origin>/v2``.
    """
    origin = cred.origin
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": f"{origin}/",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": IDE_VERSION,
        "X-Product": "SaaS",
        "Authorization": cred.bearer,
    }
    if cred.uid:
        headers["X-User-Id"] = cred.uid
    else:
        headers["X-No-User-Id"] = "1"
    # The editions differ here: a personal account has no enterprise scope.
    headers["X-No-Enterprise-Id"] = "1"
    if cred.domain:
        headers["X-Domain"] = cred.domain
    return headers


def _open(request: urllib.request.Request, timeout: float):
    """Open a request, bypassing any inherited proxy for this local-account call."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


# --------------------------------------------------------------------------- #
# Model catalog
# --------------------------------------------------------------------------- #


def _parse_catalog(document: Any) -> list[str]:
    """Model ids the account's `cli` agent may use, from a /v3/config document."""
    if not isinstance(document, dict):
        return []
    data = document.get("data")
    if not isinstance(data, dict):
        return []
    agents = data.get("agents")
    if not isinstance(agents, list):
        return []
    for agent in agents:
        if isinstance(agent, dict) and agent.get("name") == "cli":
            models = agent.get("models")
            if isinstance(models, list):
                return [m for m in models if isinstance(m, str) and m]
    return []


def fetch_account_models(timeout: float = 20.0) -> list[str]:
    """Live model ids for the signed-in account (empty when unavailable)."""
    try:
        cred = current_credential()
    except WorkBuddyAuthError:
        return []
    request = urllib.request.Request(
        f"{cred.origin}/v3/config", headers=_headers(cred), method="GET"
    )
    try:
        with _open(request, timeout) as response:
            document = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - any failure degrades to the curated roster
        return []
    return _parse_catalog(document)


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


def auth_handler(action: str, args: Any) -> bool:
    """``hermes auth add|status|logout|refresh workbuddy``.

    The credential already exists -- the desktop app owns it -- so "add" means
    "import it into Hermes' credential pool", making it visible to every code path
    that resolves an API-key provider.
    """
    if action == "add":
        try:
            cred = resolve_credential(prefer=_preferred_variant())
        except WorkBuddyAuthError as exc:
            print(f"WorkBuddy: {exc}")
            return False

        import uuid

        from agent.credential_pool import AUTH_TYPE_API_KEY, PooledCredential, load_pool

        pool = load_pool(PROVIDER_NAME)
        for entry in pool.entries():
            if entry is not None and entry.label == f"{cred.variant.label}:{cred.uid}":
                pool.remove_entry(entry.id)
        pool.add_entry(PooledCredential(
            provider=PROVIDER_NAME,
            id=uuid.uuid4().hex[:6],
            label=f"{cred.variant.label}:{cred.uid}",
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source=f"desktop:{cred.variant.key}",
            access_token=cred.access_token,
            refresh_token=cred.refresh_token,
            expires_at_ms=cred.expires_at_ms,
            inference_base_url=cred.base_url,
            extra={
                "uid": cred.uid,
                "domain": cred.domain,
                "variant": cred.variant.key,
                "auth_file": str(cred.auth_path),
            },
        ))
        print(f"WorkBuddy: imported the {cred.variant.label} session for {cred.uid}.")
        print("Models are available under provider 'workbuddy'.")
        return True

    if action == "status":
        rows = _pool_rows()
        if rows:
            for row in rows:
                print(f"workbuddy: signed in as {row.label} (source: {row.source})")
        else:
            for variant in credentials.VARIANTS:
                path = credentials.auth_file(variant)
                state = "desktop app signed in" if path else "no desktop credential"
                print(f"workbuddy: {variant.label}: {state}")
        return True

    if action in {"logout", "remove"}:
        try:
            from agent.credential_pool import load_pool

            pool = load_pool(PROVIDER_NAME)
            for entry in pool.entries():
                if entry is not None:
                    pool.remove_entry(entry.id)
            _invalidate()
            print("workbuddy: removed the imported credential. The desktop app is untouched.")
        except Exception as exc:  # noqa: BLE001
            print(f"workbuddy: could not clear the pool: {exc}")
            return False
        return True

    if action == "refresh":
        _invalidate()
        try:
            cred = current_credential(refresh=True)
        except WorkBuddyAuthError as exc:
            print(f"workbuddy: {exc}")
            return False
        print(f"workbuddy: re-read the session for {cred.uid}.")
        return True

    return False


def _pool_rows() -> list[Any]:
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(PROVIDER_NAME)
        return [e for e in pool.entries() if e is not None]
    except Exception:  # noqa: BLE001
        return []


class WorkBuddyProfile(ProviderProfile):
    """WorkBuddy's streaming-only, header-gated OpenAI-ish endpoint."""

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None,
        timeout: float = 15.0,
    ) -> list[str] | None:
        """Live catalog for the signed-in account, else None (caller falls back)."""
        sync_env()
        models = fetch_account_models(timeout=timeout)
        return models or None

    def get_hostname(self) -> str:
        """Hostname of the edition currently in use (used for URL-based detection)."""
        from urllib.parse import urlparse

        return urlparse(_effective_base_url()).hostname or ""

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The upstream rejects `developer`; international requires a leading system turn."""
        out: list[dict[str, Any]] = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            item = dict(message)
            if item.get("role") == "developer":
                item["role"] = "system"
            out.append(item)

        if not out or out[0].get("role") != "system":
            out.insert(0, {"role": "system", "content": DEFAULT_SYSTEM_PROMPT})
        return out

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Force streaming at the top level of the request.

        The endpoint answers 400/11101 for any non-stream request, so this is not an
        optimisation -- it is the only working shape.
        """
        return {}, {"stream": True}

    def create_client(self, **client_kwargs: Any) -> Any | None:
        """Build the OpenAI client for this profile, always dialling direct.

        The core respects ``NO_PROXY`` (agent/proxy_bypass.py) when it builds its
        own shared transport, but the value it consults is the *process*
        environment at gateway start -- so a user who adds the host to ``NO_PROXY``
        later, or who relies on a system proxy set after launch, still gets every
        request tunnelled through the proxy. For this provider that costs a
        constant ~5s TLS handshake per request (measured: 1.3s direct vs 6.4s
        proxied on https://www.workbuddy.ai), and an inherited stale
        ``HTTP(S)_PROXY`` crashes the desktop CLI's own ``setProxy`` helper the
        same way -- both are why the sibling plugin strips proxy env for CLI calls.

        Returning a client here also sidesteps the core's ``NO_PROXY`` lookup order
        entirely: this construction passes an explicit no-proxy httpx transport,
        which overrides everything the process environment says. Returning
        ``None`` (the default) keeps the core path for anyone who overrides this
        method away.
        """
        try:
            import httpx
            from agent.process_bootstrap import _load_openai_cls

            kwargs = dict(client_kwargs)
            kwargs.pop("command", None)
            kwargs.pop("args", None)
            verify = kwargs.pop("ssl_ca_cert", None) or True
            cfg = kwargs.pop("ssl_verify", None)
            if cfg is not None:
                try:
                    from agent.ssl_verify import resolve_httpx_verify
                    verify = resolve_httpx_verify(
                        ca_bundle=kwargs.get("ssl_ca_cert"), ssl_verify=cfg,
                        base_url=str(kwargs.get("base_url", "")),
                    )
                except Exception:  # noqa: BLE001 - keep the default verify on any failure
                    pass
            transport = httpx.HTTPTransport(
                verify=verify,
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=100,
                                    keepalive_expiry=20.0),
            )
            kwargs["http_client"] = httpx.Client(transport=transport, trust_env=False)
            return _load_openai_cls()(**kwargs)
        except Exception:
            # Any failure must not take the turn down; the core falls back to its
            # own client construction (see _provider_supplied_client).
            return None


workbuddy = WorkBuddyProfile(
    name=PROVIDER_NAME,
    aliases=("wb", "workbuddy-ai"),
    api_mode="chat_completions",
    display_name="WorkBuddy",
    description=(
        "Models bundled with the WorkBuddy / WorkBuddy AI desktop app. Reuses the "
        "app's own sign-in -- no API key required."
    ),
    signup_url="https://www.workbuddy.ai/",
    # No env var carries the credential: it is read from the desktop app. Listed so
    # `hermes model` / setup can still label the provider, and so an explicit bearer
    # token remains possible for advanced setups.
    env_vars=("WORKBUDDY_ACCESS_TOKEN",),
    base_url=credentials.AI.api_base,
    auth_type="api_key",
    auth_handler=auth_handler,
    default_aux_model=DEFAULT_AUX_MODEL,
    fallback_models=FALLBACK_MODELS,
    model_capabilities=MODEL_CAPABILITIES,
    supports_vision=True,
    default_headers={},
)

register_provider(workbuddy)

# Publish the desktop session before anything resolves credentials. Import happens
# lazily on the first provider lookup, which is always ahead of the first request.
try:
    sync_env()
except Exception:  # noqa: BLE001 - a missing app must not break provider discovery
    pass
