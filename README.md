# hermes-workbuddy-provider

把 **WorkBuddy / WorkBuddy AI 桌面应用**自带的模型接入 [Hermes Agent](https://hermes-agent.nousresearch.com/)。

**零配置**：直接复用桌面应用已有的登录态，不需要申请 API Key、不需要额外登录。

## 能用到哪些模型

| 模型 | 说明 |
|---|---|
| `deepseek-v4.1-flash` | DeepSeek V4.1 Flash，1M 上下文 |
| `glm-5.3` / `glm-5.3-flash` / `glm-5.2` | 智谱 GLM 系列 |
| `kimi-k3` / `kimi-k2.8-preview` / `kimi-k2.6` | 月之暗面 Kimi 系列 |
| `step-5-preview` | 阶跃星辰 |
| `hy4-preview` / `hy3` | 腾讯混元 |
| `grok-4.7` | xAI |
| `gemini-3.8-flash` / `gemini-3.5-flash` | Google |
| `gpt-5.6-terra` / `gpt-5.5` | OpenAI |

实际可用列表以你账号的实时目录为准——插件会优先向服务端拉取，拉不到时回退到上面的内置清单。

## 安装

```bash
# 1) 克隆到 Hermes 的插件目录（路径按你的系统调整）
#    Windows: %LOCALAPPDATA%\hermes\plugins\model-providers\
#    macOS/Linux: ~/.hermes/plugins/model-providers/
git clone https://github.com/yuneryun/hermes-workbuddy-provider.git \
  "$LOCALAPPDATA/hermes/plugins/model-providers/workbuddy"
```

确保目录结构是这样（`workbuddy/` 下直接是 `__init__.py`）：

```
plugins/
└── model-providers/
    └── workbuddy/
        ├── __init__.py
        ├── credentials.py
        └── plugin.yaml
```

```bash
# 2) 确认桌面应用已安装并登录（这一步是前提）
#    - 国际版：WorkBuddyAI.exe（https://www.workbuddy.ai/）
#    - 国内版：WorkBuddy.exe（codebuddy）

# 3) 导入登录态到 Hermes
hermes auth add workbuddy

# 4) 查看状态 / 列出模型
hermes auth status workbuddy
hermes model
```

然后在 Hermes 里选 `workbuddy` 供应商下的模型即可。

## 原理

桌面应用把登录凭证写在：

```
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info   （国际版）
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info      （国内版）
```

其中的敏感字段不是明文，而是 `{"$wbEncrypted":1,"envelope":"..."}` 形式的 AES-256-GCM 密文。解密密钥由应用自身暴露：

```
<app>.exe -e 'process.stdout.write(String(process._linkedBinding(
    "electron_browser_workbuddy_storage").loggerGet()))'
env: ELECTRON_RUN_AS_NODE=1
→ {"version":1,"atRestSecretKey":"<base64 32 bytes>"}
```

`protectorKey = sha256(atRestSecretKey)`，再配合固定的 AAD 结构解开每个字段。

这段逻辑能工作，是因为它**就是官方客户端自己的做法**——插件只是把它转录成了 Python。凭证始终留在本机，插件不联网上传任何东西（除了正常调用模型接口）。

## 两个版本（国际版 / 国内版）

两个版本都支持，各自独立解析。同时安装并登录时默认用国际版（它的凭证文件每次启动应用都会刷新）。可以显式指定：

```bash
export WORKBUDDY_VARIANT=ai   # 强制国际版
export WORKBUDDY_VARIANT=cn   # 强制国内版
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `WORKBUDDY_VARIANT` | `ai` / `cn`，强制指定版本 |
| `WORKBUDDY_ACCESS_TOKEN` | 直接提供 bearer token（高级用法，绕过桌面应用读取） |
| `WORKBUDDY_APP_PATH` | 手动指定应用可执行文件路径 |

## 已知问题与注意事项

### 1. 代理会导致每次请求固定慢数秒

如果你的机器上有系统代理（Clash / V2Ray 等），**Python 客户端与桌面应用的行为不一样**：

- 桌面应用（Electron/Chromium）：走代理内核自身的规则分流，常见的 `GEOIP,CN,DIRECT` 能命中 → 直连，很快
- Python（httpx）：**只认 `HTTPS_PROXY` / `NO_PROXY` 环境变量**，不看代理软件的规则，也不看 Windows 注册表的 `ProxyOverride` → 全丢给代理 → 可能被兜底规则送去境外 → **每次请求多花 5 秒左右做 TLS 握手**

实测（`www.workbuddy.ai`）：

```
直连：tls=0.18s  ttfb=0.26s
代理：tls=5.09s  ttfb=5.19s
```

**本插件已内置修复**：`WorkBuddyProfile.create_client()` 返回挂载了 `trust_env=False` 的 httpx 客户端，从机制上忽略一切代理设置。所以正常安装即可，无需额外配置。

如果仍然慢，可以额外在你的 `.env` 里加（改完需重启 Hermes）：

```
NO_PROXY=localhost,127.0.0.1,::1,<你的内网段>,workbuddy.ai,.workbuddy.ai
no_proxy=<同上>
```

或者在代理软件里加规则：

```yaml
- 'DOMAIN-SUFFIX,workbuddy.ai,DIRECT'
```

### 2. 必须用流式请求

上游对非流式请求直接返回 `400 / 11101 Non-stream chat request is currently not supported`。插件已强制 `stream: true`，这个不用管，但如果你自己写脚本调接口需要留意。

### 3. `429 usage exceeds frequency limit`

这是**账号级配额**，不是网络问题，也不是插件问题。响应里会给出重置时间：

```json
{"code": 6004, "msg": "... your usage will reset at 2026-10-15 22:51:09 UTC+8 ..."}
```

到点自动恢复，或临时换其他模型。

### 4. 令牌有效期

桌面应用签发的 token 有效期很长（实测约一年），并且应用每次启动都会刷新凭证文件。插件缓存 5 分钟，改完登录态后执行 `hermes auth refresh workbuddy` 可立即重读。

## 兼容性

- 依赖 Hermes 的 provider 扩展点：`ProviderProfile.create_client`、`build_api_kwargs_extras`、`prepare_messages`、`auth_handler`
- 依赖 `httpx`（Hermes 自带）
- Python 3.11+

## 免责声明

本插件通过复用**你自己机器上已登录的官方桌面应用**的会话，让你在 Hermes 里使用该应用提供的模型。它不绕过任何付费或授权机制，也不包含、不传播任何凭证。

使用前请阅读并遵守 WorkBuddy / CodeBuddy 的服务条款。请自行确认你的使用方式符合其规定。

## License

MIT
