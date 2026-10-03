import Foundation

// MARK: - ⑤ 任务详情：过程面板的放大版（左边步骤，最下面一行「现在」；右边看选中的那个）

extension AppStore {
    var detailNodes: [Step] { taskSteps }
    var detailAt: Int { min(max(detail.at, 0), max(0, detailNodes.count - 1)) }
    /// 选中的是最下面那行「现在」（结论 / 正在跑的这一轮）
    var detailLive: Bool { detail.at < 0 || detailNodes.isEmpty }

    /// ↑↓：在步骤和「现在」之间走（「现在」排在最后）
    func detailMove(_ d: Int) {
        let n = detailNodes.count
        guard n > 0 else { return }
        let cur = detail.at < 0 ? n : detail.at
        let i = max(0, min(n, cur + d))
        detail.at = i == n ? -1 : i
    }

    func detailJump(_ i: Int) {
        detail.at = i >= detailNodes.count ? -1 : i
    }

    func detailSend() {
        let v = detail.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !v.isEmpty, let id = task.id else { return }
        detail.draft = ""
        detail.at = -1
        Task { @MainActor in
            do {
                let s = try await client.input(id, text: v)
                task.summary = s
                if let i = tasks.firstIndex(where: { $0.id == id }) { tasks[i] = s }
                reloadTaskSoon()
            } catch {
                detail.draft = v
                handleError(error)
            }
        }
    }

    func detailBack() {
        switch detail.from {
        case .task: go(.task)
        case .queue: go(.queue)
        }
    }

    /// 在「现在」复制结论；回看时复制那一步
    func detailCopy() {
        if detailLive {
            taskCopy()
            return
        }
        let s = detailNodes[detailAt]
        let text = [s.title, s.kind == .step ? s.prettyArgs : s.text, s.out].filter { !$0.isEmpty }.joined(separator: "\n\n")
        copy(text, note: "已复制这一步")
    }

    func detailKey(_ e: KeyEvent) -> Bool {
        if let r = questionKey(e, draft: detail.draft) { return r }
        // 放行 / 拒绝和 ③ 一样：⌘↵ 主选项，⌫ 次选项（输入框里 ⌫ 照常删字）
        if e.cmd, e.key == .enter { taskPrimary(); return true }
        if !e.inInput, e.plain, e.key == .delete { taskSecondary(); return true }
        if e.inInput {
            if e.key == .escape { requestFocus(nil); return true }
            if e.key == .enter, !e.cmd { detailSend(); return true }
            return false
        }
        if e.plain, [.up, .down, .left, .right].contains(e.key) {
            detailMove(e.key == .down || e.key == .right ? 1 : -1)
            return true
        }
        if e.key == .tab { requestFocus(.detail); return true }
        if e.cmd, e.isChar("c") { detailCopy(); return true }
        if e.key == .escape { detailBack(); return true }
        return false
    }
}

// MARK: - 滚轮

extension AppStore {
    /// 窗口式列表（ListWindow）：滚轮一格挪一条，编号跟着重排。
    func handleScroll(rows: Int) {
        if settings != nil { settingsScroll(rows); return }
        if agentView != nil { agentScroll(rows); return }
        switch route {
        case .launcher:
            if launcher.picker { fileScroll(rows) }
            else if launcher.wsPicker { wsScroll(rows) }
            else if launcher.archivedMode { archScroll(rows) }
            else if launcherSlashMode { promptScroll(rows) }
            else if launcherSearchMode { resultScroll(rows) }
            else { launcherScroll(rows) }
        case .queue where queue.expanded:
            queue.top = ListWindow.clamp(queue.top + rows, count: queue.list.count)
        case .task where task.proc && task.stepsHovered:
            taskStepScroll(rows)
        default:
            break
        }
    }
}
