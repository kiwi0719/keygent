import SwiftUI

// MARK: - ③ 任务

extension AppStore {
    func openTask(id: String, summary: TaskSummary? = nil) {
        var s = TaskState()
        s.id = id
        s.summary = summary ?? tasks.first { $0.id == id }
        task = s
        launcher.query = ""
        launcher.pick = nil
        go(.task)
        loadTask(id)
    }

    func loadTask(_ id: String) {
        if task.detail == nil { task.loading = true }
        let archived = task.archived
        Task { @MainActor in
            defer { if task.id == id { task.loading = false } }
            do {
                let d = try await client.task(id, archived: archived)
                guard task.id == id else { return }
                task.detail = d
                task.summary = d.task
                if let seq = task.focusSeq, let i = d.steps.lastIndex(where: { $0.seq == seq }) {
                    task.step = i                    // 从搜索结果来的：展开过程，选中命中的那一步
                    task.stepTop = ListWindow.fit(i, top: d.steps.count, count: d.steps.count)
                    task.proc = true
                    task.focusSeq = nil
                }
                if archived { return }               // 归档的：只读，不进最近任务和等你的事
                if let i = tasks.firstIndex(where: { $0.id == id }) { tasks[i] = d.task }
                // 服务端的 waiting 是这个任务的全量；以它为准合并进全局列表
                waits.removeAll { $0.task == id }
                waits.append(contentsOf: d.waiting)
                waits.sort { $0.ts < $1.ts }
            } catch let e as APIError where e.code == "not_found" {
                guard task.id == id else { return }
                tasks.removeAll { $0.id == id }
                go(.launcher)
                flash(e.errorDescription ?? "没有这个任务")
            } catch {
                handleError(error)
            }
        }
    }

    var taskKind: TaskKind { task.summary?.kind ?? .run }
    var taskSteps: [Step] { task.detail?.steps ?? [] }
    var taskWaits: [WaitItem] { waits.filter { $0.task == task.id } }
    /// 放行卡（审批 / 卡住了 / 信任）：问题另有问题卡（taskQuestion），两张可以同时在
    var taskGate: WaitItem? { taskWaits.first { !$0.isQuestion } }
    var taskCreated: Double { task.summary?.created ?? task.detail?.task.created ?? 0 }
    /// 这一轮从哪一刻开始：最后一句「你说的」，没有就是任务创建时
    var taskRoundStart: Double { taskSteps.last(where: { $0.kind == .you })?.ts ?? taskCreated }

    /// 这一轮（最后一句「你说的」之后）的每一步，带上在 taskSteps 里的下标
    var taskRoundItems: [(index: Int, step: Step)] {
        let steps = taskSteps
        let start = (steps.lastIndex(where: { $0.kind == .you }) ?? -1) + 1
        return (start..<steps.count).map { ($0, steps[$0]) }
    }

    var taskCurrentStep: Int {
        let n = taskSteps.count
        guard n > 0 else { return 0 }
        return min(task.step ?? n - 1, n - 1)
    }

    /// 过程列表眼前这几步；stepTop 为 nil 时贴着最新的
    var taskStepWindow: Range<Int> {
        let n = taskSteps.count
        return ListWindow.range(top: task.stepTop ?? n, count: n)
    }

    /// 选上一步 / 下一步，窗口跟着挪（选中的那步总在眼前）
    func taskStepMove(_ d: Int) {
        let n = taskSteps.count
        guard n > 0 else { return }
        let p = max(0, min(n - 1, taskCurrentStep + d))
        task.step = p
        task.stepTop = ListWindow.fit(p, top: taskStepWindow.lowerBound, count: n)
    }

    func taskStepScroll(_ d: Int) {
        task.stepTop = ListWindow.clamp(taskStepWindow.lowerBound + d, count: taskSteps.count)
    }

    /// 「N 步 · M 轮对话 · 用时 …」
    var taskProcSummary: String {
        let steps = taskSteps
        // 优先用服务端算的步数，和结果区右上角保持一致
        let tools = task.detail?.usage.steps ?? steps.filter { $0.kind == .step && $0.status != "note" }.count
        let rounds = steps.filter { $0.kind == .you }.count
        var parts = ["\(tools) 步"]
        if rounds > 0 { parts.append("\(rounds) 轮对话") }
        if let last = steps.last?.ts, taskCreated > 0 { parts.append("用时 \(TimeText.duration(last - taskCreated))") }
        return parts.joined(separator: " · ")
    }

    /// 最近两句对话（带上在 taskSteps 里的下标，点一下跳到过程里那一步）。
    /// 已经作为「结果」显示的那句 Agent 回复不再重复成气泡。
    var taskTalk: [(index: Int, step: Step)] {
        let final = (task.detail?.final ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        // 正在跑时，这一轮 Agent 说的话已经一条条列在上面那张卡里，下面不再重复成气泡
        let live = taskKind == .run || taskKind == .wait
        let roundStart = taskRoundStart
        return Array(taskSteps.enumerated()
            .filter { $0.element.kind != .step }
            .suffix(2)
            .filter { !($0.element.kind == .agent && !final.isEmpty && $0.element.text.trimmingCharacters(in: .whitespacesAndNewlines) == final) }
            .filter { !(live && $0.element.kind == .agent && $0.element.ts >= roundStart) }
            .map { ($0.offset, $0.element) })
    }

    func taskStatus() -> (text: String, color: Color) {
        let k = taskKind
        return (k.label, k.color)
    }

    // MARK: 动作

    func taskPrimary() {
        guard let w = taskGate, let c = w.primaryChoice, task.answering == nil else { return }
        answer(w, c)
    }

    func taskSecondary() {
        guard let w = taskGate, let c = w.secondaryChoice, task.answering == nil else { return }
        answer(w, c)
    }

    func taskChoose(_ w: WaitItem, _ choice: String) {
        switch choice {
        case "edit": openEditor(w)
        case "hint":
            task.hintFor = w
            requestFocus(.task)
        default: answer(w, choice)
        }
    }

    func taskCancel() {
        guard let id = task.id, taskKind.isActive else { return }
        Task { @MainActor in
            do {
                let s = try await client.cancel(id)
                task.summary = s
                if let i = tasks.firstIndex(where: { $0.id == id }) { tasks[i] = s }
                flash("已停下这一轮 · 还能接着说")
                reloadTaskSoon()
            } catch {
                handleError(error)
            }
        }
    }

    func taskSend() {
        let v = task.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !v.isEmpty, let id = task.id, !task.sending else { return }
        if let w = task.hintFor {
            task.draft = ""
            task.hintFor = nil
            answer(w, w.noteChoice ?? "hint", note: v)
            return
        }
        task.sending = true
        Task { @MainActor in
            defer { task.sending = false }
            do {
                let s = try await client.input(id, text: v)
                task.draft = ""
                task.summary = s
                task.step = nil
                task.stepTop = nil
                if let i = tasks.firstIndex(where: { $0.id == id }) { tasks[i] = s }
                reloadTaskSoon()
            } catch {
                handleError(error)
            }
        }
    }

    func taskToggleResult() {
        guard !(task.detail?.final ?? "").isEmpty else { return }
        task.resultOpen.toggle()
        if !task.resultOpen { taskScrollBy(-.greatestFiniteMagnitude) }   // 收起后回到顶上，别停在一片空白里
    }

    func taskCopy() {
        let text = task.detail?.final ?? ""
        if text.isEmpty {
            flash("还没有结论可以复制")
        } else {
            copy(text, note: "已复制结论")
        }
    }

    func openDetail(from: DetailOrigin) {
        guard task.id != nil else { return }
        detail = DetailState()
        detail.from = from
        go(.detail(from))
    }

    func backToLauncherFromTask() {
        task.hintFor = nil
        task.optionPick = nil
        armed = nil
        if task.archived {                  // 从归档列表来的：回到归档列表
            go(.launcher)
            launcher.archivedMode = true
            requestFocus(nil)
            return
        }
        go(.launcher)
        if let id = task.id, let i = tasks.firstIndex(where: { $0.id == id }) {
            let w = launcherWindow
            if i >= w.top, i < w.top + w.count { launcher.pick = i }
        }
    }

    // MARK: 键盘

    func taskKey(_ e: KeyEvent) -> Bool {
        if let r = questionKey(e, draft: task.draft) { return r }
        // 改过的文件：⌘D 看 diff，⌘Z 撤销选中的那个（按两次）
        if e.cmd, e.isChar("d") { openDiff(); return true }
        if e.cmd, e.isChar("z"), !e.inInput { undoFile(taskChangeCurrent); return true }
        if armed != nil, !(e.cmd && e.isChar("z")) { armed = nil }
        if task.archived {
            if e.cmd, e.isChar("r"), let id = task.id { restoreTask(id); return true }
            if e.inInput || e.key == .tab || (e.key == .enter && e.plain && !task.proc) { return true }   // 只读：不能接着说
        }
        if e.cmd, e.isChar("m"), task.proc, taskSteps[safe: taskCurrentStep]?.isMemoryNote == true {
            openSettings(tab: .memory)
            return true
        }
        if task.proc, !e.inInput, e.key == .enter, e.plain, let sub = taskSteps[safe: taskCurrentStep]?.sub, !sub.isEmpty {
            openAgent(sub)
            return true
        }
        if e.cmd, e.shift, e.key == .enter { openDetail(from: .task); return true }
        // ⌘. 在输入框里也认（单行输入框用不上它）
        if e.cmd, e.isChar(".") {
            task.proc.toggle()
            // 展开后滚到底：过程面板整个露出来（不然常被切掉半截）
            if task.proc { DispatchQueue.main.async { [weak self] in self?.taskScrollBy(.greatestFiniteMagnitude) } }
            return true
        }
        // 过程开着：↑↓ / ⌘↑↓ 选步骤，窗口跟着选中的那步走；⌘数字 = 眼前第几步。
        // 输入框里也认 ⌘↑↓ 和 ⌘数字（单行输入框用不上它们），↑↓ 留给输入框
        if task.proc, e.cmd, let d = e.digit {
            let w = taskStepWindow
            if d <= w.count { task.step = w.lowerBound + d - 1 }
            return true
        }
        if task.proc, e.key == .up || e.key == .down, e.cmd || (e.plain && !e.inInput) {
            taskStepMove(e.key == .down ? 1 : -1)
            return true
        }
        if e.cmd, e.key == .enter { taskPrimary(); return true }
        if e.cmd, e.key == .delete { taskCancel(); return true }
        if e.inInput {
            if e.key == .escape {
                if task.hintFor != nil { task.hintFor = nil } else { requestFocus(nil) }
                return true
            }
            if e.key == .enter, e.plain { taskSend(); return true }
            return false
        }
        if e.plain, e.key == .delete { taskSecondary(); return true }
        if e.cmd, e.isChar("c") { taskCopy(); return true }
        if e.plain, e.key == .space { taskToggleResult(); return true }
        if e.plain, e.key == .up || e.key == .down {
            taskScrollBy(e.key == .down ? 80 : -80)
            return true
        }
        if e.key == .escape {
            if task.proc { task.proc = false } else { backToLauncherFromTask() }
            return true
        }
        if e.key == .tab || (e.key == .enter && e.plain) {
            requestFocus(.task)
            return true
        }
        return false
    }
}
