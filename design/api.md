# weaverd 接口说明（给 Keygent）

草案 v1 · 2026-10-01 · 对应 design/daemon.md 第六节

这份是给前端照着写的：每个接口都有完整的请求和返回示例，字段名就是最终的名字。后端还没实现到 HTTP 这一层，但返回的内容已经由 `weaver/daemon/status.py`、`humanize.py`、`tasks.py` 算好了，形状不会变。

---

## 0. 约定

**连接**
- 地址只有本机：`http://127.0.0.1:<port>`。端口和 token 从 `~/.weaver/daemon.json` 读：
  ```json
  {"port": 47615, "token": "wv_3f9a…", "pid": 81234, "version": "0.1"}
  ```
- 每个请求带 `Authorization: Bearer <token>`。
- 读不到 `daemon.json`，或者连不上 → Keygent 显示“Weaver 没在运行”。

**格式**
- 请求、返回都是 JSON，UTF-8。
- 时间一律是 **Unix 秒（浮点数）**，例如 `1790000000.25`。“3 分钟前”“0:05”这类显示由前端算：
  - 任务列表的 `when` = 现在 − `updated`
  - 步骤的 `time`（“0:05”）= 步骤 `ts` − 任务 `created`
- id 都是字符串，前端不要解析它。

**出错**
所有错误都是同一个形状，配合 HTTP 状态码：
```json
{"error": {"code": "not_found", "message": "没有这个任务：a1b2c3"}}
```

| 状态码 | code | 什么时候 |
|---|---|---|
| 400 | `bad_request` | 缺字段、字段类型不对、工作目录不存在、附件 id 不存在 |
| 405 | `bad_request` | 路径对、方法不对（比如 `DELETE /v1/status`） |
| 401 | `unauthorized` | token 不对或没带 |
| 404 | `not_found` | 任务 / 等待 / 附件不存在 |
| 409 | `conflict` | 这件等待已经被回答了（比如命令行抢先放行了） |
| 413 | `too_large` | 附件太大（上限 50 MB） |
| 500 | `internal` | 服务内部出错，`message` 是第一行原因 |

`message` 是给人看的中文，可以直接显示。

---

## 1. 几个共用的对象

### 1.1 任务状态 `status`

| 值 | 含义 | Keygent `TaskKind` | 列表上的颜色 |
|---|---|---|---|
| `running` | 在跑 | `.run` | 绿 |
| `waiting` | 有事等你 | `.wait` | 琥珀 |
| `queued` | 排队中（同时在跑的已满 4 个） | `.run`（灰色点） | 灰 |
| `done` | 完成 | `.done` | 浅灰 |
| `cancelled` | 已取消 | `.done`（文字“已取消”） | 灰 |
| `error` | 出错，看 `note` | `.error` | 红 |
| `idle` | 建了还没开始（几乎看不到） | `.done` | — |

建议 Swift 里 `TaskKind` 从 4 个扩成 6 个（加 `queued`、`cancelled`），或者按上表合并。**未知的值按 `.run` 处理**，以后加新状态不会崩。

### 1.2 任务摘要 `TaskSummary`（列表里的一行）

```json
{
  "id": "a1b2c3d4e5f6",
  "title": "Q3 流失分析",
  "status": "waiting",
  "note": "",
  "now": "跑命令 python send_mail.py（等你放行）",
  "created": 1790000000.0,
  "updated": 1790000230.5,
  "waiting": 1,
  "workdir": "/Users/kiwi/Documents/Q3"
}
```

| 字段 | 说明 | 对应 Keygent |
|---|---|---|
| `title` | 任务名（用户可改） | `TaskItem.name` |
| `now` | 一行状态：在跑的显示当前步骤，在等的显示等什么，结束了显示结论第一行 | `TaskItem.state` |
| `note` | 只有 `error` 才有：“预算用完，已收尾”“卡住了，已停下”“模型拒绝了这个请求”“输出被截断”，或失败原因第一行 | 出错时代替 `now` 显示 |
| `updated` | 最后有动静的时间 | `TaskItem.when` |
| `waiting` | 这个任务里在等你的有几件 | 列表上的小角标 |

### 1.3 步骤 `Step`（任务窗口的“过程”、详情的时间线）

```json
{
  "kind": "step",
  "seq": 12,
  "ts": 1790000130.2,
  "title": "读 客户名单.csv 第 1–200 行",
  "text": "{\"path\": \"客户名单.csv\", \"offset\": 1, \"limit\": 200}",
  "tool": "read_file",
  "out": "客户ID,地区,最近登录,资产等级\nC0001,华北,2026-06-02,B …",
  "why": "先看一下名单里有哪些字段。",
  "status": "ok"
}
```

| 字段 | 说明 | 对应 `ProcStep` |
|---|---|---|
| `kind` | `you` / `agent` / `step` | `kind` |
| `seq` | 账本序号，同一任务里唯一且递增，**可以当 SwiftUI 的 id** | （替代 `UUID()`） |
| `ts` | 时间 | `time`（前端换算成 “0:05”） |
| `title` | 列表里那一行。`you` 是“你：…”，`agent` 是“Agent：…”，`step` 是人能读的一句话 | `title` |
| `text` | `you` / `agent` 是完整原话；`step` 是调用参数（JSON 字符串） | `text` |
| `tool` | 工具名（`read_file`、`bash`、`mcp__fs__read_text_file`…），非工具步骤为空 | `tool` |
| `out` | 输出摘要，最多 300 字 | `out` |
| `why` | 模型发起这次调用前说的话 | `why` |
| `status` | 见下表 | （新增） |

`status` 的值：

| 值 | 含义 | 建议显示 |
|---|---|---|
| `ok` | 正常完成 | 默认样式 |
| `running` | 正在执行 | 转圈 / “正在：…” |
| `waiting` | 停在这里等你放行 | 琥珀 |
| `denied` | 被拒绝了（你拒绝的，或者规则拦下的） | 删除线 / 灰 |
| `cancelled` | 取消了 | 灰 |
| `interrupted` | 执行中途服务重启 | 灰 + “中断” |
| `error` | 工具或模型出错 | 红 |
| `note` | 不是工具调用，是一条说明：上下文压缩、记住了 N 条、Weaver 提醒、被触发 | 小字 / 灰 |

`MockData` 里 `tool` 写的是 `read_file("客户名单.xlsx")` 这种带参数的形式；真实数据里 `tool` 只有名字，参数在 `text`，人能读的说法在 `title`。

### 1.4 等待 `Wait`（放行区、“等你的事”）

三种：审批、卡住了、外部输入待确认。

**审批**（要执行一个需要你同意的操作）：
```json
{
  "id": "w-7f3a",
  "task": "a1b2c3d4e5f6",
  "task_title": "Q3 流失分析",
  "seq": 31,
  "ts": 1790000228.0,
  "kind": "approval",
  "title": "跑命令 python send_mail.py --to 流失客户.csv",
  "body": "这条命令会访问网络，不在自动放行的范围里。",
  "choices": ["allow", "deny", "always", "edit"],
  "call": {
    "id": "call_9",
    "name": "bash",
    "args": {"command": "python send_mail.py --to 流失客户.csv"}
  }
}
```

**卡住了**（Weaver 发现自己在原地打转，停下来问你）：
```json
{
  "id": "w-8c21",
  "task": "a1b2c3d4e5f6",
  "task_title": "竞品费率周报",
  "seq": 58,
  "ts": 1790000400.0,
  "kind": "stuck",
  "title": "它好像卡住了",
  "body": "连续 5 次用相同参数调用 fetch，结果都一样。",
  "choices": ["continue", "stop", "hint"]
}
```

**外部输入待确认**（不是你发的输入，比如以后的定时触发，要你确认要不要接）：
```json
{
  "id": "w-9d02",
  "task": "…", "task_title": "…", "seq": 3, "ts": 1790000001.0,
  "kind": "input",
  "title": "有一条外部输入待确认",
  "body": "（那条输入的原文）",
  "choices": ["allow", "deny"]
}
```

**问你**（Weaver 用 `ask_user` 问你一个问题，等你回答再接着做，v1.6）：
```json
{
  "id": "w-a1c4",
  "task": "…", "task_title": "Q3 流失分析", "seq": 40, "ts": 1790000300.0,
  "kind": "question",
  "title": "这次要先看华北还是全国？",
  "body": "",
  "options": ["只看华北", "全国一起看"],
  "choices": ["answer", "skip"]
}
```
`title` 就是问题；`options` 是可选的答案（0 个或 2~4 个，单选），选了哪个就把它的文字作为回答发回来，也可以自己写。

`choices` 决定显示哪些按钮：

| 值 | 按钮 | 回答时发送 |
|---|---|---|
| `allow` | 放行 | `{"decision": "allow"}` |
| `deny` | 不行 / 先不 | `{"decision": "deny", "note": "可选，告诉它为什么"}` |
| `always` | 总是允许（这类命令以后不再问） | `{"decision": "always"}` |
| `edit` | 改一下再放行 | `{"decision": "allow", "args": {改过的参数}}` |
| `continue` | 继续 | `{"decision": "continue"}` |
| `stop` | 停下 | `{"decision": "stop"}` |
| `hint` | 给个提示 | `{"decision": "hint", "note": "试试换个网址"}` |
| `answer` | 发给它（问你） | `{"decision": "answer", "note": "只看华北"}`，`note` 必填 |
| `skip` | 你自己定（问你） | `{"decision": "skip"}`：它按自己的判断继续，并在结论里写明假设 |

对应 Keygent 的 `Gate`：`title` → `Gate.title`，`body` → `Gate.body`，`sub` 由前端按 `kind` 写死（审批：“停下来等你”；卡住：“它在原地打转”），`approve` / `hold` 就是 `allow` / `deny` 两个按钮的文字。

**“改一下”怎么做**：把 `call.args` 显示成可编辑的表单（`bash` 就是一个 `command` 文本框，其它工具按 key 逐个显示），改完把**整个** `args` 发回来。Weaver 会用改过的参数执行，并告诉模型“用户把参数改成了：…”。

---


**MCP 服务器问你**（v1.9，`kind: "elicit"`）：工具调用中途服务器要信息（design/mcp2.md 第二节）。
```json
{"id": "…", "kind": "elicit", "title": "github 问你：部署到哪个环境？", "body": "github 在要信息；不要在这里填密码",
 "choices": ["accept", "decline", "cancel"], "server": "github", "mode": "form", "url": "",
 "fields": [{"name": "env", "title": "Env", "description": "", "type": "enum", "required": true,
             "options": ["staging", "prod"], "default": null}]}
```
`type` 是 `string` / `number` / `integer` / `boolean` / `enum`；`mode: "url"` 时没有字段，`url` 是要你打开的网址。回答：`POST /v1/waits/{id}` `{"decision": "accept", "values": {"env": "staging"}}`（按类型和必填项校验，不对回 400）/ `decline` / `cancel`。填的值只交给服务器，不进模型的上下文。等你回答时调用不计超时；任务被取消时这件作废。

**MCP 服务器借用模型**（v1.9）：普通审批，`call.name` 是 `"sampling"`、`call.args` 是 `{"server"}`，标题“X 想借用模型”，`body` 是它要发给模型的内容（前 500 字）。`always` = 以后这个任务里不再问这个服务器。不要放进“全部放行”。

## 2. 接口

### 2.1 胶囊：`GET /v1/status`

```json
{
  "running": 2,
  "queued": 0,
  "waiting": 3,
  "first_wait": {"task": "a1b2c3d4e5f6", "task_title": "Q3 流失分析", "title": "跑命令 python send_mail.py …", "kind": "approval"},
  "error": {"task": "f0e1d2c3b4a5", "task_title": "竞品费率周报", "note": "RuntimeError: HTTP 500"},
  "version": "0.1"
}
```

胶囊状态怎么选（优先级从高到低）：

| 条件 | `CapsuleState` | 文字 |
|---|---|---|
| `waiting > 0` | `.waiting` | `first_wait.task_title · 等你放行`，`waiting > 1` 显示 +N |
| `error != null` | `.error` | `error.task_title · 出错了` |
| `running > 0` | `.running` | `N 个在跑` |
| 否则 | `.idle` | — |

`error` 只给“出错了、你还没打开看过”的任务（打开过详情就算看过），没有就是 `null`。

### 2.2 任务列表：`GET /v1/tasks`

```json
{
  "tasks": [
    {"id": "a1b2c3d4e5f6", "title": "Q3 流失分析", "status": "waiting", "note": "",
     "now": "跑命令 python send_mail.py（等你放行）", "created": 1790000000.0,
     "updated": 1790000230.5, "waiting": 1, "workdir": "/Users/kiwi/Documents/Q3"},
    {"id": "f0e1d2c3b4a5", "title": "竞品费率周报", "status": "running", "note": "",
     "now": "跑命令 curl -s https://…", "created": 1789999280.0,
     "updated": 1790000229.0, "waiting": 0, "workdir": "/Users/kiwi/Weaver/scratch/f0e1d2c3b4a5"},
    {"id": "0a9b8c7d6e5f", "title": "合规口径修订", "status": "done", "note": "",
     "now": "已按新口径改完 3 处定义，见 口径_v3.md", "created": 1789990000.0,
     "updated": 1789996400.0, "waiting": 0, "workdir": "/Users/kiwi/Documents/合规"}
  ]
}
```

按 `updated` 从新到旧。不分页（本机任务数不会大到需要分页；归档的不在里面）。

### 2.3 新建任务：`POST /v1/tasks`

请求：
```json
{
  "text": "分析一下 Q3 为什么流失，名单在附件里",
  "workdir": "/Users/kiwi/Documents/Q3",
  "attachments": ["b-5e6f7a"]
}
```
- `text` 必填。
- `workdir` 可选；不给就用一个新的空目录 `~/Weaver/scratch/<任务 id>`。
- `attachments` 可选，是 2.9 上传后拿到的 id。

返回 `201`，内容是这个任务的 `TaskSummary`（状态通常是 `running`，满了是 `queued`）。

### 2.4 任务详情：`GET /v1/tasks/{id}`

```json
{
  "task": { …TaskSummary… },
  "steps": [
    {"kind": "you", "seq": 2, "ts": 1790000000.1, "title": "你：分析一下 Q3 为什么流失，名单在附件里",
     "text": "分析一下 Q3 为什么流失，名单在附件里", "files": ["客户名单.csv"], "tool": "", "out": "", "why": "", "status": "ok"},
    {"kind": "agent", "seq": 6, "ts": 1790000004.0, "title": "Agent：我先看一下名单里有哪些字段。",
     "text": "我先看一下名单里有哪些字段。", "tool": "", "out": "", "why": "", "status": "ok"},
    {"kind": "step", "seq": 6, "ts": 1790000004.0, "title": "读 客户名单.csv 第 1–200 行",
     "text": "{\"path\": \"客户名单.csv\", \"offset\": 1, \"limit\": 200}", "tool": "read_file",
     "out": "客户ID,地区,最近登录,资产等级 …", "why": "我先看一下名单里有哪些字段。", "status": "ok"},
    {"kind": "step", "seq": 30, "ts": 1790000228.0, "title": "跑命令 python send_mail.py --to 流失客户.csv",
     "text": "{\"command\": \"python send_mail.py --to 流失客户.csv\"}", "tool": "bash",
     "out": "", "why": "挽留邮件写好了，现在发出去。", "status": "waiting"}
  ],
  "waiting": [ …Wait，同 1.4… ],
  "final": "",
  "usage": {"tokens": 48210, "budget": 1000000, "steps": 9, "max_steps": 200}
}
```

- 注意：同一条模型回复里的文字和工具调用 `seq` 相同（`agent` 和后面的 `step`）。当 SwiftUI id 时用 `"\(seq)-\(index)"`，或者直接用数组下标。
- `final`：最后一轮的结论全文（Markdown），没结束就是空。任务窗口的“结果”区显示它。
- `usage.tokens` 是折算后的用量（缓存命中按十分之一算）。

`MockData` 里的表格结果（`rows`、`columns`）后端暂时没有对应：Weaver 的结论是 Markdown，前端先用 Markdown 渲染 `final`。以后需要结构化结果再加。

### 2.5 改标题：`PATCH /v1/tasks/{id}`

```json
{"title": "Q3 流失分析（华北）"}
```
返回新的 `TaskSummary`。

### 2.6 插话 / 追问：`POST /v1/tasks/{id}/input`

```json
{"text": "华北那个再细看一下", "attachments": []}
```
- 在跑的时候发：它在下一步之前看到这句话。
- 结束以后发：开启新的一轮，接着之前的上下文。

返回 `202`，内容是 `TaskSummary`。

### 2.7 取消：`POST /v1/tasks/{id}/cancel`

不需要请求体。取消当前这一轮：正在跑的工具会被打断，等你的事一起作废，状态变成 `cancelled`。之后还能用 2.6 接着说。返回 `202` + `TaskSummary`。

### 2.8 归档：`DELETE /v1/tasks/{id}`

不真删，移到 `~/.weaver/tasks/.archive/`。在跑的先取消，它启动的后台命令一起结束。返回 `204`。

### 2.9 上传附件：`POST /v1/blobs`

请求体是文件原始字节（不是 JSON），头部：
```
Content-Type: text/csv
X-Filename: 客户名单.csv        （URL 编码：%E5%AE%A2…）
```
返回 `201`：
```json
{"id": "b-5e6f7a", "name": "客户名单.csv", "mime": "text/csv", "size": 238114}
```
剪贴板文字也走这个（`Content-Type: text/plain`，`X-Filename: 剪贴板.txt`）。附件在 24 小时内没被任何任务用到就清掉。

### 2.10 所有在等的事：`GET /v1/waits`

```json
{"waits": [ …Wait，同 1.4，按 ts 从旧到新… ]}
```

### 2.11 回答一件等待：`POST /v1/waits/{wait_id}`

```json
{"decision": "allow"}
{"decision": "deny", "note": "先别发，我再看看名单"}
{"decision": "always"}
{"decision": "allow", "args": {"command": "python send_mail.py --to 流失客户.csv --dry-run"}}
{"decision": "hint", "note": "那个网站要加 ?page=2"}
```
- `decision` 必须在这件等待的 `choices` 里（`edit` 是用 `allow` + `args` 表示），否则 `400`。
- 已经被别处回答过 → `409`，前端直接刷新掉这一项。
- 成功返回 `200` + 这个任务的 `TaskSummary`。

---

### 2.12 设置页：`/v1/settings/…`（v1.7）

设计见 [settings.md](settings.md)。只管用户级配置（`~/.weaver/.env`、`~/.weaver/mcp.json`、`~/.weaver/skills/`）。出错同第 0 节的格式，`message` 原样显示给人看。

**模型**

```
GET /v1/settings/model
→ {"url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-4.5", "key": "••••a1b2",
   "advanced": {"context_window": "", "compact_at": "", "max_running": "6", "mcp_timeout": ""},
   "defaults": {"context_window": "自动（按模型）", "compact_at": "自动", "max_running": "4", "mcp_timeout": "10"}}

PUT /v1/settings/model
{"url": "…", "model": "…", "key": "sk-…（不填 = 沿用已保存的，前提是同一家）", "advanced": {"max_running": "6", "compact_at": ""}}
→ 200 {"restart": true}     // 前端收到后重启 weaverd；advanced 里给 "" = 删掉这一项、用默认
```

`key` 没保存过时是 `null`。400 的情况：地址不是 http/https、模型名空、远程地址没 key、高级项不是正整数。

**MCP**

```
GET /v1/settings/mcp
→ {"servers": [{"name": "github", "config": {…原样…}, "transport": "http",
                "status": "connected", "error": "", "tools": ["get_issue", …]}],
   "problem": null}
```

`status`：`connected`（含闲置被关、工具清单还在的）/ `connecting` / `failed`（`error` 是原因，比如“服务器拒绝了（HTTP 401）：令牌不对、过期或没有权限”“找不到命令 npx”）/ `idle`（还没连过；`error` 可能写着“已停用”或“weaverd 没装 MCP SDK”）/ `invalid`（配置本身有问题，比如“环境变量没设置：GITHUB_TOKEN”）。`mcp.json` 不是合法 JSON 时 `servers` 为空、`problem` 是原因，这时所有写操作回 409。

| 方法 | 路径 | 请求 | 返回 |
|---|---|---|---|
| POST | `/v1/settings/mcp/parse` | `{"text": "粘贴的内容"}` | `{"servers": {名字: 配置}, "conflicts": ["已存在的名字"]}`（只解析不保存；认 `{"mcpServers": …}`、`{名字: 配置}`、单个服务器） |
| POST | `/v1/settings/mcp` | `{"servers": {名字: 配置}, "overwrite": false}` | 201 `{"added": [名字]}`；同名且没 overwrite → 409 |
| PUT | `/v1/settings/mcp/{name}` | `{"config": {…}, "rename": "新名字（可选）"}` | `{"name": 最终名字}` |
| DELETE | `/v1/settings/mcp/{name}` | | 204（归档进 `mcp-archive.json`） |
| POST | `/v1/settings/mcp/{name}/reconnect` | | 204（不等连上；之后轮询列表） |
| GET | `/v1/settings/mcp/presets` | | `{"presets": [{"id", "name", "title", "description", "config", "needs": [{"id": "GITHUB_TOKEN", "kind": "env", "label": "GitHub 令牌", "url": "…"}]}]}`；`kind: "arg"` 表示填的值追加到 `args`（比如文件服务器的目录） |
| POST | `/v1/settings/mcp/presets/{id}` | `{"values": {"GITHUB_TOKEN": "…"}}` | 201 `{"added": [名字]}`；缺了要填的 → 400 “要填：GitHub 令牌” |
| GET | `/v1/settings/mcp/import` | | `{"sources": [{"from": "Claude Code" \| "Claude 桌面版", "path", "servers": {名字: 配置}}]}`；只列有服务器的来源，导入用 `POST /v1/settings/mcp` |

加、改之后服务器在后台开始连，前端每 2 秒 `GET /v1/settings/mcp` 看状态。

**登录**（v1.8，设计见 [mcp2.md](mcp2.md) 第一节）：`status` 多两种——`needs_login`（要登录；`error` 是原因，上次登录失败时写着为什么）、`logging_in`（登录进行中，多一个 `login` 字段，见下）。

`POST /v1/settings/mcp/{name}/login` → 三种之一：

```
{"kind": "done", "via": "gh"}                    // GitHub 且本机 gh 已登录：配置改成 "auth": "gh"，马上重连
{"kind": "browser", "url": "https://…/authorize?…"}   // weaverd 已经用浏览器打开授权页；url 给“再开一次”用
{"kind": "device", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device", "expires_in": 900}
```

登录在后台进行（最多 10 分钟），列表里的 `login` 就是上面这个对象；完成后变 `connecting` → `connected`，失败回到 `needs_login`。再调一次 login 会取消上一次。400：本地服务器、没装 SDK、这个服务器不支持自动登录（`message` 写着怎么办）。令牌存在 `~/.weaver/mcp-tokens.json`，接口里不返回。

常用服务器里的 GitHub 不再要求填令牌（`needs` 为空，多一个 `"login": "github"`）：本机 gh 已登录时加入后直接能用；否则加入后是 `needs_login`，前端可以紧接着调 login。

**Skills**

| 方法 | 路径 | 请求 | 返回 |
|---|---|---|---|
| GET | `/v1/settings/skills` | | `{"skills": [{"name", "description", "source": "weaver" \| "claude" \| "builtin", "editable", "path", "shadowed"?}], "problems": [{"path", "message"}]}`；`claude` 和 `builtin`（内置）只读；`shadowed: true` = 被前面同名的盖住（优先级 Weaver > Claude Code > 内置；新建同名的 Weaver skill 可以盖过内置的） |
| GET | `/v1/settings/skills/{name}` | | `{"name", "text": "SKILL.md 全文", "editable", "files": ["scripts/run.sh", …]}` |
| POST | `/v1/settings/skills` | `{"text": "SKILL.md 全文"}` | 201 `{"name"}`（名字取自开头的 name） |
| PUT | `/v1/settings/skills/{name}` | `{"text"}` | `{"name": 最终名字}`（name 改了目录跟着改名） |
| DELETE | `/v1/settings/skills/{name}` | | 204（归档到 `skills/.archive/`） |

400：没有开头那段、名字不合法（小写字母数字和 -，最长 64）、description 空、改 Claude Code 的 skill；409：重名。

### 2.13 改了哪些文件、diff、撤销（v1.9）

任务详情多两个字段：

- `todos`：当前 todo 清单 `[{"content", "status": "pending"|"in_progress"|"completed"|"cancelled"}]`（没有就是空数组）。
- `changes`：这个任务（含它的子 Agent）改过的文件，按第一次改的先后：
  `[{"path": "/abs/tests/conftest.py", "rel": "tests/conftest.py", "added": 9, "removed": 1, "created": false, "undone": false, "can_undo": true, "why": ""}]`。
  `can_undo: false` 时 `why` 写原因（“之后又被改过”“文件被删了”）。

步骤（1.3）多三个可选字段：`diff`（`edit_file` / `write_file` 那一步，从参数算的统一 diff，最多 200 行）、`sub`（`task` 那一步：子账本的会话名）、`images`（工具返回的图片 `[{"id", "mime"}]`，用 2.18 取）。审批（1.4）里改文件的多一个 `diff`（按磁盘现状和参数算）。`jobs` 项多一个 `sub`（后台子 Agent 的子账本）。

- `GET /v1/tasks/{id}/changes?path=<路径>` → `{"path", "rel", "diff"}`：这个文件从任务第一次改之前到最后一次改之后的统一 diff（上下文 3 行，最多 400 行）。没改过这个文件 → 404。
- `POST /v1/tasks/{id}/undo` `{"path"}` → `{"changes": [...]}`：把这个文件恢复到任务改它之前（倒着一次次撤销）。之后又被别人改过 → 409，文件不动。撤销后账本里记一条背景输入（不开启新的一轮），模型下一轮知道；步骤里多一条 note“你撤销了 X 的改动”。

### 2.14 子 Agent 的过程（v1.9）

`GET /v1/tasks/{id}/agents/{sub}` → `{"sub", "title", "status", "steps": [Step], "final", "usage": {"tokens", "steps"}}`。`sub` 来自步骤或 `jobs` 的 `sub`、搜索结果的 `sub`。归档的任务加 `?archived=1`。子账本的事件不单独推，在跑时客户端自己隔几秒拉一次。

### 2.15 归档的任务（v1.9）

- `GET /v1/tasks?archived=1` → `{"tasks": [TaskSummary + "archived": true, "archived_at"]}`，最近归档的在前。
- `GET /v1/tasks/{id}?archived=1` → 只读详情（同 2.4，`waiting` 为空）。`GET /v1/tasks/{id}/changes?…&archived=1` 同理。
- `POST /v1/tasks/{id}/restore` → TaskSummary：搬回来，之后就是普通任务。没有这个归档 → 404。

### 2.16 设置 › 权限、记忆（v1.9）

- `GET /v1/settings/permissions` → `{"rules": [{"task", "task_title", "key", "kind": "bash"|"mcp", "ts"}], "projects": [{"root", "what": ["skill", "子 Agent 类型", "MCP 服务器"], "state": "trusted"|"denied"}], "builtin": ["…"]}`。借用模型的“总是允许”的 key 是 `sampling:<服务器>`。
- `DELETE /v1/settings/permissions/rules` `{"task", "key"}` → 204：撤销一条“总是允许”（之后同样的操作重新问；再点一次“总是允许”就又生效）。
- `DELETE /v1/settings/permissions/projects` `{"root"}` → 204：不再信任（或不再拒绝）这个项目，下一轮开始时重新问。
- `GET /v1/settings/memory` → `{"scopes": [{"scope": "user"|"project", "root", "label", "items": [{"name", "type", "description", "path"}]}], "template"}`。
- `GET|PUT|DELETE /v1/settings/memory/{name}?scope=user|project&root=<项目>`：读（`{"name", "text"}`，整个文件）、保存（`{"text"}`，frontmatter 里 name 改了就是改名）、删除（归档）。`POST /v1/settings/memory` `{"scope", "root", "text"}` 新建。像密钥、像注入指令的内容 → 400。

### 2.17 MCP 提示词（v1.9，design/mcp2.md 第四节）

- `GET /v1/prompts?workdir=<目录>` → `{"prompts": [{"server", "name", "title", "description", "arguments": [{"name", "description", "required"}]}]}`：用户级 + 这个项目已信任的服务器。
- `POST /v1/tasks` 可以用 `{"prompt": {"server", "name", "arguments": {…}}, "workdir"}` 代替 `text`：weaverd 拿到 prompt 的消息作为第一句话，标题是 `/<server>:<name>`。

### 2.18 取图片：`GET /v1/blobs/{sha256}?mime=image/png`（v1.9）

返回原始字节（步骤 `images` 里的 `id`）。

## 3. 事件流：`GET /v1/events?after=<cursor>`

标准 SSE（`text/event-stream`）。一条连接收所有任务的事件。

```
id: 1042
event: task
data: {"task": "a1b2c3d4e5f6", "summary": {…TaskSummary…}}

id: 1043
event: wait
data: {"task": "a1b2c3d4e5f6", "wait": {…Wait…}}

id: 1044
event: step
data: {"task": "a1b2c3d4e5f6", "step": {…Step…}}

event: delta
data: {"task": "f0e1d2c3b4a5", "text": "华北的流失主要"}

event: ping
data: {}
```

| 事件 | 什么时候 | 前端做什么 |
|---|---|---|
| `task` | 任务状态、`now`、`waiting` 数有变化；新建、改名 | 替换列表里那一行，重算胶囊（或者再 `GET /v1/status`） |
| `wait` | 出现一件新的等待 | 加进“等你的事”；App 在后台时弹系统通知 |
| `wait_closed` | 某件等待被回答 / 作废了：`{"task", "wait_id"}` | 从“等你的事”里删掉 |
| `step` | 新步骤，或者已有步骤的状态 / 输出变了（按 `seq` + `title` 认，同一个就替换） | 打开着这个任务时更新过程列表 |
| `delta` | 模型正在输出的文字片段 | 打开着这个任务时显示“正在写…”，**可以忽略** |
| `archived` | 任务被归档：`{"task"}` | 从列表删掉 |
| `ping` | 每 15 秒一次 | 不用管，只是保持连接 |

**断线重连**：除了 `delta` 和 `ping`，每条事件都有 `id`（全局递增的游标）。断线后带上最后收到的 id 重连（`?after=1044`，或者标准的 `Last-Event-ID` 头），服务端把错过的补发。`delta` 不补，丢了无妨。

**游标太旧**：服务只保留最近 1 万条，`after` 太旧时先发一条：
```
event: reset
data: {}
```
收到就重新 `GET /v1/tasks`、`GET /v1/waits` 拿全量。

**推荐用法**：
1. 启动：`GET /v1/status`、`GET /v1/tasks`、`GET /v1/waits`，然后连事件流。
2. 打开某个任务：`GET /v1/tasks/{id}`，之后靠 `step` 事件增量更新。
3. 事件流断了：1 秒后重连，失败就逐步拉长到 30 秒。

前端如果处理不了太快的事件也没关系：服务端给每个连接一个有上限的队列，满了会主动断开，前端重连补齐即可。

---

## 4. Swift 对照（可以直接用）

```swift
struct TaskSummary: Codable, Identifiable, Equatable {
    let id: String
    var title: String
    var status: String          // running / waiting / queued / done / cancelled / error / idle
    var note: String
    var now: String
    var created: Double
    var updated: Double
    var waiting: Int
    var workdir: String
}

struct Step: Codable, Equatable {
    enum Kind: String, Codable { case you, agent, step }
    var kind: Kind
    var seq: Int
    var ts: Double
    var title: String
    var text: String
    var tool: String
    var out: String
    var why: String
    var status: String          // ok / running / waiting / denied / cancelled / interrupted / error / note
}

struct WaitCall: Codable, Equatable {
    var id: String
    var name: String
    var args: [String: JSONValue]
}

struct WaitItem: Codable, Identifiable, Equatable {
    let id: String
    var task: String
    var taskTitle: String
    var seq: Int
    var ts: Double
    var kind: String            // approval / stuck / input
    var title: String
    var body: String
    var choices: [String]
    var call: WaitCall?
}

struct TaskDetail: Codable {
    var task: TaskSummary
    var steps: [Step]
    var waiting: [WaitItem]
    var final: String
    var usage: Usage
    struct Usage: Codable { var tokens, budget, steps, maxSteps: Int }
}

struct StatusSummary: Codable {
    struct Ref: Codable { var task: String; var taskTitle: String; var title: String? ; var note: String? }
    var running: Int
    var queued: Int
    var waiting: Int
    var firstWait: Ref?
    var error: Ref?
}

/// 工具参数是任意 JSON
enum JSONValue: Codable, Equatable {
    case string(String), number(Double), bool(Bool), array([JSONValue]), object([String: JSONValue]), null
    // init(from:) / encode(to:) 按值类型逐个尝试，略
}
```

字段名是蛇形（`task_title`、`first_wait`、`max_steps`），解码时用：
```swift
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
```

和现有假数据模型的对应：

| 现有 | 换成 |
|---|---|
| `TaskItem` | `TaskSummary`（`name`→`title`，`state`→`now`/`note`，`when`←`updated`，`kind`←`status`） |
| `ProcStep` | `Step`（`time`←`ts − created`，多一个 `status`） |
| `Gate` | `WaitItem`（`sub`、按钮文字前端按 `kind` 定） |
| `Mail`（等你的事） | `WaitItem`；邮件这类内容在 `call.args` 里 |
| `RunningRow` | `TaskSummary` 里 `status == running` 的那些 |
| `CapsuleState` | 由 `StatusSummary` 按 2.1 的规则算 |
| `TaskContent.rows` / `columns` | 暂无，先渲染 `TaskDetail.final`（Markdown） |

---

## 5. 假数据模式（联调用，计划中）

后端会提供 `weaverd --fake`：所有接口照常，但用假模型跑几个预设剧本（一个正常完成、一个停在审批、一个卡住、一个出错、一个在跑且持续推 `delta`），不花 token，前端可以对着它把每种状态都看一遍。在第 2、3 步做完之后提供。

---

## 变更记录

- 2026-10-03 v1.9：任务详情多 `todos`、`changes`；步骤多 `diff`、`sub`、`images`；审批多 `diff`；`jobs` 多 `sub`（2.13）。新接口：改动的 diff 和撤销（2.13）、子 Agent 的过程（2.14）、归档列表 / 只读详情 / 恢复（2.15）、设置 › 权限和记忆（2.16）、MCP 提示词和用 prompt 新建任务（2.17）、取图片（2.18）。新的等待类型 `elicit`，借用模型是 `call.name: "sampling"` 的审批（1.4 末尾）。`GET /v1/status` 的 `first_wait.kind` 可能是 `elicit`（胶囊按“问你”提醒）。一轮结束时，这一轮里没回答的等待（外部输入的确认除外）一律作废。

- 2026-10-02 v1.8：MCP 登录——`POST /v1/settings/mcp/{name}/login`；列表状态多 `needs_login`、`logging_in`（带 `login`）；常用服务器 GitHub 改成登录（2.12）。
- 2026-10-02 v1.7：设置页接口 `/v1/settings/…`（2.12）：模型（含高级项）、用户级 MCP 服务器（列表带连接状态、粘贴解析、常用服务器、从 Claude 导入）、用户级 skills（新建 / 编辑 / 改名 / 归档）。
- 2026-10-02 v1.6：新的等待类型 `kind: "question"`（见 1.4“问你”）：Weaver 用 `ask_user` 工具问你一个问题。多一个字段 `options`（可能为空），`choices: ["answer", "skip"]`；`answer` 必须带非空 `note`（否则 400），`skip` 不带。任务状态 `waiting`，`now` 是“问你：<问题>”。`GET /v1/status` 的 `first_wait` 多一个 `kind`（胶囊按它出文字：question 是“X · 问你”）。步骤里出现“问你：<问题>”（`tool: ask_user`，等回答时 `status: waiting`），有结论后 `status: ok`、`out` 是“你答：…”或“你让它自己定”。不要把 question 放进“全部放行”。设计见 `design/ask-user.md`。
- 2026-10-02 v1.5：`kind: "trust"` 的等待也包括项目配置里的 **MCP 服务器**（`.mcp.json`、`.weaver/mcp.json`）。标题按内容变：“信任项目 X 里的 skill、子 Agent 类型、MCP 服务器吗？”（只列有的那几样）；`body` 里写着每个服务器要运行的命令或连接的地址。其余规则不变（按项目算、拒绝对整个项目生效、内容变了才会再问、不挡着任务）。信任后**下一轮**开始时，这些服务器的工具才出现。任务刚交出去、一轮开始前（连 MCP 服务器时）状态是 `running`、`now` 是“准备开始”。Keygent 不用改。
- 2026-10-01 v1.4：
  - 新接口 `GET /v1/search?q=关键字&archived=0&limit=20` → `{"results": [{task, task_title, workdir, archived, sub, seq, ts, kind, excerpt}]}`。`sub` 不为空表示命中在子 Agent 的账本里（`seq` 是子账本的序号，不能直接跳到主任务的步骤）。
  - 新的等待类型 `kind: "trust"`：“信任项目 X 里的 skill 和子 Agent 类型吗？”，`choices: ["allow", "deny"]`，id 形如 `trust-<任务 id>`，多一个 `root`（项目路径）。**按项目算**：同一个项目只出现一件（挂在最早的那个任务上），拒绝对整个项目生效，项目里的这些文件内容变了才会再问。不挡着任务往下跑：任务状态可能是“在跑 / 完成”，但 `waiting` 计数里包含它。前端不要把它放进“全部放行”。
  - 子 Agent 升上来的等待：`kind` 还是 `approval` / `stuck`，多一个 `from_agent`（子 Agent 的任务名），`body` 末尾写着“（来自子 Agent：X）”。按钮和普通审批一样。
  - 任务详情多两个字段：`jobs`（后台命令和后台子 Agent：`{id, kind: shell/agent, title, status, started, ended}`）、`usage.extract_tokens`（记忆提取的用量）。
  - 步骤里会出现：“记住了 N 条 / 没有新的记忆 / 记忆提取失败”（`status: note`）、“派后台子 Agent：X”“派子 Agent：X（reviewer）”“派子 Agent：X（分叉）”“查记录 X（所有任务）”。
- 2026-10-01 v1.3：系统通知的分工——**连着事件流的 App 负责提醒，weaverd 就不再发 macOS 通知**；Keygent 没开时由 weaverd 兜底（新的等待、出错、跑了 1 分钟以上的任务完成）。命令行客户端连事件流时带 `X-Weaver-Client: cli`，不算 App。Keygent 不用改，连着就算。
- 2026-10-01 v1.2：“改一下再放行”已实现。新增几种会出现在步骤里的东西：后台命令（标题“跑命令 …（后台）”）、后台命令结束的通知（`status: note`，标题“后台命令 X（…）结束了…”）、改过参数的调用（标题后缀“（你改过参数）”）、被取消打断的命令（`status: cancelled`）。
- 2026-10-01 v1.1：后端已实现（`weaver/daemon/server.py`），除了“改一下再放行”（第 4 步之前发 `args` 会回 400）。补充：方法不对回 405；带附件时，`you` 步骤的 `text` 只有用户自己说的话，附件名放在 `files` 数组里（“附件已放在工作目录”这类说明只给模型，不出现在步骤里）。
- 2026-10-01 v1：初稿。字段来自已实现的 `weaver/daemon/{tasks,status,humanize}.py`。
