# Weaver MCP 设计

草案 v1 · 2026-10-01

## 一、要解决什么问题

MCP（Model Context Protocol）是接外部能力的通用插头：GitHub、数据库、浏览器、Sentry、Figma……别人写好的 MCP 服务器，接上就能用，不用我们一个个写工具。

main.md 第 19 课：MCP 提供五样东西——

| 能力 | 是什么 | 在 Agent 里变成什么 |
|---|---|---|
| tools | 可调用的函数 | 工具，名字一般是 `mcp__服务器__工具` |
| resources | 可读取的数据 | 列出 / 读取资源的工具 |
| prompts | 提示词模板 | 斜杠命令 |
| instructions | 服务器自带的使用说明 | 注入上下文 |
| elicitation | 服务器反过来向用户提问 | 中断等待用户回答 |

结论：**tools 人人支持，其余参差不齐**；**工具多了要延迟加载 + 搜索**；远程服务器要 OAuth；Pi 故意不做。

## 二、协议本身

MCP 是 JSON-RPC 2.0，两种传输：

- **stdio**：启动一个子进程，一行一条 JSON 消息。本地服务器（`npx …`、`uvx …`）都是这种。
- **Streamable HTTP**：POST 一条 JSON-RPC 请求，响应是 JSON 或一段 SSE 流。远程服务器用这种。

一次会话的流程：`initialize`（交换版本和能力，服务器可附 `instructions`）→ `notifications/initialized` → `tools/list`（可能分页）→ 按需 `tools/call`、`resources/list`、`resources/read`。

**自己写客户端，不引入官方 SDK**（和模型接入层同样的理由）：协议就是几种 JSON 消息，stdio 加 HTTP 估计 300 行左右；官方 Python SDK 依赖 anyio、pydantic、httpx 等一串包，项目到现在一直只用标准库；而且我们的 SSE 读取器、重试已经现成。

## 三、配置

```json
// .weaver/mcp.json（项目级）、~/.weaver/mcp.json（用户级）；也读项目根目录的 .mcp.json（兼容 Claude Code）
{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}
    },
    "fs": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": {"FOO": "bar"},
      "trust": "readonly"
    }
  }
}
```

- 格式和 Claude Code 的 `.mcp.json` 一样，现成的配置能直接用。
- `${VAR}` 从环境变量展开，密钥不用写进文件。
- **项目级配置要先信任一次**：克隆来的仓库里的 `.mcp.json` 可以写任意命令，等于让别人在你机器上跑程序（Claude Code 也会先问）。第一次见到某个项目的某个服务器（按“项目路径 + 配置内容的哈希”认），启动前问一次，同意后记到 `~/.weaver/trusted-mcp.json`；配置改了要重新问。用户级配置是你自己写的，不用问。

## 四、在 Weaver 里变成什么

### 1. 工具

- 名字：`mcp__<服务器>__<工具>`，只保留字母、数字、`_`、`-`，超过 64 个字符就截短并加短哈希（Cline 的做法）。
- 参数：直接用服务器给的 `inputSchema`。
- 说明：`[MCP 服务器 github] ` + 服务器给的说明。
- 结果：`content` 里的文字拼起来；`structuredContent` 转成 JSON 文字；图片先写“[图片 …，未展开]”（我们的工具结果目前只支持文字）；`isError: true` 就是出错结果。
- **输出上限**：25k token（约 75KB，和 Claude Code 一样），超了截断并说明。
- 结果照样经过执行者的脱敏（redaction.md）。

### 2. 工具太多：两种模式

工具清单在请求的**最前面**，会话中途增删工具会让整个缓存前缀失效（cache.md 的铁律）。所以：

| 情况 | 做法 |
|---|---|
| MCP 工具总数 ≤ 30 | **直接列成工具**，会话开始时定下来，整个会话不变 |
| > 30 | **延迟模式**：只给两个固定工具——`mcp_search(query)` 按关键字搜工具，返回名字、说明和参数 schema；`mcp_call(server, tool, arguments)` 调用。所有 MCP 服务器的“名字 + 一句话说明”作为背景信息放进上下文 |

延迟模式和 Claude Code 的做法不一样：它把搜到的工具**加进工具清单**，我们用固定的 `mcp_call`。代价是参数不能被 API 按 schema 校验（由服务器自己校验、报错回来）；好处是工具清单永远不变，缓存不断。

### 3. 服务器说明（instructions）

`initialize` 返回的 `instructions` 作为一个背景提供者（`kind="mcp"`，environment.md 的机制）放进上下文，变了才在末尾追加，不碰 system prompt。连不上的服务器也在这里说明（“github：连接失败，原因 …”），模型就不会去调用它。

### 4. 资源（resources）

两个固定工具（Claude Code 的做法）：`mcp_list_resources(server?)`、`mcp_read_resource(server, uri)`。只在至少有一个服务器声明了 resources 能力时才加。

### 5. 暂不做

prompts（等有了交互式界面再做成斜杠命令）、elicitation（第一版收到就回复“拒绝”）、sampling（服务器反过来借用我们的模型）、OAuth（第一版用请求头里的 token）、Weaver 自己当 MCP 服务器。

## 五、权限、并行、沙箱

- **权限**：MCP 工具干什么我们不知道，**默认问人**；服务器在工具上标了 `readOnlyHint: true` 的，放行（Codex、Claude Code 也看这个标记）；配置里写 `"trust": "readonly"` 表示信任这个服务器的只读标记，`"trust": "all"` 表示这个服务器的工具全部放行。审批时同样可以选“总是允许”（按工具名记进账本）。
- **并行**：标了 `readOnlyHint` 的可以并行，其余独占。
- **子 Agent**：explore 只拿到只读的 MCP 工具，general 拿到全部。
- **沙箱**：stdio 服务器第一版**不放进沙箱**（它们常要联网、要写自己的缓存目录），靠“项目级要先信任”和“工具调用默认问人”两道关。启动服务器时环境变量同样去掉密钥（bash 的做法），只传配置里明确写的 `env`。

## 六、生命周期

- 会话开始时并行连接所有已信任的服务器（每个最多等 10 秒），连不上的记下原因、不影响其他。
- 进程退出时关闭（stdio 发 EOF、等 2 秒、再杀进程）。
- 调用时服务器挂了：重连一次再试；还不行就作为出错结果交回模型。
- 工具清单在会话开始时定下来；服务器发来 `list_changed` 通知，下一个会话才生效（不在中途改工具清单，理由同上）。

## 七、代码结构

```
weaver/mcp/
  protocol.py     JSON-RPC 消息、initialize 握手、分页
  stdio.py        子进程传输
  http.py         Streamable HTTP 传输（复用 providers/sse.py）
  config.py       读配置、${VAR} 展开、项目级信任
  manager.py      连接所有服务器、汇总工具、调用、关闭
  tools.py        变成 Weaver 工具（直接模式 / 延迟模式、资源工具）
```

## 八、测试

1. 协议：用一个测试用的假 MCP 服务器（Python 脚本，走 stdio）完整跑握手、分页列工具、调用、出错、资源。
2. HTTP 传输：假的 HTTP 响应（JSON 和 SSE 两种）。
3. 配置：三处文件合并、`${VAR}` 展开、项目级信任（第一次问、配置改了重新问）。
4. 工具：名字规整和截短、结果转换（文字、结构化、图片、出错）、输出上限、脱敏。
5. 直接模式 / 延迟模式的切换；延迟模式下工具清单固定。
6. 权限：默认问人、`readOnlyHint` 放行、`trust` 配置；并行声明。
7. 生命周期：连不上的服务器写进背景信息；服务器挂了重连一次。
8. 真实验证：接官方的 filesystem 服务器（`npx @modelcontextprotocol/server-filesystem`），让模型通过它列目录、读文件。

---

## 补充：自己写 还是 用官方 SDK（2026-10-01）

查了本地 17 个参考仓库：大多数用官方 SDK（Codex 用 Rust 的 `rmcp`；Claude Code、OpenCode 用 TS SDK；OpenAI Agents SDK、Hermes、Deep Agents、DeerFlow 用 Python 的 `mcp`）；只有 Claw Code 自己写（Rust，MCP 相关文件合计约 5700 行）。

| | 官方 SDK | 自己写 |
|---|---|---|
| OAuth（远程服务器越来越需要） | 现成 | 要自己写，复杂（发现授权服务器、PKCE、动态注册、刷新） |
| 跟上协议更新 | SDK 跟（Hermes 注释：mcp 2.0 实现了 2026-07-28 修订版） | 自己追 |
| 边角情况（会话 id、断线续传、旧 SSE 传输、取消、进度、版本协商） | 都有 | 第一版只覆盖常用路径 |
| 以后让 Weaver 当 MCP 服务器 | FastMCP 几行 | 要另写 |
| 依赖 | 一串：pydantic、anyio、HTTP 客户端，还会间接带上 Starlette。Hermes 注释：Starlette 的一个 CVE（BadHost）就是经 mcp 间接引入的——第 21 课说的供应链风险 | 只用标准库 |
| 稳定性 | 变化快：mcp 2.0 把 HTTP 层从 `httpx` 换成了 `httpx2`，Hermes 要专门处理 | 自己掌控 |
| 编程模型 | 异步（anyio），我们的执行者是同步 + 线程，要多一层桥接 | 同步，直接接 |

**结论：混合做法**

- 先定一个客户端接口（连接、列工具、调用、读资源、关闭）——内核“端口与适配器”的思路。
- 第一版自己写 stdio + 普通 HTTP（请求头里带 token 认证），覆盖本地服务器和大部分远程服务器，估计 300~400 行，仍然只用标准库。
- 需要接要求 OAuth 的远程服务器时，把官方 SDK 作为**可选依赖**（`pip install weaver[mcp]`）接到同一个接口后面；不需要的人不用装，内核和工具层不改。

---

## 进度（2026-10-01）

**改为用官方 SDK**（`mcp` 2.2.0，可选依赖，装在项目的 `.venv` 里：`requirements-mcp.txt`）。所以第七节的代码结构简化为：

```
weaver/mcp/
  config.py     读三处配置、${VAR} 展开、项目级信任（不依赖 SDK）
  manager.py    后台线程跑事件循环；每个服务器一个“守连接”任务；同步调用、断线重连一次、关闭
  tools.py      变成 Weaver 工具（直接 / 延迟模式、资源工具）、权限规则、背景信息
```

协议、stdio、HTTP 都交给 SDK。实现时的要点：

- **anyio 的连接必须在同一个异步任务里打开和关闭**，所以每个服务器一个常驻任务：打开连接 → 等关闭信号 → 关闭；执行者线程用 `run_coroutine_threadsafe` 提交调用。
- stdio 服务器的报错输出写到 `.weaver/mcp-logs/<服务器>.log`，不刷到终端。
- 环境变量：SDK 只继承少数安全的变量（PATH、HOME 等）再加配置里写的，我们的 key 不会传给服务器。
- HTTP 认证：配置里的 `headers` 交给 SDK 的 HTTP 客户端（OAuth 以后再接 SDK 自带的）。
- 没装 SDK 时：读得到配置也只提示“请用 `.venv/bin/python -m weaver` 运行”，不影响其他功能。
- `WEAVER_MCP_TIMEOUT` 调连接超时（`npx` 第一次要下载包）。
- 关闭时要 `loop.close()`，否则事件循环内部的 socket 泄漏（测试里用“资源警告当错误”抓到的）。

测试见 `tests/test_mcp.py`：配置部分不需要 SDK；连接部分用 SDK 服务端写的假服务器（`tests/fixtures/fake_mcp_server.py`）跑真实的 stdio 连接，覆盖工具、出错、资源、背景信息、只读放行、explore 子集、脱敏、“总是允许”、服务器挂了重连、`trust: all`、35+ 个工具时的延迟模式、连不上的服务器。系统 Python（没装 SDK）下这些自动跳过。

真实验证：接官方的 filesystem 服务器（`npx @modelcontextprotocol/server-filesystem`），连上后注册了 14 个工具；让模型“只用 fs 的工具”列目录、读 todo.txt，它依次调用 `list_allowed_directories`、`list_directory`、`read_text_file`，结果正确。

---

## 九、常驻服务（weaverd）里的 MCP（2026-10-02）

命令行是“一个会话一个进程”，会话开始时连好、结束时关掉。weaverd 常驻、同时跑很多任务，任务可以在不同项目里、可以停很久再继续，所以换了做法。

### 1. 连接：所有任务共用（`weaver/mcp/pool.py` 的 `McpPool`）

- weaverd 里只有一个 `McpManager`（一个事件循环线程），服务器按 key 登记：**服务器名 + 配置哈希 + 工作目录**。配置相同、工作目录相同的只起一个。
- 工作目录：**用户级服务器用家目录**（所有任务共用一个进程；要指定就在配置里写 `cwd`）；**项目级服务器用项目目录**（同一个项目的任务共用）。
- 启动时先把用户级服务器连起来（不等），任务开始时多半已经连好。
- **闲置 10 分钟关掉**（`WEAVER_MCP_IDLE` 秒），工具清单留着，下次调用时再连。临时目录的任务很多，不关的话进程只增不减。
- **连不上的 60 秒内不再试**，免得每个任务开始都卡一次超时；真的调用时照样重连一次。
- 同一个服务器同时被几个任务要求连接：只连一次，大家一起等。
- 服务器日志：`~/.weaver/mcp-logs/<key>.log`。

### 2. 工具清单：每个任务一份（`TaskMcp`）

- **每一轮开始前**（`Runner.before_run`，在工作线程里，不卡 HTTP 接口）读一次配置、把用得上的服务器连上（最多等 `WEAVER_MCP_TIMEOUT` 秒）。
- 能用的服务器**没变就什么都不动**；变了才换工具清单和背景信息。会变的情况只有：第一次、信任了项目、配置改了、之前连不上的连上了。连接被闲置关掉、断开重连都不算变。工具清单变一次，缓存前缀断一次。
- `McpTools` 改成造出来那一刻的**快照**（连不上的原因、服务器说明都记下来），命令行也受益：服务器中途挂了，背景信息不再跟着变。
- 背景信息里还会说：哪些服务器配置有问题、哪些是项目级还没信任的。模型被问到时能说清为什么用不了。
- 背景信息是在用户这句话**之后**追加的（第一轮要先连上才知道说什么），内核不在乎顺序。

### 3. 项目级要先信任：并进“信任这个项目吗”

- `agents.ProjectTrust` 多管一样：项目里没信任的 MCP 服务器。App 里那件 `trust` 等待的标题变成“信任项目 X 里的 skill、子 Agent 类型、MCP 服务器吗？”，正文列出每个服务器要运行的命令。
- 信任记在 `~/.weaver/trusted-mcp.json`，和命令行共用（命令行确认过的，weaverd 里不再问，反过来也一样）。
- 信任后立刻在后台把它连起来，**下一轮**开始时工具出现。拒绝和 skill 一样按项目记，配置改了会再问。
- 没装 SDK 时不问（信任了也用不了），背景信息里说明“weaverd 没装 MCP SDK”。
- 临时目录的任务没有项目，只读用户级配置。

### 4. 其它

- 权限、只读并行、explore 子 Agent 只拿只读工具、“总是允许”、步骤标题：和命令行一样，`TaskMcp` 有同样的 `tools()` / `rule()` 接口。
- 任务列表：一轮开始前在连 MCP 时，状态是“在跑 · 准备开始”（之前会显示成“空闲”或上一轮的“完成”）。
- **Keygent.app 一定带 SDK**：`Keygent/scripts/fetch-python.sh` 下载内嵌 Python 时一起装上（`mcp` 2.2.0，内嵌 Python 从 55M 变成 93M）；构建阶段 `embed-weaverd.sh` 检查，没装就报错、不让打包。SDK 只在真的配了 MCP 服务器时才导入，没配的人不多占内存。

### 5. 测试与验证

`tests/test_mcp.py` 的 `Daemon`：信任后项目级工具下一轮出现、用户级连接跨任务共用、拒绝后不再问、闲置关掉后工具清单不变且调用时重连、连不上的写进背景信息且 60 秒内不重试。

真实验证（OpenRouter，用 `.venv` 起的独立 weaverd，临时 WEAVER_HOME）：用户级配了测试服务器，项目里 `.mcp.json` 配了官方 filesystem 服务器。
1. 在项目里交任务：模型用 fake 的 echo 回显成功，说明 fs“是项目配置里的服务器，用户还没确认信任”；等你的事里出现“信任项目 proj 里的 MCP 服务器吗？”。
2. 信任 → filesystem 服务器立刻在项目目录启动；追问“用 fs 读 todo.txt 的第二件事”→ 调用 `read_text_file`（只读，没问），回答“修自行车”。
3. 同一个项目再交一个任务：没有再问信任，进程 id 不变（共用连接）。
4. 停止 weaverd：MCP 子进程一起退出，日志没有 ERROR。
