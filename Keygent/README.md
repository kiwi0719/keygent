# Keygent（macOS · SwiftUI）

weaverd 的桌面前端，复刻自设计稿「Agent 启动器 · 完整流程」。接口按 [`design/api.md`](../design/api.md) v1 对接，没有假数据。

## 运行

```bash
./scripts/fetch-python.sh      # 第一次：下载内嵌 Python + MCP SDK（放在 Vendor/python）
xcodegen generate
xcodebuild -project Keygent.xcodeproj -scheme Keygent -derivedDataPath build/DD build
open build/DD/Build/Products/Debug/Keygent.app
```

或 `open Keygent.xcodeproj` 后直接 ⌘R。

- 连接信息从 `~/.weaver/daemon.json` 读（`port` + `token`）。读不到或连不上时显示「Weaver 没在运行」，每 3 秒重试，weaverd 起来后自动接上。
- 联调时可以用环境变量 `WEAVER_DAEMON_JSON=/path/to/daemon.json` 指到别的文件。
- `⌘⇧空格` 呼出 / 收起启动器（唯一的全局热键）；等你的那件事排在 `⌘1`。点菜单栏胶囊直接打开胶囊里的那件事。
- 菜单栏胶囊：左键打开，右键有「等你的事」「重新连接」「退出」。面板收起时（有任务在跑）胶囊下面浮出一张小卡 4 秒，列着清单或最近几步；鼠标停在胶囊上也出来。
- 任务页：`⌘.` 看过程（`↑↓` 选步骤，派子 Agent 那一步 `↵` 看它的过程）；`⌘D` 看改过的文件的 diff，`⌘Z` 撤销一个文件（按两次）。
- 启动器：`?` 开头搜以前的任务（含归档的）；`/` 开头用 MCP 服务器的提示词；`⌘⇧A` 已归档的任务（`⌘R` 恢复）。
- `⌘,` 设置：模型 · MCP · Skills · 权限 · 记忆五页（`⌘[` `⌘]` 切换），全键盘。联调时用 `WEAVER_DAEMON_JSON` 接到另一个 weaverd：App 不注册、不重启 launchd 里的 weaverd，模型配置也写到那个 weaverd 的目录。

## 结构

| 目录 | 内容 |
| --- | --- |
| `API/` | `APIModels`（api.md 第 4 节的类型）、`WeaverClient`（第 2 节的接口）、`EventStream`（第 3 节 SSE，断线退避重连 + 游标补发 + `reset`） |
| `Store/` | `AppStore` 核心（连接、事件处理、回答等待）+ 每屏一个 extension（Launcher / Task / Queue / Detail），键盘逻辑也在这里 |
| `Model/Models.swift` | 状态 → 颜色 / 文字（1.1）、胶囊规则（2.1）、等待的按钮（1.4）、时间显示（第 0 节） |
| `Views/` | ① `Launcher` ② `Capsule` ③ `Task` ④ `Queue` ⑤ `Detail`；`Components/MarkdownView` 渲染 `final`，`ArgsEditorView` 是「改一下再放行」 |

和设计稿的对应：

| 设计稿 | 现在的数据 |
| --- | --- |
| 结果表格 | `TaskDetail.final`（Markdown，表格照样渲染成表格） |
| 等你放行 / 出错 | `WaitItem`（审批 / 卡住了 / 外部输入），按钮由 `choices` 决定；`status == error` 显示 `note` |
| 等你的事 · 逐封看 | 所有 `WaitItem`，逐件看；文字框 = `hint` 或带 `note` 的 `deny` |
| 详情的版本时间线 | 步骤时间线：在「现在」看结论，往回走看当时那一步 |
| 最近用过的文件 | 在 Keygent 里附加过的文件（本机记录），`⌘⇧O` 从访达选 |

## 调试

Debug 构建带一个调试入口（`Debug/DebugHooks.swift`），只有用 `KEYGENT_DEBUG_HOOKS=1` 启动时才生效：监听分布式通知 `com.keygent.debug`，可以切界面、打印状态，方便配合按键模拟做实测。
