import SwiftUI
import Observation
import AppKit

enum InputField: Hashable {
    case launcher, task, queueEdit, detail, editor
    /// MCP 服务器提问的第几个字段；启动器里 prompt 的第几个参数
    case elicit(Int), promptArg(Int)
    /// 设置页（AppStore+Settings）：模型页第几行、多行编辑器、MCP 服务器名字、常用服务器筛选、要填的第几项
    case settingsRow(Int), settingsEditor, settingsName, settingsFilter, settingsNeed(Int)
}

enum DetailOrigin: Equatable { case task, queue }

/// 同一个面板里的几种形态：① 启动器 → ③ 任务 → ⑤ 详情，④ 等你的事。
enum Route: Equatable {
    case launcher, task, queue
    case detail(DetailOrigin)
}

enum Connection: Equatable { case connecting, online, offline }

// MARK: - 各屏状态

/// 窗口式列表：一屏只摆 size 条，⌘数字 = 眼前第几条，滚轮 / ↑↓ 挪窗口，编号跟着重排。
/// 最近任务、逐件看、添加文件、工作区、搜索结果、过程步骤都用这一套，条数再多快捷键也够用。
enum ListWindow {
    static let size = 5

    static func clamp(_ top: Int, count: Int) -> Int {
        max(0, min(top, count - size))
    }

    /// 让第 cur 条露出来，窗口尽量少动
    static func fit(_ cur: Int, top: Int, count: Int) -> Int {
        var t = top
        if cur < t { t = cur }
        if cur > t + size - 1 { t = cur - size + 1 }
        return clamp(t, count: count)
    }

    static func range(top: Int, count: Int) -> Range<Int> {
        let t = clamp(top, count: count)
        return t..<min(count, t + size)
    }
}

struct LauncherState {
    static let viewCount = ListWindow.size

    var query = ""
    var pick: Int? = nil
    var top = 0
    /// ⌘O 添加文件：面板开着没有、高亮第几个、窗口从第几个开始
    var picker = false
    var filePick = 0
    var fileTop = 0
    /// ⌘E 选工作区：面板开着没有、高亮第几条（0 = 临时，1… = 最近）、最近列表的窗口顶、选定的文件夹（nil = 临时）
    var wsPicker = false
    var wsPick = 0
    var wsTop = 0
    var workdir: String? = Workspaces.current
    var attachments: [Attachment] = []
    var submitting = false
    /// 搜索（输入以 ? 开头）：结果、选中第几条、正在搜
    var results: [SearchHit] = []
    var resultPick: Int? = nil
    var resultTop = 0
    var searching = false
    var searchedFor = ""
    /// ⌘⇧A 已归档：列表、选中第几条、窗口顶、正在读
    var archivedMode = false
    var archived: [TaskSummary] = []
    var archPick = 0
    var archTop = 0
    var archLoading = false
    /// 输入以 / 开头：MCP prompts（按工作区拉一次）、选中第几条、窗口顶；选了带参数的 → 参数表单
    var prompts: [PromptItem] = []
    var promptsFor: String? = nil
    var promptsLoading = false
    var promptPick = 0
    var promptTop = 0
    var promptArgs: PromptItem? = nil
    var argValues: [String] = []
    var argCur = 0
}

struct TaskState {
    var id: String? = nil
    var summary: TaskSummary? = nil
    var detail: TaskDetail? = nil
    var loading = false
    var proc = false
    /// 过程里选中的步骤；nil = 跟着最新一步
    var step: Int? = nil
    /// 过程列表的窗口顶；nil = 贴着最新的几步。鼠标在列表上时滚轮才挪它
    var stepTop: Int? = nil
    var stepsHovered = false
    var draft = ""
    var sending = false
    /// 模型正在输出的文字（delta 事件），新的 agent 步骤到了就清空
    var delta = ""
    /// 「给个提示」模式：输入框发出去的是对这件等待的提示
    var hintFor: WaitItem? = nil
    /// 正在回答的等待
    var answering: String? = nil
    /// 从搜索结果打开：加载完跳到这一条（账本序号）
    var focusSeq: Int? = nil
    /// 结果太长时默认折叠，space 展开
    var resultOpen = false
    /// 问题卡里 ↑↓ 高亮的选项（下标）
    var optionPick: Int? = nil
    /// 归档的任务：只读，⌘R 恢复
    var archived = false
    /// 改过的文件里选中第几个（⌘D 看 diff、⌘Z 撤销它）
    var changePick = 0
    /// 结果卡底部改过的文件太多时折起来，展开了没有
    var changesOpen = false
    /// MCP 服务器问你：在填哪一件（等待 id）、填了什么、光标在第几个字段
    var elicitFor: String? = nil
    var elicitValues: [String: JSONValue] = [:]
    var elicitCur = 0
}

struct PeekState {
    var task: String? = nil
    var todos: [TodoItem] = []
    var steps: [Step] = []
}

/// ⌘D：一个任务改过的文件的 diff，↑↓ 换文件
struct DiffSheetState {
    var task: String
    var archived: Bool
    var files: [FileChange]
    var cur: Int
    var diffs: [String: FileDiff] = [:]
    var error: String? = nil
}

/// 子 Agent 的过程（从过程面板「看它的过程」、后台条、搜索结果进来）
struct AgentViewState {
    var task: String
    var sub: String
    var archived: Bool
    var detail: AgentDetail? = nil
    var step: Int? = nil
    var top: Int? = nil
    var focusSeq: Int? = nil
    var error: String? = nil
}

struct QueueState {
    static let viewCount = ListWindow.size

    var expanded = false
    /// 逐件看时的列表快照：处理过的留在原位显示结果，编号不乱跳
    var list: [WaitItem] = []
    var decided: [String: String] = [:]
    var cur = 0
    var top = 0
    var editDraft = ""
    var working: String? = nil
    var handledCount = 0
}

struct DetailState {
    var from: DetailOrigin = .task
    var at = -1
    var draft = ""
}

/// 「改一下」：把 call.args 摊成表单
struct ArgsEditorState {
    struct Field: Identifiable {
        var id: String { key }
        let key: String
        var text: String
        let original: JSONValue
    }

    let wait: WaitItem
    var fields: [Field]

    init(wait: WaitItem) {
        self.wait = wait
        fields = (wait.call?.args ?? [:]).sorted { $0.key < $1.key }.map {
            Field(key: $0.key, text: $0.value.editableText, original: $0.value)
        }
    }

    var args: [String: JSONValue] {
        Dictionary(uniqueKeysWithValues: fields.map { ($0.key, JSONValue.from(text: $0.text, like: $0.original)) })
    }
}

// MARK: - Store

@Observable
final class AppStore {
    var route: Route = .launcher
    var connection: Connection = .connecting
    /// 连不上时，内嵌 weaverd 那边的原因（DaemonService）
    var daemon: DaemonService.Health? = nil

    var status: StatusSummary? = nil
    var tasks: [TaskSummary] = []
    var waits: [WaitItem] = []
    var loadedOnce = false

    var launcher = LauncherState()
    var task = TaskState()
    var queue = QueueState()
    var detail = DetailState()
    var editor: ArgsEditorState? = nil
    var diffSheet: DiffSheetState? = nil
    /// 胶囊下那张小卡的内容（PeekController）
    var peek = PeekState()
    var agentView: AgentViewState? = nil
    /// 等第二次确认的操作（任务页）：“undo:路径”
    var armed: String? = nil
    /// 设置页（AppStore+Settings）；面板收起时不清，切出去复制 key 再回来还在
    var settings: SettingsState? = nil
    /// 设置页上次停在哪个分页
    @ObservationIgnored var lastSettingsTab: SettingsTab = .model
    @ObservationIgnored var mcpTimer: Timer?
    @ObservationIgnored var mcpParseWork: DispatchWorkItem?

    var toast: String? = nil
    /// 顶部横幅：面板开着、你在忙时来了问题（AppStore+Question）
    var banner: String? = nil
    /// 自动弹出问题卡后的输入锁：到这个时间之前的按键全部吞掉（PanelController）
    var questionLockUntil: Date? = nil
    var panelVisible = false
    /// 面板最多能长多高（顶边固定，往下到屏幕可视区底部）；PanelController 打开时按当前屏幕算
    var panelMaxHeight: CGFloat = 800

    private(set) var focusRequest: InputField? = .launcher
    private(set) var focusToken = 0
    var focusedField: InputField? = nil

    @ObservationIgnored let client = WeaverClient()
    @ObservationIgnored private(set) lazy var stream = EventStream(client: client)

    // 由 AppKit 层注入的副作用
    @ObservationIgnored var hidePanel: () -> Void = {}
    /// 弹出面板并成为 key（不激活 App，收起后键盘自然回到原来的 App）
    @ObservationIgnored var popPanel: () -> Void = {}
    /// 自动弹出的那个问题：答完 / esc 后收起面板
    @ObservationIgnored var poppedQuestion: String? = nil
    /// 你按 esc 收起过的问题：不再自动弹
    @ObservationIgnored var dismissedQuestions: Set<String> = []
    @ObservationIgnored var bannerWork: DispatchWorkItem?
    /// 横幅说的是哪个问题：点横幅直接去它
    @ObservationIgnored var bannerFor: WaitItem? = nil
    @ObservationIgnored var openFinder: () -> Void = {}
    @ObservationIgnored var openFolder: () -> Void = {}
    @ObservationIgnored var resignInput: () -> Void = {}
    /// 面板收着时来了新的等待（AppDelegate 接到胶囊下的小卡上）。系统通知只由 weaverd 在 App 没开时发
    @ObservationIgnored var onWaitWhileHidden: () -> Void = {}
    /// 任务页中间那块滚动区按键滚动（↑↓），由视图里的 ScrollNudger 注册
    @ObservationIgnored var taskScrollBy: (CGFloat) -> Void = { _ in }

    @ObservationIgnored private var toastWork: DispatchWorkItem?
    @ObservationIgnored private var statusWork: DispatchWorkItem?
    @ObservationIgnored private var detailWork: DispatchWorkItem?
    @ObservationIgnored private var offlineTimer: Timer?
    @ObservationIgnored var agentTimer: Timer?

    var capsule: CapsuleState { connection == .offline ? .offline : CapsuleState(status) }

    // MARK: 启动 / 连接

    func start() {
        stream.onEvent = { [weak self] in self?.handle($0) }
        stream.onConnected = { [weak self] up in
            guard let self else { return }
            if up {
                if self.connection != .online { self.refreshAll() }
            }
        }
        stream.start()
        refreshAll()
        // 没连上时每 3 秒再试一次（weaverd 起来后自动接上）
        offlineTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            guard let self, self.connection == .offline else { return }
            self.refreshAll()
        }
    }

    /// 重启内嵌的 weaverd，起来后由每 3 秒的重试自动接上
    func restartDaemon() {
        DaemonService.restart()
        connection = .offline
        status = nil
        daemon = .starting
        flash("正在重启 Weaver")
    }

    func refreshAll() {
        Task { @MainActor in
            do {
                async let s = client.status()
                async let t = client.tasks()
                async let w = client.waits()
                let (ss, tt, ww) = try await (s, t, w)
                status = ss
                tasks = tt.sorted { $0.updated > $1.updated }
                waits = ww
                connection = .online
                daemon = nil
                loadedOnce = true
                clampLauncher()
                if let id = task.id, route != .launcher { loadTask(id) }
            } catch {
                handleError(error, quiet: true)
            }
        }
    }

    func refreshStatusSoon() {
        statusWork?.cancel()
        let w = DispatchWorkItem { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                if let s = try? await self.client.status() { self.status = s }
            }
        }
        statusWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: w)
    }

    func reloadTaskSoon() {
        guard let id = task.id else { return }
        detailWork?.cancel()
        let w = DispatchWorkItem { [weak self] in self?.loadTask(id) }
        detailWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: w)
    }

    func handleError(_ error: Error, quiet: Bool = false) {
        if let e = error as? APIError, e == .notRunning {
            connection = .offline
            status = nil
            daemon = DaemonService.health()
            // 还没配模型：面板里只给配置，不显示用不了的启动器
            if daemon == .missingConfig, settings == nil { openSettings() }
            if !quiet { flash("Weaver 没在运行") }
            return
        }
        if !quiet { flash((error as? LocalizedError)?.errorDescription ?? error.localizedDescription) }
    }

    // MARK: 事件流

    func handle(_ ev: WeaverEvent) {
        switch ev {
        case .task(let s):
            if let i = tasks.firstIndex(where: { $0.id == s.id }) { tasks[i] = s } else { tasks.insert(s, at: 0) }
            tasks.sort { $0.updated > $1.updated }
            if task.id == s.id {
                let wasActive = task.summary?.kind.isActive ?? true
                task.summary = s
                task.detail?.task = s
                // 一轮结束了：拉一次详情拿 final
                if wasActive && !s.kind.isActive { reloadTaskSoon() }
            }
            refreshStatusSoon()

        case .wait(let w):
            if let i = waits.firstIndex(where: { $0.id == w.id }) { waits[i] = w } else { waits.append(w) }
            if queue.expanded, !queue.list.contains(where: { $0.id == w.id }) { queue.list.append(w) }
            if w.isQuestion {
                questionArrived(w)                 // 弹出问题卡，不再另发系统通知
            } else if !panelVisible {
                onWaitWhileHidden()                // 面板收着：胶囊下的小卡说一声（面板开着时顶上的横幅说）
            }
            refreshStatusSoon()

        case .waitClosed(_, let wid):
            waits.removeAll { $0.id == wid }
            if queue.expanded, queue.decided[wid] == nil, queue.list.contains(where: { $0.id == wid }) {
                queue.decided[wid] = "已在别处处理"
            }
            if task.hintFor?.id == wid { task.hintFor = nil }
            dismissedQuestions.remove(wid)
            if poppedQuestion == wid { poppedQuestion = nil }
            if editor?.wait.id == wid { editor = nil }
            refreshStatusSoon()

        case .step(let tid, let step):
            guard tid == task.id, task.detail != nil else { return }
            if let i = task.detail!.steps.firstIndex(where: { $0.seq == step.seq && $0.title == step.title }) {
                task.detail!.steps[i] = step
            } else {
                task.detail!.steps.append(step)
            }
            if step.kind == .agent { task.delta = "" }
            // 清单变了、改了文件：拉一次详情拿 todos / changes（事件里不带）
            if ["todo_write", "edit_file", "write_file", "task", "undo"].contains(step.tool), step.status != "running" {
                reloadTaskSoon()
            }

        case .delta(let tid, let text):
            guard tid == task.id else { return }
            task.delta = String((task.delta + text).suffix(600))

        case .archived(let tid):
            tasks.removeAll { $0.id == tid }
            waits.removeAll { $0.task == tid }
            if task.id == tid, route != .launcher {
                go(.launcher)
                flash("这个任务被归档了")
            }
            clampLauncher()
            refreshStatusSoon()

        case .reset:
            refreshAll()
        }
    }

    // MARK: 通用

    func requestFocus(_ f: InputField?) {
        focusRequest = f
        focusToken &+= 1
        if f == nil { resignInput() }
    }

    func flash(_ text: String) {
        toastWork?.cancel()
        toast = text
        let w = DispatchWorkItem { [weak self] in self?.toast = nil }
        toastWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.2, execute: w)
    }

    func copy(_ text: String, note: String = "已复制") {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        flash(note)
    }

    func go(_ r: Route) {
        route = r
        switch r {
        case .launcher: requestFocus(.launcher)
        default: requestFocus(nil)
        }
    }

    /// 面板被呼出时
    func didShow() {
        panelVisible = true
        if let st = settings {
            switch st.tab {
            case .model: requestFocus(.settingsRow(st.row))
            case .mcp: requestFocus(mcpFocus)
            case .skills: requestFocus(st.skills.editing != nil ? .settingsEditor : nil)
            case .permissions: requestFocus(nil)
            case .memory: requestFocus(st.memory.editing != nil ? .settingsEditor : nil)
            }
        } else if route == .launcher {
            requestFocus(.launcher)
        } else if taskQuestion != nil {
            // 问题卡：窗口成为 key 之后再聚焦回答框（之前的请求会落空）
            if case .detail = route { requestFocus(.detail) } else if route == .task { requestFocus(.task) }
        }
        if connection != .online { refreshAll() }
    }

    func didHide() {
        panelVisible = false
        poppedQuestion = nil                       // 你自己点走了：之后手动回来答完，不再替你收起面板
        launcher.picker = false
        launcher.wsPicker = false
        editor = nil
        armed = nil
    }

    func handleKey(_ e: KeyEvent) -> Bool {
        if settings != nil { return settingsKey(e) }
        if e.cmd, e.isChar(",") { openSettings(); return true }
        if editor != nil { return editorKey(e) }
        if diffSheet != nil { return diffKey(e) }
        if agentView != nil { return agentKey(e) }
        switch route {
        case .launcher: return launcherKey(e)
        case .task: return taskKey(e)
        case .queue: return queueKey(e)
        case .detail: return detailKey(e)
        }
    }

    // MARK: 胶囊

    /// 点胶囊：直接打开胶囊里的那件事（多件等你 → ④，单个任务 → ③）。
    func openCapsuleTarget() {
        switch capsule {
        case .waiting(let tid, _, let more, _):
            // 有问题在等：直接到问题卡，光标在回答框（不经过「等你的事」，也不管别处有没有更早的审批）
            if let w = questionTarget {
                openQuestion(w)
            } else if more > 0 { openQueue() } else { openTask(id: tid) }
        case .error(let tid, _):
            openTask(id: tid)
        default:
            go(.launcher)
        }
    }

    /// 胶囊里那件事排在启动器 ⌘1。
    func pinCapsuleTaskFirst() {
        let tid: String?
        switch capsule {
        case .waiting(let t, _, _, _), .error(let t, _): tid = t
        default: tid = nil
        }
        guard let tid, let i = tasks.firstIndex(where: { $0.id == tid }), i != 0 else { return }
        let t = tasks.remove(at: i)
        tasks.insert(t, at: 0)
    }

    // MARK: 回答等待（③ ④ 共用）

    func answer(_ w: WaitItem, _ decision: String, note: String? = nil, args: [String: JSONValue]? = nil,
                values: [String: JSONValue]? = nil, done: ((Bool) -> Void)? = nil) {
        task.answering = w.id
        queue.working = w.id
        Task { @MainActor in
            defer {
                if task.answering == w.id { task.answering = nil }
                if queue.working == w.id { queue.working = nil }
            }
            do {
                let s = try await client.answer(w.id, decision: decision, note: note, args: args, values: values)
                waits.removeAll { $0.id == w.id }
                if let i = tasks.firstIndex(where: { $0.id == s.id }) { tasks[i] = s }
                if task.id == s.id { task.summary = s; reloadTaskSoon() }
                refreshStatusSoon()
                done?(true)
            } catch let e as APIError where e.code == "conflict" {
                waits.removeAll { $0.id == w.id }
                flash("这件事已经在别处处理了")
                refreshStatusSoon()
                done?(true)
            } catch {
                handleError(error)
                done?(false)
            }
        }
    }

    // MARK: 「改一下」表单

    func openEditor(_ w: WaitItem) {
        guard w.call != nil else { return }
        editor = ArgsEditorState(wait: w)
        requestFocus(.editor)
    }

    func submitEditor() {
        guard let ed = editor else { return }
        answer(ed.wait, "allow", args: ed.args) { [weak self] ok in
            guard let self else { return }
            if ok {
                self.editor = nil
                if self.queue.expanded { self.queueMarkDecided(ed.wait.id, "改过后放行") }
                self.flash("已按改过的参数放行")
            }
        }
    }

    func editorKey(_ e: KeyEvent) -> Bool {
        if e.cmd, e.key == .enter { submitEditor(); return true }
        if e.key == .escape {
            editor = nil
            requestFocus(nil)
            return true
        }
        return false
    }

    // 设置页（模型 · MCP · Skills）在 AppStore+Settings.swift
}
