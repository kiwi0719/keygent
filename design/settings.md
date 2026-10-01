# 设置页（模型 · MCP · Skills）设计

草案 v1 · 2026-10-02

## 〇、在整体里的位置

用户要求（2026-10-02）：MCP 能力不能缺；Keygent 里要有一个配置界面，管理模型（API Key、地址）、MCP 和 skills，全键盘操作。拆成三期，各自设计 → 实现 → 验证：

| 期 | 内容 | 文档 |
|---|---|---|
| **一** | **设置页 + 后端接口**（本文） | settings.md |
| 二 | 补全 MCP 客户端能力：prompts（启动器里 `/`）、elicitation（“等你的事”里的表单）、OAuth 登录（设置页里登录）、sampling（每次放行）、工具返回的图片给模型看、工具清单中途变化、roots | mcp.md 续 |
| 三 | Weaver 自己当 MCP 服务器（让 Claude Code、Cursor 派任务、看进度、回答等待） | 另写 |

先做一期：二期的登录入口、三期之后的“常用服务器”扩充都挂在设置页上。

**已定的选择**：配置由 weaverd 统一管（新接口 `/v1/settings/…`），App 只显示和调接口。理由：MCP 连接状态、OAuth 只有 weaverd 知道；校验只写一份；命令行以后也能用；不会两边同时写一个文件。唯一例外：weaverd 没在运行时（第一次用、没配模型），App 照现在这样直接写 `~/.weaver/.env`。

## 一、管什么、存在哪、怎么生效

| 分页 | 管的东西 | 存在哪 | 改了怎么生效 |
|---|---|---|---|
| 模型 | 地址、模型名、Key；高级项：上下文窗口（`WEAVER_CONTEXT_WINDOW`）、压缩阈值（`WEAVER_COMPACT_AT`）、同时跑几个任务（`WEAVER_MAX_RUNNING`）、MCP 连接超时（`WEAVER_MCP_TIMEOUT`） | `~/.weaver/.env`（0600） | 保存后 App 重启 weaverd（现有的 `restartDaemon`）；在跑的任务被打断、重启后自动接着跑 |
| MCP | 用户级服务器 | `~/.weaver/mcp.json`（改成 0600：里面可能有令牌） | 不用重启：每一轮开始前本来就读一次配置；保存后立刻在后台开始连 |
| Skills | `~/.weaver/skills/<名字>/SKILL.md` | 同左 | 不用重启：下一句话就看到 |

边界：

- **只管用户级。** 设置页不针对某个项目；项目里的 `.mcp.json`、项目 skills 不在这里，仍然走“信任这个项目吗”。
- **Claude Code 的 skills**（`~/.claude/skills/`）列出来、标“来自 Claude Code”，**只能看**，不改不删（别动另一个工具的文件）。新建的一律放 `~/.weaver/skills/`。
- **删除都是归档**：skill 目录移到 `~/.weaver/skills/.archive/<名字>-<时间>/`；MCP 服务器那一段配置追加进 `~/.weaver/mcp-archive.json`（带时间）。
- **weaverd 没在运行时**：设置页只开模型页（App 直接写 `.env`，就是现在的逻辑）；MCP、Skills 页显示“Weaver 没在运行”。
- Skills 这一期不做开关和安装（用户没选）。

## 二、接口（weaverd，`/v1/settings/`）

和现有接口同样的 token、错误格式 `{"error": {code, message}}`（message 是给人看的中文，App 原样显示）。

### 模型

- `GET /v1/settings/model` → `{"url", "model", "key": "••••abcd" | null, "advanced": {"context_window": "", "compact_at": "", "max_running": "", "mcp_timeout": ""}, "defaults": {"context_window": "自动（按模型）", "compact_at": "自动", "max_running": "4", "mcp_timeout": "10"}}`
- `PUT /v1/settings/model` `{"url", "model", "key"?, "advanced"?}` → 校验（地址 http/https、模型名不空、远程地址要有 key：没填就沿用已保存的，前提是同一家；高级项是正整数或空），写 `.env`（规则同现在的 `ModelSettingsState.apply`：只改这几项，别的行原样；换了地址就去掉 `WEAVER_PROVIDER` / `WEAVER_PROTOCOL`），回 `{"restart": true}`。App 收到后重启 weaverd。

### MCP

- `GET /v1/settings/mcp` → `{"servers": [{"name", "config": {原始配置}, "transport": "stdio"|"http", "status": "connected"|"connecting"|"failed"|"idle"|"invalid", "error", "tools": ["名字", …]}], "problem": "mcp.json 读不了：…" | null}`
  - `idle`：配置好了但还没连过（或闲置被关、工具清单还在时算 `connected`）；`invalid`：配置本身有问题（缺命令、环境变量没设置……），`error` 写原因。
- `POST /v1/settings/mcp/parse` `{"text"}` → `{"servers": {名字: 配置}, "conflicts": [已存在的名字]}`。认三种写法：`{"mcpServers": {…}}`、`{名字: 配置, …}`、单个服务器 `{"command": …}`（没有名字时用命令或地址猜一个，返回后可以改）。只解析，不保存。
- `POST /v1/settings/mcp` `{"servers": {名字: 配置}, "overwrite": false}` → 加入（一次可以多个）。同名且没给 `overwrite` → 409，`message` 列出同名的。保存后在后台连接。
- `PUT /v1/settings/mcp/{name}` `{"config"}` → 替换（可以借此改名：`{"config", "rename": "新名字"}`）；保存后重新连接。
- `DELETE /v1/settings/mcp/{name}` → 归档。
- `POST /v1/settings/mcp/{name}/reconnect` → 断开重连（不受“60 秒内不重试”限制）。
- `GET /v1/settings/mcp/presets` → 常用服务器清单 `[{"id", "name", "title", "description", "config", "needs": [{"env": "GITHUB_TOKEN", "label": "GitHub 令牌", "url": "https://github.com/settings/tokens"}]}]`。
- `POST /v1/settings/mcp/presets/{id}` `{"values": {"GITHUB_TOKEN": "…"}}` → 令牌写进 `.env`（weaverd 自己的进程环境也更新，不用重启），配置里只写 `${GITHUB_TOKEN}`，然后同 `POST /v1/settings/mcp`。
- `GET /v1/settings/mcp/import` → `{"sources": [{"from": "Claude Code" | "Claude 桌面版", "path", "servers": {名字: 配置}}]}`。读 `~/.claude.json` 的顶层 `mcpServers`（用户级）和 `~/Library/Application Support/Claude/claude_desktop_config.json`。导入用 `POST /v1/settings/mcp`。

**常用服务器（这一期）**：只放不用 OAuth 登录的；要令牌的加的时候填。**每个都实际装起来连一次，连得上才收录**（实现时确认包名和参数）。候选：文件（`@modelcontextprotocol/server-filesystem`，参数是要开放的目录，加的时候填）、抓网页（`mcp-server-fetch`，uvx）、浏览器（`@playwright/mcp`）、GitHub（远程 HTTP + 令牌）、记忆（`@modelcontextprotocol/server-memory`）。要 OAuth 的（Notion、Linear、Sentry……）二期加。

### Skills

- `GET /v1/settings/skills` → `{"skills": [{"name", "description", "source": "weaver"|"claude", "editable", "path"}], "problems": […]}`（problems：frontmatter 坏了的文件，照样列出来能打开修）
- `GET /v1/settings/skills/{name}` → `{"name", "text", "editable", "files": [同目录下别的文件]}`
- `POST /v1/settings/skills` `{"text"}` → 新建：从 frontmatter 取 name。
- `PUT /v1/settings/skills/{name}` `{"text"}` → 保存；frontmatter 的 name 变了就把目录一起改名（目录里的脚本等跟着走）。
- `DELETE /v1/settings/skills/{name}` → 归档。
- 校验：有 frontmatter、`name` 只能是小写字母数字和 `-`（最长 64）、有 `description`、名字不和别的 skill 重复（含 Claude Code 的）、只能写 `~/.weaver/skills/`。

## 三、界面和键盘

沿用现有习惯：⌘↵ = 确定 / 放行；⌘数字 = 眼前窗口里第几条（一屏 5 条，↑↓ 挪窗口，`ListWindow`）；空格 = 勾选 / 展开；esc 退一层；底部一行写着现在能按什么。

**整体**

- ⌘, 打开 / 关闭设置，占满整个面板（就是现在模型设置的位置）。
- 顶部三个分页：模型 · MCP · Skills。**⌘[ / ⌘]** 切换（⌃Tab / ⌃⇧Tab 也行）；记住上次在哪页。

```
┌─ 设置 ──────────────────────────────────────────┐
│  模型    [ MCP ]    Skills              ⌘[ ⌘] 切换 │
├───────────────────────────────────────────────┤
│ ⌘1 ● github     已连接 · 32 个工具               │
│ ⌘2 ● fetch      已连接 · 1 个工具                │
│▸⌘3 ◌ playwright 连接中                          │
│ ⌘4 ✕ sentry     连不上：连接超时（10 秒）           │
├───────────────────────────────────────────────┤
│ ↑↓ 选  ↵ 编辑  ⌘N 添加  ⌘R 重连  ⌘⌫ 删除  esc 关闭 │
└───────────────────────────────────────────────┘
```

**模型页**：一行一项——地址、模型、Key；分隔线下是高级项（空着 = 默认，灰字写着默认值）。↑↓ / Tab 换行，↵ 保存（自动重启 Weaver），esc 不保存退出。第一次用时只有这一页，标题是“先接上一个模型”（同现在）。

**MCP 页**

- 列表见上图。`●` 已连接（工具数）、`◌` 连接中、`○` 还没连、`✕` 连不上 / 配置有问题（原因）。列表每 2 秒刷新一次状态（只在这一页开着时）。
- ↵ 打开编辑器：上半是这个服务器的 JSON（`{"command": …}` 这一层，名字在标题里，⌘R 不可用）；下半是工具名（只读）。⌘↵ 保存（保存后重连），esc 退出。JSON 错了底部说错在哪，不让保存。要改名：编辑器标题那行就是名字，Tab 过去改。
- ⌘R 重连；⌘⌫ 删除（归档）：底部变成“再按一次 ⌘⌫ 删除 github”，按别的键取消。
- ⌘N 添加：先出三选一（⌘1/⌘2/⌘3 或 ↑↓↵）：
  1. **粘贴 JSON**：空编辑器，⌘V 贴进去，下面实时显示“认出了 2 个服务器：github、fetch”（调 parse）；同名的标“会覆盖”。⌘↵ 加入。
  2. **常用服务器**：可以打字筛选的列表，↵ 选中；有要填的（令牌、目录）就出一行输入框，底部写着去哪拿（⌘O 打开那个网址），↵ 加入。
  3. **从 Claude Code 导入**：按来源分组列出，空格勾选（默认全选、已存在的不选并标“已有”），⌘↵ 导入。一个也没找到就直接说“没找到 Claude Code / Claude 桌面版配过的 MCP 服务器”。

**Skills 页**

- 列表：名字、一句话说明、来源（Weaver / Claude Code，后者灰一点）。
- ↵ 打开编辑器，编辑整份 SKILL.md（等宽字体）。Claude Code 的只读，标题写“只读 · 来自 Claude Code”。
- ⌘N 新建：编辑器里预填
  ```
  ---
  name: 
  description: 
  ---

  ```
  光标停在 `name: ` 后面。
- ⌘↵ 保存（新建 / 修改；改了 name 就连目录一起改名）；名字不合法、重名、缺说明在底部报错。⌘⌫ 删除（归档），同样按两次确认。

**编辑器通用**：多行文字里 ↵ 是换行，所以保存一律 ⌘↵；有改动没保存时 esc 先提示“再按一次 esc 放弃修改”。

## 四、出错处理

- 校验都在 weaverd 做；App 把 `message` 原样放在底部那一行（红字）。
- 写文件不留半截：先写临时文件再改名，0600（同 `daemon.json`）。
- 同时改：设置相关的写操作在 weaverd 里排队（一把锁），每次都基于文件**当前**内容改——命令行或手动改的不会被旧内容覆盖。
- `mcp.json` 被手动改坏：MCP 页顶部提示“mcp.json 读不了：第 N 行 …”，列表空；**这时拒绝添加 / 修改**（不覆盖手写内容），让用户先手动修好。
- 模型保存后 weaverd 起不来（比如地址写错导致启动失败）：App 走现有的“Weaver 没在运行”路径，自动打开模型页。
- 服务器连不上不影响保存；列表里写原因，日志在 `~/.weaver/mcp-logs/`，⌘R 重连。
- `.env` 里不归设置页管的行（注释、别的变量）原样保留。

## 五、代码结构

```
weaver/settings/            （新，纯 Python，不依赖 HTTP）
  envfile.py    读写 .env：只改指定的 key，别的行原样；原子写 0600（和 Swift 的 EnvFile 规则一致）
  model.py      模型设置：读、校验、写；高级项
  mcp.py        mcp.json 读写、parse、归档、presets、从 Claude 导入
  skills.py     skill 的列表、读、新建 / 保存 / 改名、归档、校验
  presets.json  常用服务器清单
weaver/daemon/server.py     加 /v1/settings/* 路由（薄：解析参数 → 调上面 → 回 JSON）
weaver/mcp/pool.py          加：按名字查用户级服务器的状态、重连
Keygent/
  Model/SettingsModels.swift         接口类型
  API/WeaverClient.swift             加设置接口
  Store/AppStore+Settings.swift      设置页的状态和按键（替换现在 AppStore 里的模型设置部分）
  Views/Settings/SettingsView.swift  外框 + 分页
  Views/Settings/ModelPage.swift     （由 ModelSettingsView 改来）
  Views/Settings/McpPage.swift       列表 + 编辑器 + 添加（三种）
  Views/Settings/SkillsPage.swift    列表 + 编辑器
  Views/Components/CodeEditor.swift  多行等宽编辑器（JSON / SKILL.md 共用）
design/api.md               v1.6：设置接口
```

## 六、测试

- **Python（`tests/test_settings.py`）**：每个接口的正常和出错；key 只回末 4 位；`.env` 别的行原样；parse 认三种写法、冲突；归档；Claude Code / 桌面版导入（假配置文件）；skill 新建、改名带目录、重名、缺说明、Claude Code 只读、归档；并发保存不丢；`mcp.json` 坏了拒绝写；用测试服务器验证保存后状态从“连接中”到“已连接”、工具数对。
- **常用服务器**：清单里每个都实际装起来连一次，连得上才收录。
- **App**：构建通过；用调试入口（`KEYGENT_DEBUG_HOOKS`）+ 按键模拟走一遍：⌘, → 切三个分页 → 粘贴 JSON 加服务器、看到连上 → 新建 skill、改名、删除 → 改模型高级项保存重启。每屏截图检查。
- 最后在用户正在用的 Keygent 里跑一遍（先确认 weaverd 没有在跑的任务再重启）。

---

## 进度（2026-10-02，第一期已完成）

**后端**：`weaver/settings/`（envfile、model、mcp、skills、service、presets.json）+ `server.py` 的 `/v1/settings/*` + `weaver/errors.py`（三个接口异常挪出来，settings 不依赖 daemon）。测试 `tests/test_settings.py` 19 项（含并发写 10 个都在、令牌不进日志、坏 mcp.json 拒绝写且不动文件、真连测试服务器看状态变化）。

**App**：`Views/Settings/`（SettingsView、ModelPage、McpPage、SkillsPage）、`Views/Components/CodeEditor.swift`（NSTextView，关掉智能引号）、`Store/AppStore+Settings.swift`、`Model/SettingsModels.swift`；删掉 `ModelSettingsView.swift`。

**和设计不一样 / 补充的**：

- 常用服务器 5 个都实际连过：文件 14 个工具、抓网页 1、浏览器 25、知识图谱记忆 9；GitHub 地址正确（错令牌回 401），全部收录。`needs` 的格式改成 `{"id", "kind": "env"|"arg", "label", "url"?}`。
- **连不上的原因写成人话**（`weaver/mcp/manager.py` 的 `_describe`）：SDK 会吞掉 HTTP 状态码，所以远程服务器失败时再探一次——“服务器拒绝了（HTTP 401）：令牌不对、过期或没有权限”“地址不对（HTTP 404）”“连不上 x：网络不通或地址写错”；本地服务器：“找不到命令 x”“服务器进程退出了，看日志 …”。
- `McpConfig` 的环境变量改成每次读当前进程的（设置页填的令牌对已经开着的任务也生效）。
- **内置 skills**（另一个会话同时加的 `weaver/builtin_skills/`）也列出来，只读，标“内置”；可以新建同名的 Weaver skill 盖过它。
- 联调隔离：`WEAVER_DAEMON_JSON` 设了时 App 的 weaver home 跟着那个文件走、不注册 / 不重启 launchd 里的 weaverd；`KEYGENT_DEBUG_NAME` 让调试通知只发给这个实例、也不抢 ⌥空格。调试入口加了 `settings[:tab]`、`keys:cmd+n`、`settext:`、`settings-state`、`winid`。
- 任务 5 的单独自检并进了界面实测（界面用到全部接口）。

**实测**（隔离的测试实例 + 临时 weaverd，真模型配置）：MCP 页 空状态 → ⌘N → 粘贴 JSON 认出 1 个 → ⌘↵ → “连接中”→“已连接 · 4 个工具”；常用服务器加“抓网页”“文件（填目录）”都连上；GitHub 不填令牌报“要填：GitHub 令牌”；编辑改名、⌘R 重连、⌘⌫ 两次删除进归档；从 Claude Code 导入（本机没有配置，显示“没找到”）。Skills 页：新建、改名（目录跟着改）、有改动 esc 提示、⌘⌫ 两次归档；Claude Code / 内置的只读。模型页：高级项填 abc 报错、填 6 保存写进临时 home 的 .env、真实的 `~/.weaver/.env` 没变。

**实测中修掉的问题**：① Swift 独占访问崩溃（`settings?.x = f(settings!.y)` 同一个表达式里边改边读）；② 切模式时新出现的输入框拿不到焦点（`focusWhenRequested`）。

**没做**：在用户正在用的 Keygent 里验证——另一个会话同时在改、在重启 Keygent（测试中途两个实例都被它重启过），这次没有去动用户的构建和 weaverd；下次 ⌘R 构建后，App 会因代码指纹变了自动重启 weaverd，设置页的 MCP / Skills 页才有接口可用。真实键盘输入（打字时引号不被替换成弯引号）没法用调试入口模拟，留给人工确认。

**独立审查找到、已修的（2026-10-02）**：

1. 高级项“同时跑几个任务”保存了不生效：`MAX_RUNNING` 在 import 时读环境变量，早于读 `~/.weaver/.env`。改成 `resolve_max_running()` 在 load_env 之后读。
2. 换了令牌仍用旧连接：连接池的 key 按 `${VAR}` 展开前的配置算，令牌变了 key 不变。改成按展开之后的内容算。
3. 新建 skill 可能盖掉一个目录名相同、但 frontmatter 名字不同的 skill：新建前先看目录在不在（409）。
4. ⌘, 关闭设置时直接丢掉编辑器里没保存的内容：改成和 esc 一样，先提示再按一次。
5. 模型页保存时把打开那一刻的高级项全发回去，会盖掉期间命令行 / 手动改的：只发改过的项；连着还没有设置接口的旧 weaverd（404）时退回直接写 `.env`。
6. 异步请求回来时不管设置页是不是已经关了 / 换了分页，会抢焦点、盖掉刚按 ⌘N 的内容；连按 ⌘↵ 会发两次：加了 `submit()`——一次只发一个，回来时对一下“还是同一份设置页、同一个分页”。
7. `.env`、`mcp.json`、SKILL.md 是软链（链到 dotfiles）时，写入会把软链换成普通文件：写到它指向的文件。
8. 叫 `presets` 的服务器重连会被当成“添加常用服务器”：reconnect 路由排到前面。

第 1、2、3、7、8 条有测试（`tests/test_settings.py` 的 `ReviewFixes` 和路由测试）；第 4、5 条在隔离实例里实测过（外面改的 `WEAVER_COMPACT_AT` 保存后还在；有改动时第一次 ⌘, 只提示）。第 5 条的 404 退回、第 6 条的竞态没有实测。
