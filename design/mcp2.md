# MCP 第二期：补全客户端能力

草案 v1 · 2026-10-02 · 接 [mcp.md](mcp.md)（第一版 + 第九节常驻服务）和 [settings.md](settings.md)（设置页）

## 〇、要补的和不补的

用户要求：MCP 能力不能缺（2026-10-02）。Weaver 作为**客户端**，MCP 标准里还没做的：

| 能力 | 现在 | 这一期 |
|---|---|---|
| 远程服务器登录（OAuth） | 只能在 `headers` 里写令牌 | 一键登录：浏览器授权 / 复用 `gh` / 设备码（第一节） |
| elicitation（服务器向你提问） | 一律拒绝 | 表单进“等你的事”，填完交回服务器（第二节） |
| sampling（服务器借用模型） | 一律拒绝 | 每次问你放行，用这个任务的模型（第三节） |
| prompts（提示词模板） | 没做 | 启动器里打 `/` 选用（第四节） |
| 工具 / 资源返回的图片 | 写一句“[图片，未展开]” | 真的给模型看（第五节） |
| 工具清单中途变化 | 下个会话才生效 | 下一轮生效（第六节） |
| roots（告诉服务器能碰哪些目录） | 没声明 | 声明并按任务给（第六节） |

**不在这一期**：Weaver 自己当 MCP 服务器（第三期，另写）；资源订阅（`resources/subscribe`，没有用得上的场景）；服务器日志转发到界面（写进 `mcp-logs/` 就够）。

SDK：全部用官方 `mcp` 2.2 已有的接口——`OAuthClientProvider` + `TokenStorage`、`elicitation_callback`、`sampling_callback`、`list_roots_callback`、`message_handler`、`list_prompts` / `get_prompt`。**不自己实现协议**，只写“接到 Weaver 的哪里”。

## 一、登录

### 1. 什么时候算“要登录”

远程服务器连接时回 401，并且带 `WWW-Authenticate: Bearer … resource_metadata="…"`（MCP 授权规范的标志；GitHub、Notion、Linear、Sentry 都这样）→ 状态 **`needs_login`**（设置页显示 `○ 要登录 · ⌘L`），不算“连不上”。连接时**不自动弹浏览器**：只有你在设置页按 ⌘L 才开始登录（任务跑着跑着突然弹浏览器很吓人）。

背景信息里对模型说：“X：要登录，请用户在设置 › MCP 里按 ⌘L 登录”，模型就会这样告诉你。

### 2. 三条路（⌘L 时按顺序挑第一条能走的）

| 情况 | 做法 |
|---|---|
| 服务器支持客户端自动注册（授权服务器元数据里有 `registration_endpoint`；Notion、Linear、Sentry 等） | **浏览器授权**：SDK 的 `OAuthClientProvider` 做发现、注册、PKCE；weaverd 在 `127.0.0.1` 上开一个只接一次的回调端口；`open` 打开授权页；你点“允许”后浏览器跳回本机，页面显示“已登录，可以关掉这页”；令牌存好、自动重连 |
| GitHub，且本机 `gh` 已登录 | **复用 gh**：配置写成 `"auth": "gh"`，每次连接时现取 `gh auth token` 放进请求头。不存令牌，gh 换了令牌也跟着换。零点击 |
| GitHub，没有 gh | **设备码**：weaverd 向 GitHub 要一个码（如 `ABCD-1234`），App 弹出这个码、复制到剪贴板、打开 `github.com/login/device`；你粘贴、点授权；weaverd 每几秒问一次，拿到令牌就存好、重连。需要一个注册好的 GitHub OAuth App 的 Client ID（见下） |

都走不通（比如服务器既不支持自动注册、也不是 GitHub）：提示“这个服务器不支持自动登录，请在它的网站上生成令牌，填进配置的 headers”，并打开编辑器。

**GitHub 的 Client ID**：GitHub 不支持自动注册，设备码要用一个注册好的应用。由用户在自己的 GitHub 账号下注册一个叫 Weaver 的 OAuth App（开启 Device Flow），Client ID 是公开的标识、不是密码，写进 `weaver/settings/presets.json`。没有它时只走 gh 那条路，再不行退回填令牌。

### 3. 令牌存哪、怎么续期

- `~/.weaver/mcp-tokens.json`（0600），按服务器地址存：令牌、刷新令牌、过期时间、注册得到的客户端信息。和 `.env` 里的模型 key 同一个保护级别（第二步再考虑放进钥匙串）。
- 浏览器授权得到的：交给 SDK 的 `OAuthClientProvider` 用，过期前自动刷新；刷新失败 → 状态回到 `needs_login`。
- 设备码得到的（GitHub OAuth App 的令牌默认不过期）：直接放进请求头；401 → `needs_login`。
- 删掉服务器时，它的令牌一起删。设置页 ⌘L 对已登录的服务器 = 重新登录。

### 4. 接口和界面

- `POST /v1/settings/mcp/{name}/login` → 三种回应之一：
  - `{"kind": "browser", "url": "…"}`：weaverd 已经用 `open` 打开授权页（返回 url 是给“浏览器没打开？⌘O 再开一次”用的）；
  - `{"kind": "device", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device", "expires_in": 900}`；
  - `{"kind": "done"}`（gh：立刻就好）。
- 登录进行中，列表里的状态是 `logging_in`；成功变 `connecting` → `connected`；超时（10 分钟）或你拒绝了 → `needs_login` + 原因。设置页本来就每 2 秒刷新列表，不另加推送。
- 设置页 MCP 列表：`needs_login` 的行显示 `要登录`；底栏多一个 `⌘L 登录`。设备码时在列表上方出一块：大号的码 + “已复制，在打开的网页里粘贴” + `⌘O 再开一次网页` + `esc 不登了`。
- 常用服务器里的 GitHub：不再要求填令牌。加入时有 gh 就直接用 gh；没有就加入后立刻开始设备码登录。
- 命令行：`weaver mcp login <名字>`（同样三条路，码印在终端里）。

## 二、服务器向你提问（elicitation）

MCP 服务器在一次工具调用中途可以问用户要信息（“部署到哪个环境？”“确认删除这 3 个文件吗？”）或让用户打开一个网址（第三方授权、付款）。

### 1. 变成什么

- 一件新的等待 **`kind: "elicit"`**，挂在**发起这次调用的那个任务**里，进“等你的事”；任务状态 `waiting`，`now` 是“<服务器> 问你：<问题>”。
- 两种：
  - **表单**（`mode: form`）：服务器给一个平铺的字段列表（字符串、数字、布尔、单选枚举，MCP 规定只能这几种），App 一行一个字段。
  - **网址**（`mode: url`）：显示说明和网址，`⌘O` 打开；你在网页上弄完后按“好了”。
- 选择：`accept`（交上去，表单带 `values`）/ `decline`（不给）/ `cancel`（取消这次调用）。和审批一样，任务被取消时这件等待作废，服务器收到 `cancel`。
- 等你的事里：标题“<服务器> 问你：<问题>”；表单的键盘：Tab / ↑↓ 在字段间移动，空格切换布尔，枚举用 ←→ 或数字键选，`⌘↵` 交上去，`⌘⌫` 不给，`esc` 先放着。
- 接口：`POST /v1/waits/{id}` `{"decision": "accept", "values": {"env": "staging"}}`。值按服务器给的类型和必填项校验，不对回 400。
- 子 Agent 的调用里来的提问：照审批的做法升到主任务（`Runner.ask_up`）。命令行：在终端里逐个字段问。
- **敏感信息**：MCP 规定表单不能用来要密码 / 令牌。我们照样显示服务器原话，并在表单上方写一行“<服务器> 在要信息；不要在这里填密码”。填的值不进模型的上下文（只交给服务器）。

### 2. 怎么接到“哪个任务”

所有任务共用连接，服务器的提问要送回发起调用的那个任务：
- 新协议（2026-07-28 版）里提问是这次调用结果的一部分，SDK 在 `call_tool` 里面调回调——用 `contextvars` 记下“当前是哪个任务在调”，回调里直接拿到。
- 旧协议里是服务器单独发来的请求：查这个服务器上**正在进行的调用**，只有一个就是它；有几个就交给最近开始的那个；一个都没有就拒绝（“现在没有在进行的调用”）。

### 3. 超时

一次调用的超时（120 秒）**不算等你的时间**：等你回答期间暂停计时，否则你去倒杯水回来调用已经超时了。

## 三、服务器借用模型（sampling）

服务器请 Weaver 的模型帮它生成一段文字（比如让模型总结它查到的东西）。

- **每次都问你**：一件 `kind: "approval"` 的等待，标题“<服务器> 想借用模型”，正文是它要发给模型的内容（前 500 字）和上限 token；可选“总是允许 <服务器>”（记进账本，同 MCP 工具的“总是允许”）。
- 放行后用**这个任务的模型**生成（不带工具、不带我们的 system prompt，只用服务器给的 system 和消息），结果交回服务器。
- **用量记账**：记进这个任务的账本（`ActionCompleted`，`kind: "sampling"`，和记忆提取一样只当记录），算进这个任务的 token 预算；预算不够时直接拒绝。
- 只声明基本的 sampling 能力，**不声明“sampling 带工具”**（让服务器借我们的模型去调它自己的工具，绕开了我们的权限，不做）。
- 不在任何调用里发来的 sampling（服务器自己主动要）：拒绝。

## 四、提示词模板（prompts）

服务器提供的现成提示词（比如 GitHub 的“审查这个 PR”、Sentry 的“分析这个错误”），带参数。

- **启动器里打 `/`**：列出当前工作区能用的所有 prompt（用户级服务器 + 这个项目已信任的服务器），显示为 `/github:review-pr  审查一个 PR`，打字筛选，↑↓ 选，↵ 选中。
- 有参数：启动器下方出一行一个参数的小表单（必填的标 `*`），Tab 换行，↵ 交出去；没参数：直接交出去。
- 交出去 = 新建一个任务：weaverd 调 `get_prompt` 拿到消息，把文字拼起来作为这个任务的第一句话（服务器给的图片、资源作为附件）。任务的标题用 prompt 的名字。
- 接口：`GET /v1/prompts?workdir=…` → `{"prompts": [{"server", "name", "title", "description", "arguments": [{"name", "description", "required"}]}]}`；新建任务时 `POST /v1/tasks` 可以用 `{"prompt": {"server", "name", "arguments": {…}}, "workdir"}` 代替 `text`。
- 以后可以把 skills 也放进 `/` 列表，这一期不做。

## 五、图片

工具结果、资源里的图片现在只写一句“[图片，未展开]”。改成真的给模型看：

- MCP 返回的图片解码后存进 blob 仓库（和附件同一个），工具结果变成“文字 + 图片引用”的列表。单张上限 5 MB、一次结果最多 4 张，多的写“还有 N 张没展开”。
- 投影（`project.py`）：工具结果可以是字符串或片段列表。
- Anthropic：`tool_result` 的内容本来就能放图片，直接放。
- OpenAI 兼容协议：工具消息只能是文字——工具消息里写“[图片见下一条]”，紧跟着补一条用户消息放图片（Codex、OpenCode 也这样做）。
- 模型能不能看图：和现在的图片附件一样，一律按能看处理（不能看的模型会报错，报错原样交给模型和你）。
- 资源（`mcp_read_resource`）里的图片同样处理；音频、视频还是一句说明。
- 脱敏：只对文字部分做（图片里的字不处理，写进设计的已知限制）。

## 六、工具清单变化、roots

- **`tools/list_changed`**（服务器说“我的工具变了”）：连接池重新拉一次工具清单；用到它的任务在**下一轮开始时**换（第一版说“下个会话”，现在是任务常驻，等下个会话就是永远）。会让缓存前缀断一次，只在服务器真的变了时发生。`prompts/list_changed`、`resources/list_changed` 同理刷新。
- **roots**（告诉服务器“你可以碰这些目录”）：
  - 项目级服务器：项目目录。
  - 用户级服务器（所有任务共用一个连接）：在某次调用里被问到时 = 发起调用的那个任务的工作目录；平时 = 空列表。
  - 声明 `roots.listChanged`：任务换了工作目录不会发生，所以实际不会发通知，只为了让服务器知道我们支持。

## 七、代码结构

```
weaver/mcp/
  auth.py        TokenStorage（mcp-tokens.json）、回调端口、gh、GitHub 设备码、“要登录”的判断
  callers.py     “当前是哪个任务在调”：contextvar + 每个服务器正在进行的调用；提问 / 借模型 / roots 都靠它找任务
  manager.py     接上 auth、各个回调、list_changed；超时不算等人的时间
  pool.py        TaskMcp：调用时登记自己；prompts 列表
  tools.py       图片结果
weaver/runner.py         ask_during_call（同 ask_up 的机制：记一件等待、阻塞到有回答）
weaver/project.py、providers/*   工具结果里的图片
weaver/daemon/status.py  elicit 等待、sampling 审批的显示和选择
weaver/daemon/server.py  /login、/v1/prompts、POST /v1/tasks 的 prompt
weaver/__main__.py       weaver mcp login；命令行里回答提问
Keygent：设置页 ⌘L 和设备码那一块；等你的事里的表单；启动器的 / 列表和参数表单
design/api.md            v1.8
```

## 八、测试与实测

- 测试服务器（`tests/fixtures/fake_mcp_server.py`）加：一个会提问的工具、一个会借模型的工具、一个返回图片的工具、一个 prompt、一个调用后改工具清单的工具。
- 登录：本机起一个假的 OAuth 授权服务器（发现、注册、授权、换令牌、刷新），走完整的浏览器授权（用测试代替浏览器去访问授权地址）；gh 用假的 `gh` 脚本；设备码用假的 GitHub 接口。
- 提问：表单的校验、拒绝、取消；任务取消时作废；子 Agent 里的升到主任务；两个任务同时调同一个服务器，提问各回各的；超时暂停。
- 借模型：放行 / 拒绝 / 总是允许、用量记账、预算不够拒绝。
- 图片：两种协议的投影、大小上限、不支持图片时的退回。
- **真实验证**：Notion（或 Linear）的浏览器登录；GitHub 用 gh 登录后列出自己的仓库；用官方的 `everything` 测试服务器（`@modelcontextprotocol/server-everything`，各种能力都有）验证提问、借模型、图片、prompt；App 里截图走一遍。

## 九、实现顺序

| 步 | 内容 | 为什么这个顺序 |
|---|---|---|
| 1 | 登录（gh → 浏览器授权 → 设备码） | 用户最先碰到的；GitHub 现在就要 |
| 2 | “哪个任务在调” + 调用中途等人（`ask_during_call`、超时暂停） | 提问和借模型都靠它 |
| 3 | 提问（elicitation）+ 等你的事里的表单 | |
| 4 | 借模型（sampling） | |
| 5 | 图片 | 改内核投影和两种协议，独立 |
| 6 | prompts + 启动器 `/` | 界面最多 |
| 7 | list_changed、roots | 小 |

每步做完都跑全量测试、更新 api.md，能实测的实测。

---

## 进度

### 第 1 步：登录（2026-10-02，已完成）

- `weaver/mcp/auth.py`：令牌文件（`mcp-tokens.json`，0600）、SDK `TokenStorage`、只接一次的回调端口（优先 33418）、gh、GitHub 设备码、`probe_login`、`http_auth`。
- `McpManager`：远程连接带上令牌；401 且服务器声明了授权信息 → `needs_login`；`login(key, "browser" | "device")` 在后台进行，再登录一次会取消上一次。
- 设置页后端：`mcp_login` 选路（GitHub + gh → 配置改 `"auth": "gh"`；支持自动注册 → 浏览器；GitHub 无 gh → 设备码，需要 Client ID）；列表状态 `needs_login` / `logging_in`；删服务器时删令牌；常用服务器 GitHub 不再要令牌。`POST /v1/settings/mcp/{name}/login`（api.md v1.8）。
- App：`⌘L` 登录、`⌘O` 再开一次授权页、设备码那一块（码自动复制）、加完 GitHub 自动开始登录。
- 命令行：`weaver mcp`（列表）、`weaver mcp login <名字>`；weaverd 没在运行时本地登录。
- 测试 `tests/test_mcp_auth.py` 11 项：用 SDK 自带的授权服务器写的夹具（`tests/fixtures/oauth_mcp_server.py`）走真的浏览器授权流程；重启后读回令牌、过期先刷新（不重新授权）、撤销后回到“要登录”、两次 ⌘L 只剩一个回调端口、令牌不进日志和接口、gh、设备码（假的 GitHub 接口）、命令行。

**实现中发现、和设计不一样的**：

1. SDK 读回存着的令牌时不知道它什么时候过期，过期后会直接要求重新授权、不先刷新 → 令牌文件另记绝对过期时间，`OAuthClientProvider` 的子类读回时告诉 SDK。
2. 传给 SDK 的 HTTP 客户端它不负责关，每次重连漏一个连接（测试里的资源警告抓到的）→ 连接结束时自己关。
3. GitHub 的 MCP 服务器不支持自动注册客户端（授权服务器元数据里没有 `registration_endpoint`），所以 GitHub 只有 gh 和设备码两条路；设备码要等用户注册 Weaver OAuth App 拿到 Client ID（`weaver/mcp/auth.py` 的 `GITHUB_CLIENT_ID`，或环境变量 `WEAVER_GITHUB_CLIENT_ID`），没有时提示用 gh。

**实测**（隔离的测试实例 + 临时 weaverd）：
- 本地 OAuth 测试服务器：列表“要登录 · ⌘L”→ ⌘L → 默认浏览器打开授权页、自动跳回 → “已连接 · 1 个工具”。
- 真的 GitHub：常用服务器加 GitHub → 用本机 gh 的登录直接连上，45 个工具，配置里只有 `"auth": "gh"`、没存令牌；真实任务“我的用户名、最近更新的 3 个仓库”：工具总数超过 30 自动用搜索模式，调了 `get_me`、`search_repositories`（只读，没问人），回答正确。
- `weaver mcp` 列出各服务器状态。

没实测：真的第三方服务（Notion / Linear）的浏览器授权——需要你在浏览器里点“允许”，等你方便时一起做；GitHub 设备码——等 Client ID。
