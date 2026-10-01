# Weaver 环境信息设计

草案 v1 · 2026-10-01

## 一、要解决什么问题

实测里模型开头白白浪费两轮：先 `cd` 到一个自己编的路径，又用了本机没有的 `python`。它不知道自己在哪、机器上有什么。

Codex 的做法：会话开始时把“环境上下文”（工作目录、shell、日期等）作为一条消息放进历史，变化时再追加。我们照这个思路，复用记忆快照的 `context` 机制。

## 二、写什么

```
<system-reminder>
## 环境
- 工作目录：/Users/kiwi/proj（bash 每条命令都从这里开始，不用 cd 到别处；相对路径都相对它）
- 平台：macOS 15.8（arm64）
- shell：/bin/bash，每条命令一个新 shell，不保留 cd 和环境变量
- 可用命令：python3（3.14.7，没有 python，请用 python3）、pip3、node（v22.1.0）、npm、git、make
- git：是 git 仓库，当前分支 main
- 沙箱：seatbelt，只能写工作目录和临时目录，可联网
- 日期：2026-10-01
</system-reminder>
```

- **可用命令**：只列本机真的有的，从一份常用清单里找（python3、python、pip3、pip、uv、node、npm、pnpm、yarn、bun、go、cargo、rustc、java、make、docker、git）。只有 python、node 查版本（各 2 秒超时），别的只看有没有。有 python3 没有 python 时专门写一句“请用 python3”，这正是实测踩的坑。
- 控制在 15 行以内，几百 token。

## 三、放在哪：和记忆快照同一套机制

**不放进 system prompt**：system 要整个会话逐字节不变（缓存）。日期、分支会变，放进去会破坏前缀。

**作为 `source=context` 的背景输入记进账本**：

| 时机 | 做法 |
|---|---|
| 会话里第一条用户输入之前 | 追加“环境”背景输入，再追加“记忆”背景输入 |
| 之后每条用户输入之前 | 重新生成；和账本里上一份不同（比如换了分支、过了一天）就**在末尾追加**一份新的，说明“环境有变化” |
| 同一任务中途 | 不动 |

追加在末尾，不改前缀，缓存不受影响；压缩时 `context` 输入原样保留（记忆设计里已经做了）。

## 四、代码结构：把“背景输入”做成通用机制

现在记忆快照的逻辑写在 `Memory.context_input` 里，执行器专门为它开了个口子。加了环境之后，改成通用的“背景提供者”：

```python
class ContextProvider:
    kind: str                       # "environment" / "memory"
    def render(self) -> str         # 当前内容；空字符串表示没有可说的
    first_head: str                 # 第一次放进账本时的开头说明
    update_head: str                # 内容变了、追加更新时的开头说明
```

- 执行器持有一个提供者列表 `[环境, 记忆]`，每条用户输入前逐个检查：账本里这个 `kind` 最近一份的哈希和现在的不一样，就追加一条 `InputReceived(source=context, context_kind=…, digest=…)`。
- 记忆模块改成一个提供者（兼容已有账本里的 `memory_digest` 字段）。
- 以后别的背景信息（比如打开的文件、MCP 服务器说明）也按这个接口加。

## 五、测试

1. 环境内容：工作目录、平台、只列存在的命令、只有 python3 时的提示、git 分支、非 git 目录、沙箱说明。
2. 顺序：会话开头先环境、后记忆，都在第一条用户输入之前。
3. 没变化不追加；换分支后下一条输入前追加“环境有变化”；多轮之间前缀不变。
4. 兼容：旧账本里只有 `memory_digest` 的快照照样被认出来，不重复追加。
5. 真实验证：重跑“修 calc.py”，看模型开头还会不会乱 `cd`、用 `python`。

---

## 进度（2026-10-01）

已实现：`weaver/context.py`（通用背景提供者、`pending_context`）、`weaver/environment.py`；记忆模块改成一个提供者（兼容旧账本的 `memory_digest`）；执行器用 `context=[环境, 记忆]`；CLI 显示 `[环境]`、`[记忆]`。测试见 `tests/test_environment.py`。

真实验证：重跑“修 calc.py”。之前开头浪费两轮（`cd` 到编造的路径、用不存在的 `python`）；这次一开始就用 `python3`，`cd` 的也是真实的工作目录，整个任务从 8 次模型调用降到 6 次。
