# Weaver ask_user 设计

草案 v1 · 2026-10-02

## 一、要解决什么问题

有些事 Weaver 自己定不了，得问人：需求不清楚、要你在几个方案里拍板、设计要你批准才能动手。内置的 brainstorming、writing-plans、systematic-debugging（见 builtin-skills.md）都依赖“问一句、等回答、接着做”。

现在只有审批（放行 / 先不）、卡住了（继续 / 停下 / 提示）这类等待，没有“问一个问题”。模型只能结束这一轮、把问题写进结论，再靠追问（api.md 2.6）接上——Keygent 把任务显示成“已完成”，用户不知道它在等自己。

## 二、怎么做：当成一个“每次都等人”的工具

考虑过三种：

| 方案 | 做法 | 结论 |
|---|---|---|
| ① 工具 + 复用审批 | `ask_user` 是个工具，权限规则一律“等人”；回答作为这次调用的结果交回模型 | **采用**。落盘、事件、`/v1/waits`、取消作废、409 全部现成，内核只加一个分支 |
| ② 结束这一轮 | 问题写进结论，前端识别出“这是个问题” | 不要。靠识别文字不可靠，每问一次重开一轮 |
| ③ 内核新动作 | 状态机里加一种“问人” | 不要。改动约是 ① 的三倍，好处很少 |

## 三、后端

### 1. 工具

```
ask_user(question: str, options?: [str])
```

- 一次一问。`options` 可以不给（纯文字回答），给的话 2~4 个，单选；有选项时用户也总能自己写。
- 描述里写清楚什么时候用：需求不清楚、要用户拍板、方案要批准。**普通的“做完了告诉你”不要用它**，写进结论。
- 只给主 Agent。子 Agent 的提示里本来就有“不要问用户问题”，工具清单里也不给它。
- 参数校验：`question` 不能空；`options` 个数不对 → 工具报错，不生成等待。

### 2. 等待

权限规则（policy.py）对 `ask_user` 一律返回 `WaitSpec(kind="question")`，内核照常 `StartWait`，payload 和审批一样带 `call_id`、`name`、`args`。这条判断放在所有自动放行规则（“总是允许”、放行全部的模式）之前，任何设置都不会跳过它。

daemon 渲染（status.py）：

```json
{
  "id": "w-a1c4", "task": "…", "task_title": "Q3 流失分析", "seq": 40, "ts": 1790000300.0,
  "kind": "question",
  "title": "这次要先看华北还是全国？",
  "body": "",
  "options": ["只看华北", "全国一起看"],
  "choices": ["answer", "skip"]
}
```

- 任务状态 `waiting`，`now` 是“问你：<问题>”。
- 批量“全部放行”跳过 `question`（和 `trust` 一样）。
- 取消、这一轮结束：payload 带 `call_id`，按现有规则自动作废（kernel.py `RunFinished` 那段），不用改。

### 3. 回答

`POST /v1/waits/{id}`：

| 用户做了什么 | 请求 | 交回模型的工具结果 |
|---|---|---|
| 选了选项 / 自己写了 | `{"decision": "answer", "note": "只看华北"}` | `用户回答：只看华北` |
| 先不答，让它自己定 | `{"decision": "skip"}` | `用户这次没回答。按你自己的判断继续，并在结论里写明你做了什么假设。` |

- `answer` 的 `note` 不能空（400）。
- manager.py `_value`：`answer` → `{"allow": true, "answer": note}`；`skip` → `{"allow": true, "skip": true}`。

### 4. 内核：答案变成工具结果

kernel.py 第 6 条（还有没执行的工具调用）里，等待有结论时：

- `w.kind == "question"` → 不走 `Execute`，产出 `Synthesize(c, <上表的结果>, is_error=False)`。
- `Synthesize` 加一个字段 `is_error: bool = True`（现有用法都是报错，默认值不变）。

所以 ask_user 的工具函数本身不会被真正执行；注册它只是为了让模型看到这个工具、让参数校验有地方做。

### 5. 过程显示（humanize.py）

- 步骤标题：`问你：这次要先看华北还是全国？`，状态 `waiting`（“等你回答”）。
- 有结论后：`out` 是“你答：只看华北”或“你让它自己定”。

### 6. 命令行客户端（daemon/client.py `ask_wait`）

```
Q3 流失分析 问你：这次要先看华北还是全国？
  1. 只看华北
  2. 全国一起看
回答（数字选选项，直接打字自己写，空行 = 你自己定）：
```

`--yes`（非交互）时：不等人，直接按 `skip` 处理。

### 7. 改动的文件

| 文件 | 改什么 |
|---|---|
| `weaver/tools/__init__.py` 或新文件 | 注册 `ask_user`（只给主 Agent） |
| `weaver/policy.py` | `ask_user` 一律 `WaitSpec("question")` |
| `weaver/kernel.py` | question 结论 → 非错误的工具结果；`Synthesize.is_error` |
| `weaver/daemon/status.py` | 新 kind 的渲染、`CHOICES["question"]`；`first_wait` 带 `kind` |
| `weaver/daemon/manager.py` | `answer` / `skip` 的翻译和校验；批量放行跳过 |
| `weaver/daemon/humanize.py` | 步骤标题和结果 |
| `weaver/daemon/client.py` | 终端里回答问题 |
| `weaver/subagent.py` | 子 Agent 不给 `ask_user` |
| `design/api.md` | v1.6 变更记录（见第五节） |

## 四、Keygent

### 1. 什么时候弹

| 当时的情况 | 做法 |
|---|---|
| 面板没开，你在用别的 App | 记下当前 App → 弹出面板、抢焦点 → 打开这个任务的问题卡，光标在回答框 |
| 面板开着、你没在打字 | 直接切到问题卡 |
| 面板开着、你在打字（任何输入框里有字）/ 改参数编辑器开着 / 在逐件看 | **不打断**：顶部横幅「Q3 流失分析 问你 · ⌘⇧空格 去回答」 |
| 同时有好几个问题 | 一次弹一个，答完再弹下一个 |
| 这个问题被你 esc 收起过 | 不再自动弹；胶囊一直是「X · 问你」，⌘⇧空格 随时回来 |

“记下当前 App”只在自动弹出时做；你自己按热键打开的不算。

### 2. 防误输入

- **输入锁 0.5 秒**：自动弹出后 0.5 秒内的按键全部吞掉（PanelController 的本地按键监听里直接丢弃），回答框上有一条淡的进度条，走完才能输入。
- **答完回原来的 App**：回答、⌘↵（你自己定）或 esc 之后，激活弹出前记下的那个 App，键盘回到你原来的地方。

### 3. 问题卡

```
┌─ Q3 流失分析 · 问你 ────────────────────────┐
│ 这次要先看华北还是全国？                      │
│                                             │
│  ⌘1  只看华北                                │
│  ⌘2  全国一起看                    ← ↑↓ 高亮  │
│ ┌─────────────────────────────────────────┐ │
│ │ 回答 ▌                                  │ │
│ └─────────────────────────────────────────┘ │
│ ↵ 发送   ⌘1–4 选这个   ⌘↵ 你自己定   esc 稍后 │
└─────────────────────────────────────────────┘
```

| 按键 | 作用 |
|---|---|
| 打字 + ↵ | 发自己写的回答；⇧↵ 换行 |
| ⌘1–4 | 选第几个选项，**立即发送** |
| ↑↓ + ↵ | 回答框空着时 ↑↓ 高亮选项，↵ 发高亮的那个；框空、没高亮时 ↵ 不做事 |
| ⌘↵ | 先不答，让它自己定（`skip`） |
| esc | 收起面板、回原来的 App，问题留着 |
| ⌘⌫ | 停下这一轮（和现在一样） |

和现有约定一致：⌘↵ 在别处是主选项（放行 / 继续），意思都是“让它往下走”；⌘数字 一直是“眼前第几条”。

### 4. 其他地方

- **胶囊**：按 kind 出文字——question「X · 问你」，其余还是「等你放行」。
- **任务页 / 详情页**：GateCard 加问题样式（问题 + 带 ⌘数字 的选项）；底部输入框进入“回答”模式（新状态 `answerFor`，用法同 `hintFor`，前缀标签“回答”，EnterHint「发给它」）。详情页的输入框也要认 `answerFor`（现在它只能插话）。
- **逐件看**：问题类显示问题和选项，↵ 跳到这个任务的问题卡去答；不在逐件看里直接答（⌘1–5 在这里是跳到第几件，会冲突）。“放行剩下的”跳过问题类。
- **启动器**：任务行徽标不变（「N 件等你」）。

### 5. 改动的文件

| 文件 | 改什么 |
|---|---|
| `API/APIModels.swift` | `WaitItem.options: [String]?`；`StatusSummary.Ref.kind` |
| `Model/Models.swift` | question 的 `sub`、按钮文字（answer 发给它、skip 你自己定）、`primaryChoice` 不适用；胶囊文字按 kind |
| `App/PanelController.swift` | 自动弹出（记下 / 恢复原来的 App）、输入锁 |
| `Store/AppStore.swift` | `wait` 事件到来时按第 1 小节的规则决定弹 / 横幅；`answerFor`；批量放行跳过 |
| `Store/AppStore+Task.swift`、`+Detail.swift`、`+Queue.swift` | 问题卡的按键 |
| `Views/Task/TaskView.swift`（GateCard）、`Views/Detail/DetailView.swift`、`Views/Queue/QueueView.swift` | 问题样式、横幅 |

## 五、API 变更（api.md v1.6）

- 新的等待类型 `kind: "question"`：多一个 `options: [String]`（可能为空），`choices: ["answer", "skip"]`。
- `answer` 必须带非空 `note`；`skip` 不带。
- `GET /v1/status` 的 `first_wait` 多一个 `kind`。
- 步骤里会出现“问你：…”（`status: waiting`），有结论后 `out` 是“你答：…”或“你让它自己定”。

## 六、测试

后端（`tests/`）：
1. 工具：主 Agent 有、子 Agent 没有；参数校验（空问题、选项 1 个或 5 个）报错且不生成等待。
2. 等待：调用 ask_user → 一件 `question` 等待，任务 `waiting`，`now` 是“问你：…”。
3. 回答：`answer` → 工具结果“用户回答：…”、`is_error` 为假、模型接着跑；`skip` → 对应文字；`answer` 不带 note → 400；重复回答 → 409。
4. 作废：等回答时取消 → 等待作废；批量放行跳过 question。
5. 终端：`ask_wait` 数字选选项、打字、空行 skip；`--yes` 直接 skip。
6. 真实验证：让 Weaver 做一件需求故意含糊的事（“帮我把这个目录整理一下”），看它是否用 ask_user 问、答完是否照着做。

Keygent（手动 + DebugHooks）：
1. 面板没开时来问题：弹出、焦点在回答框、0.5 秒内按键被吞、答完回到原来的 App。
2. 正在启动器打字时来问题：只出横幅，⌘⇧空格 进问题卡。
3. ⌘1 / ↑↓↵ / 打字↵ / ⌘↵ / esc 各走一遍，确认发出的请求和结果。
4. 两个问题连着来：答完第一个自动出第二个。

## 七、不做的事

- 一次多问、多选（以后真需要再加）。
- 子 Agent 直接问人。
- 问题超时自动 skip（等待一直挂着，和审批一样）。

---

## 进度（2026-10-02）

已实现：后端（`weaver/ask.py`、kernel 第 6 条、policy、daemon 渲染 / 回答 / 过程、两个命令行）、Keygent（问题卡、自动弹出 + 0.5 秒输入锁、横幅、⌘⇧空格 直达）、api.md v1.6。测试 `tests/test_ask_user.py`、`tests/test_daemon_manager.py::Questions`。

和上面设计的出入：
- “回到原来的 App”没有记 NSWorkspace：面板是不激活 App 的悬浮窗，收起后键盘自然回到原来的 App。
- 回答框是单行的：⇧↵ 不换行，⇧↵ ⌥↵ ⌃↵ 都吞掉（不发送）。
- 只有主 Agent 的权限规则（`PermissionPolicy(ask_user=True)`）为 ask_user 等人；分叉的子 Agent 在执行时被拒绝（`FORK_DENIED`）。
- 评审后补的：同一任务里审批排在问题前面时，问题卡和放行卡分开显示（以前回答会被当成普通追问发出去）；`first_wait` 优先给问题，⌘⇧空格 / 点横幅有问题就直达问题卡；用户的回答不被上下文裁剪，摘要附带的“用户原话”里也有它。

真实验证：真实模型 + Keygent，“帮我把这个目录整理一下，动手前先用 ask_user 问我”→ 面板自动弹出问题卡（4 个选项、回答模式、胶囊“问你”）→ 用户在面板上选了第 1 项 → 步骤显示“你答：按文件类型分文件夹…” → 它按类型分进 4 个文件夹后结束。

没做 / 留待以后：详情页答问题时来第二个问题的排队细节、自动弹出会清掉启动器里写了一半的话、`weaver approve` 命令行没法带回答文字、后台触发的任务是否也该抢焦点弹出。
