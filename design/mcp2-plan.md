# MCP 第二期实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 [mcp2.md](mcp2.md) 补全 Weaver 的 MCP 客户端能力：登录、提问、借模型、图片、prompts、工具清单变化、roots。

**Architecture:** 全部接官方 `mcp` 2.2 SDK 已有的接口（`OAuthClientProvider`、各种回调）；新代码只负责“接到 Weaver 的哪里”：`weaver/mcp/auth.py`（令牌、回调端口、gh、设备码）、`weaver/mcp/callers.py`（哪个任务在调）、`Runner.ask_during_call`（调用中途等人）。

**Tech Stack:** Python 3.14 + `mcp` 2.2（`httpx2`、`uvicorn` 随 SDK 装好）；SwiftUI。

**Spec:** `design/mcp2.md`

## Global Constraints

- 令牌文件 `~/.weaver/mcp-tokens.json`，0600，原子写（`weaver.settings.envfile.write_private`）；日志、错误信息里不出现令牌。
- 登录只在用户按 ⌘L（或 `weaver mcp login`）时开始；平时连接遇到“要登录”不弹浏览器。
- OAuth 回调只监听 `127.0.0.1`，只接一次，10 分钟超时；回调地址 `http://127.0.0.1:<端口>/callback`，端口优先用 33418，被占用就随机（换端口要重新注册客户端）。
- 等人的时间不算进调用超时。
- 没装 SDK 时一切照旧跳过（系统 Python 跑测试时 MCP 相关测试 skip）。
- 每步结束：`.venv/bin/python -m unittest` 和 `python3 -m unittest` 都是 OK；改了 Swift 的步骤 `xcodebuild … build` 成功；更新 api.md（v1.8 起）。
- 测试和实测都在临时 `WEAVER_HOME` 里做，不碰用户的 `~/.weaver`；App 实测用隔离实例（`WEAVER_DAEMON_JSON` + `KEYGENT_DEBUG_NAME`）。

## Review Focus

1. **令牌泄露**：令牌出现在日志、`/v1/settings/mcp` 的返回、异常文字里。→ 任务 1 测试：登录全过程的日志和列表返回里搜不到令牌。
2. **两个任务同时用同一个服务器，提问送错任务**。→ 任务 6 测试。
3. **登录到一半关掉设置页 / 再按一次 ⌘L**：不留下第二个回调端口、不卡死。→ 任务 2 测试：连续两次登录，第一次被取消。
4. **刷新令牌失败**（授权服务器撤销了）：状态回到“要登录”，不是一直“连不上”。→ 任务 1 测试。
5. **等你回答时任务被取消**：服务器收到 cancel，调用正常结束，任务不挂住。→ 任务 6 测试。

---

## 第 1 步：登录

### Task 1：`weaver/mcp/auth.py` + 连接池接上令牌

**Files:** Create `weaver/mcp/auth.py`；Modify `weaver/mcp/manager.py`（`_transport`、`ServerState`、`_describe`）；Test `tests/test_mcp_auth.py`，fixture `tests/fixtures/oauth_mcp_server.py`（SDK 的 `MCPServer` + 内存版 `OAuthAuthorizationServerProvider`：支持动态注册，`authorize` 直接重定向回 `redirect_uri?code=…&state=…`，签发会过期的令牌、支持刷新、可以“撤销全部”）。

**Interfaces（Produces）：**
- `class TokenFile(path)`：`get(url) -> dict | None`、`put(url, **fields)`、`drop(url)`；内容 `{url: {"tokens": {...}, "client": {...}, "kind": "oauth"|"device", "saved": 时间}}`。
- `class FileTokenStorage(TokenFile, url)`：实现 SDK 的 `TokenStorage`（`get_tokens`/`set_tokens`/`get_client_info`/`set_client_info`）。
- `class NeedsLogin(Exception)`：平时连接时 SDK 要走授权（`redirect_handler` 被调用）就抛它。
- `class Callback`：`start() -> redirect_uri`、`async wait(timeout) -> AuthorizationCodeResult`、`close()`；回调页面写“已登录，可以关掉这一页”。
- `def gh_token() -> str | None`：跑 `gh auth token`（5 秒超时），失败返回 None。
- `async def probe_login(cfg) -> dict | None`：401 + `WWW-Authenticate` 带 `resource_metadata` → `{"resource_metadata", "auth_server", "registration": bool, "device": bool}`；否则 None。
- `def http_auth(cfg, tokens: TokenFile, interactive: bool=False, redirect=None, callback=None)` → `(headers, auth)`：`auth: gh` → 头里放 gh 令牌；设备码令牌 → 头里放令牌；有 OAuth 记录或 `interactive` → `OAuthClientProvider`；否则 `(cfg.headers, None)`。
- `ServerState` 加 `needs_login: bool`、`login: dict | None`（进行中的登录：`{"kind", "url"|"user_code", "error"}`）。
- `_describe`：401 时先 `probe_login`，是 → `needs_login=True`，错误文字“要登录（设置 › MCP 里按 ⌘L）”。

- [x] 写测试：①没有令牌 → `needs_login`；②`auth: gh`（PATH 里放假 `gh` 脚本输出令牌）→ 连上；③令牌文件 0600、内容不进日志；④刷新：令牌过期后自动刷新还能调用；⑤服务器撤销全部令牌 → 状态回到 `needs_login`
- [x] 跑，确认失败
- [x] 实现
- [x] 跑，确认通过；全量测试

### Task 2：登录流程 + 接口 + 设置页后端

**Files:** Modify `weaver/mcp/auth.py`（`async def login(...)`）、`weaver/mcp/manager.py`（`login(key) -> dict`）、`weaver/settings/service.py`（`mcp_login(name)`、列表状态 `needs_login` / `logging_in`）、`weaver/settings/mcp.py`（删服务器时删令牌）、`weaver/settings/presets.json`（GitHub 不再要令牌：`"login": "github"`，`"github_client_id": ""`）、`weaver/daemon/server.py`（`POST /v1/settings/mcp/{name}/login`）、`weaver/mcp/pool.py`（背景信息里写“要登录”）、`design/api.md`（v1.8）。

**Interfaces：**
- `McpManager.login(key, open_url=…) -> dict`：按 mcp2.md 第一节的顺序挑路；浏览器授权：开回调端口、`OAuthClientProvider(interactive)`，后台任务里做一次连接触发授权，`redirect_handler` 记下 url 并 `open_url(url)`、立刻把 `{"kind": "browser", "url"}` 返回；成功后存令牌、重连；失败 / 超时 → `login["error"]`、`needs_login=True`。同一个服务器再次 login → 取消上一次（关掉它的回调端口）。
- 设备码：`POST https://github.com/login/device/code`（client_id、scope=`repo read:org read:user gist notifications project`），返回 `{"kind": "device", "user_code", "verification_uri", "expires_in"}`，后台按 `interval` 轮询，`slow_down` 时加 5 秒；拿到令牌 `kind="device"` 存好、重连。GitHub 的地址常量放在 auth.py，测试里可以换成假的。
- `Settings.mcp_login(name) -> dict`；列表 `status` 新增 `needs_login`、`logging_in`（`login` 字段带上进行中的登录信息，令牌不带）。

- [ ] 写测试：浏览器授权全流程（测试代替浏览器：拿到 url 后用 httpx 跟随重定向访问）→ 令牌落盘 → 状态 connected → 调工具成功；连续两次 login 只剩一个回调端口；设备码（假的 GitHub 接口：先 `authorization_pending` 两次再给令牌）；gh 路径立即 done；都走不通 → 400“这个服务器不支持自动登录…”；删服务器 → 令牌没了；接口和列表里搜不到令牌
- [ ] 跑，确认失败；实现；跑，确认通过；全量测试；api.md

### Task 3：App：⌘L、状态、设备码那一块

**Files:** Modify `Keygent/Keygent/Model/SettingsModels.swift`（`McpServerItem.login`、`LoginStart`）、`API/WeaverClient.swift`（`mcpLogin`）、`Store/AppStore+Settings.swift`（`⌘L`；`McpPageState.device: LoginStart?`；`esc` 关掉设备码那块）、`Views/Settings/McpPage.swift`（`needs_login` / `logging_in` 的行；设备码那一块：大号码、“已复制”、`⌘O 再开一次网页`）、`Views/Settings/SettingsView.swift`（底栏 `⌘L 登录`）。

- [ ] 实现；构建
- [ ] 隔离实例实测：加测试 OAuth 服务器 → `○ 要登录` → ⌘L → （测试代替浏览器访问 url）→ `● 已连接`；截图

### Task 4：命令行 `weaver mcp login <名字>` + 真实验证

- [ ] `weaver mcp login`：同样三条路，浏览器授权时打印网址、设备码时打印码
- [ ] 真实：GitHub 走 gh（本机已登录）→ 列出自己的仓库；Linear 或 Notion 浏览器授权（需要用户在浏览器里点“允许”——到这一步时请用户来点）
- [ ] mcp2.md 记进度

## 第 2–7 步

第 1 步做完、实测过之后，按同样的格式把第 2 步（哪个任务在调 + 调用中途等人）到第 7 步细化进这份计划再做——它们依赖第 1 步里对连接管理的改动，现在写细容易写错。
