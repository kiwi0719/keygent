# Weaver 设计文档总览

更新于 2026-10-01

Weaver 是一个按 `main.md`（开源 AI Agent 架构教程）一层层搭起来的编码 Agent。核心思路：**事件日志（账本）是唯一的事实，内核是纯函数，其余一切挂在内核外面**。同一个内核，本地能跑，以后也能搬到服务器上。

## 文档

| 文档 | 讲什么 | 状态 |
|---|---|---|
| [kernel.md](kernel.md) | 内核：账本、9 种事件、规则表、执行者、不变量 | 已实现（v6，和代码同步） |
| [providers.md](providers.md) | 模型接入：OpenAI / Anthropic 两种协议、流式、各家方言差异 | 已实现 |
| [cache.md](cache.md) | 提示缓存：前缀不变的铁律、断点、观测 | 已实现 |
| [compaction.md](compaction.md) | 上下文压缩：先裁剪、再摘要，原文不删 | 已实现 |
| [memory.md](memory.md) | 记忆：指令文件、两层记忆、会话开始冻结快照 | 已实现（第一版） |
| [environment.md](environment.md) | 环境信息：工作目录、平台、可用命令；通用背景输入机制 | 已实现 |
| [tools.md](tools.md) | 读文件与搜索：read_file、grep、find_files、随项目打包的 ripgrep | 已实现 |
| [write-tools.md](write-tools.md) | 写文件、改文件、跑命令；权限；macOS / Linux 沙箱；撤销 | 已实现 |
| [parallel-subagent.md](parallel-subagent.md) | 并行执行工具；子 Agent（explore / general）；缓存预热 | 已实现 |
| [todo-loop.md](todo-loop.md) | todo 清单；防死循环（分层：提醒 → 问人 / 停下）；预算提醒 | 已实现 |
| [redaction.md](redaction.md) | 脱敏：gitleaks 精选规则 + 环境变量里的已知值；进账本前脱敏；防写回占位符 | 已实现 |
| [mcp.md](mcp.md) | MCP：官方 SDK（可选依赖）、工具 / 资源 / 说明、工具多时的延迟模式、项目级信任；第九节：接进 weaverd（所有任务共用连接） | 已实现（需要 `.venv`） |
| [mcp2.md](mcp2.md) | MCP 第二期：登录（OAuth / gh / 设备码）、服务器提问、借用模型、prompts、图片、工具清单变化、roots | 设计中 |
| [settings.md](settings.md) | 设置页：模型 · MCP · Skills，全键盘；weaverd 的 `/v1/settings/*`（计划：[settings-plan.md](settings-plan.md)） | 第一期已实现 |
| [daemon.md](daemon.md) | 常驻服务 weaverd（阶段 A）：任务管理、工作线程、本机 HTTP + SSE 接口、步骤翻译、对接 Keygent、launchd | 已实现（第 1–6 步） |
| [api.md](api.md) | weaverd 的接口说明（给 Keygent，带 JSON 示例和 Swift 对照） | 已实现 |
| [round2.md](round2.md) | 第二轮：recall 查账本、记忆提取（压缩前 / 任务后）、后台任务与通知、边生成边执行、分叉子 Agent、自定义子 Agent 类型 | 部分实现（recall、后台命令） |
| [round3.md](round3.md) | 第三轮（常驻服务下）：记忆自动提取、子 Agent 进阶（自定义类型、后台、分叉、审批升级）、跨任务搜索 | 已实现 |
| [skills.md](skills.md) | Skills：发现、作为背景信息注入、skill 工具加载、项目级信任；第二版模型自己写（提案 + 审批） | 第一版已实现 |
| [main.md](main.md) | 参考教程（32 课） | 参考资料 |

每份设计文档末尾都有“进度”一节：实现了什么、真实验证的结果、实测中发现并修掉的问题。

## 代码地图

| 目录 / 文件 | 内容 |
|---|---|
| `weaver/kernel.py` | 内核：`fold`（翻账本）、`decide`（规则表）、`billable` |
| `weaver/policy.py`、`weaver/permissions.py` | 规则；权限（放行 / 问人 / 拒绝） |
| `weaver/runner.py` | 执行者：调模型、执行工具（分批并行）、压缩、背景输入 |
| `weaver/project.py` | 投影：账本 → 发给模型的对话 |
| `weaver/providers/` | 模型接入：`openai_chat.py`、`anthropic.py`、共用的 `base.py`、`sse.py`、模型目录 `catalog.py` |
| `weaver/cache.py`、`weaver/compaction.py` | 缓存断点；压缩 |
| `weaver/context.py`、`weaver/environment.py`、`weaver/memory.py` | 背景输入机制；环境信息；记忆 |
| `weaver/tools/` | 工具：读、搜索、写、改、bash |
| `weaver/sandbox.py` | 沙箱：macOS sandbox-exec、Linux bubblewrap |
| `weaver/subagent.py` | 子 Agent（task 工具） |
| `weaver/jobs.py`、`weaver/tools/recall.py`、`weaver/corpus.py` | 后台命令与后台子 Agent 的登记表；查账本原文；跨任务搜索 |
| `weaver/extract.py`、`weaver/agents.py` | 记忆自动提取；自定义子 Agent 类型与项目信任 |
| `weaver/todo.py`、`weaver/guards.py` | todo 清单；卡死模式检测 |
| `weaver/redact.py` | 脱敏 |
| `weaver/skills.py` | Skills |
| `weaver/mcp/` | MCP：配置与信任、连接管理（官方 SDK）、变成工具 |
| `weaver/stores.py` | 账本（JSONL）和文件仓库 |
| `weaver/__main__.py` | 命令行（weaverd 在运行时交给它，否则本地跑） |
| `weaver/daemon/` | 常驻服务：`tasks.py` 任务存储、`humanize.py` 步骤翻译、`status.py` 状态推导、`manager.py` 任务管理与工作线程、`factory.py` 造执行者、`events.py` 事件总线、`uploads.py` 附件、`server.py` HTTP + SSE、`notify.py` 系统通知、`client.py` 命令行薄客户端、`control.py` 启停与 launchd、`__main__.py` 启动（`python -m weaver.daemon`） |
| `weaver/vendor/ripgrep/` | 随项目打包的 ripgrep 15.2.0（附许可证） |
| `tests/` | 全部离线测试（假模型、假 SSE 流） |
| `scripts/test-linux.sh` | 在 Docker 里跑 Linux 测试（bubblewrap、x86_64 ripgrep） |

## 内核之外，还没做的

| 模块 | 没做的 |
|---|---|
| 内核 / 执行 | 边生成边执行、Bash 失败取消同批（长命令转后台、取消打断命令已做） |
| 子 Agent | 给运行中的子 Agent 追加指令、续上旧的子 Agent、能改文件的后台子 Agent（要 git worktree）、重启后接着跑后台子 Agent、团队协作 |
| MCP | OAuth、prompts（斜杠命令）、elicitation、Weaver 自己当 MCP 服务器 |
| 模型接入 | OpenAI Responses API、Gemini 原生、服务端工具、备用模型 |
| 压缩 | 压缩后重读最近的文件（找回原文、压缩前提取记忆已做） |
| 记忆 | 按相关度挑记忆、“做梦”式整合（自动提取、搜索历史任务已做） |
| 权限 | 模型审查员（第 17 课的三级结构）、按消息来源分档授权、`sed -i` 可撤销 |
| main.md 里没碰的 | 计划模式（第 12 课）、完成判定的测试关卡 / 裁判模型（第 13 课，目前只有“清单没做完打回”）、防死循环的裁判模型、定时任务（第 23 课）、服务器版执行者的远程部署（第 24 课；本机常驻服务 weaverd 已做）、评测集（第 26 课） |
| 工程 | git 仓库、打包安装（`weaver` 命令，目前用 `python3 -m weaver`）、交互式 REPL、README、CI |
