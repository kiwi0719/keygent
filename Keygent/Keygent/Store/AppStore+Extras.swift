import AppKit

// design/keygent-gaps.md：改过的文件 / diff / 撤销、子 Agent 的过程、归档的任务。

extension AppStore {
    // MARK: 改过的文件

    var taskChanges: [FileChange] { task.detail?.changes ?? [] }
    var taskTodos: [TodoItem] { task.detail?.todos ?? [] }

    var taskChangeCurrent: FileChange? {
        let c = taskChanges
        return c.isEmpty ? nil : c[min(task.changePick, c.count - 1)]
    }

    /// ⌘D：打开选中的那个文件的 diff（↑↓ 在弹层里换文件）
    func openDiff(_ path: String? = nil) {
        guard let id = task.id, !taskChanges.isEmpty else {
            flash("这个任务还没改过文件")
            return
        }
        let files = taskChanges
        let cur = path.flatMap { p in files.firstIndex { $0.path == p } } ?? min(task.changePick, files.count - 1)
        diffSheet = DiffSheetState(task: id, archived: task.archived, files: files, cur: cur)
        requestFocus(nil)
        loadDiff()
    }

    private func loadDiff() {
        guard let d = diffSheet, d.files.indices.contains(d.cur) else { return }
        let f = d.files[d.cur]
        guard d.diffs[f.path] == nil else { return }
        Task { @MainActor in
            do {
                let r = try await client.fileDiff(d.task, path: f.path, archived: d.archived)
                guard diffSheet?.task == d.task else { return }
                diffSheet?.diffs[f.path] = r
            } catch {
                diffSheet?.error = (error as? LocalizedError)?.errorDescription
            }
        }
    }

    func diffKey(_ e: KeyEvent) -> Bool {
        guard let d = diffSheet else { return false }
        if e.key == .escape || (e.cmd && e.isChar("d")) {
            diffSheet = nil
            armed = nil
            return true
        }
        if e.plain, e.key == .up || e.key == .down || e.key == .left || e.key == .right {
            let n = d.files.count
            let step = (e.key == .down || e.key == .right) ? 1 : -1
            diffSheet?.cur = (d.cur + step + n) % n
            task.changePick = diffSheet!.cur
            armed = nil
            loadDiff()
            return true
        }
        if e.cmd, let n = e.digit, n <= d.files.count {
            diffSheet?.cur = n - 1
            task.changePick = n - 1
            loadDiff()
            return true
        }
        if e.cmd, e.isChar("z") {
            undoFile(d.files[d.cur])
            return true
        }
        armed = nil
        return true                    // 弹层开着：别的键不漏到下面
    }

    /// ⌘Z：撤销选中的那个文件。第一次只提示，第二次才真撤销
    func undoFile(_ f: FileChange?) {
        guard let f, let id = task.id else { return }
        if task.archived { flash("归档的任务不能撤销，先 ⌘R 恢复"); return }
        if f.undone { flash("\(f.rel) 已经撤销过了"); return }
        if !f.canUndo { flash("撤销不了：\(f.why)"); return }
        guard armed == "undo:\(f.path)" else {
            armed = "undo:\(f.path)"
            return
        }
        armed = nil
        Task { @MainActor in
            do {
                let changes = try await client.undo(id, path: f.path)
                guard task.id == id else { return }
                task.detail?.changes = changes
                if diffSheet?.task == id {
                    diffSheet?.files = changes
                    diffSheet?.diffs[f.path] = nil
                    loadDiff()
                }
                flash("已撤销 \(f.rel) · 它下一步会知道")
                reloadTaskSoon()
            } catch {
                handleError(error)
            }
        }
    }

    // MARK: 子 Agent 的过程

    func openAgent(_ sub: String, focusSeq: Int? = nil) {
        guard let id = task.id else { return }
        agentView = AgentViewState(task: id, sub: sub, archived: task.archived, focusSeq: focusSeq)
        requestFocus(nil)
        loadAgent()
        agentTimer?.invalidate()
        // 子 Agent 的事件不单独推：在跑时每 2 秒拉一次，只在这一层开着时
        agentTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self, let a = self.agentView else { self?.agentTimer?.invalidate(); return }
            if a.detail?.status == "running" || a.detail?.status == "waiting" { self.loadAgent() }
        }
    }

    func closeAgent() {
        agentView = nil
        agentTimer?.invalidate()
        agentTimer = nil
    }

    private func loadAgent() {
        guard let a = agentView else { return }
        Task { @MainActor in
            do {
                let d = try await client.agent(a.task, sub: a.sub, archived: a.archived)
                guard agentView?.sub == a.sub else { return }
                agentView?.detail = d
                if let seq = agentView?.focusSeq, let i = d.steps.lastIndex(where: { $0.seq <= seq }) {
                    agentView?.step = i
                    agentView?.top = ListWindow.fit(i, top: d.steps.count, count: d.steps.count)
                    agentView?.focusSeq = nil
                }
            } catch {
                agentView?.error = (error as? LocalizedError)?.errorDescription
            }
        }
    }

    var agentSteps: [Step] { agentView?.detail?.steps ?? [] }

    var agentCurrentStep: Int {
        let n = agentSteps.count
        guard n > 0 else { return 0 }
        return min(agentView?.step ?? n - 1, n - 1)
    }

    var agentStepWindow: Range<Int> {
        let n = agentSteps.count
        return ListWindow.range(top: agentView?.top ?? n, count: n)
    }

    func agentScroll(_ d: Int) {
        agentView?.top = ListWindow.clamp(agentStepWindow.lowerBound + d, count: agentSteps.count)
    }

    func agentKey(_ e: KeyEvent) -> Bool {
        guard agentView != nil else { return false }
        let n = agentSteps.count
        if e.key == .escape { closeAgent(); return true }
        if e.cmd, let d = e.digit {
            let w = agentStepWindow
            if d <= w.count { agentView?.step = w.lowerBound + d - 1 }
            return true
        }
        if e.key == .up || e.key == .down {
            guard n > 0 else { return true }
            let p = max(0, min(n - 1, agentCurrentStep + (e.key == .down ? 1 : -1)))
            agentView?.step = p
            agentView?.top = ListWindow.fit(p, top: agentStepWindow.lowerBound, count: n)
            return true
        }
        return true
    }

    // MARK: 归档的任务（启动器里 ⌘⇧A）

    func openArchived() {
        launcher.archivedMode = true
        launcher.archPick = 0
        launcher.archTop = 0
        launcher.archLoading = true
        launcher.pick = nil
        Task { @MainActor in
            defer { launcher.archLoading = false }
            do {
                launcher.archived = try await client.archivedTasks()
            } catch {
                handleError(error)
            }
        }
    }

    func closeArchived() {
        launcher.archivedMode = false
        requestFocus(.launcher)
    }

    var archWindow: Range<Int> { ListWindow.range(top: launcher.archTop, count: launcher.archived.count) }

    func archScroll(_ d: Int) {
        launcher.archTop = ListWindow.clamp(launcher.archTop + d, count: launcher.archived.count)
    }

    func archivedKey(_ e: KeyEvent) -> Bool {
        let n = launcher.archived.count
        if e.key == .escape || (e.cmd && e.shift && e.isChar("a")) { closeArchived(); return true }
        if e.plain, e.key == .up || e.key == .down {
            guard n > 0 else { return true }
            let p = max(0, min(n - 1, launcher.archPick + (e.key == .down ? 1 : -1)))
            launcher.archPick = p
            launcher.archTop = ListWindow.fit(p, top: launcher.archTop, count: n)
            return true
        }
        if e.cmd, let d = e.digit {
            let w = archWindow
            if d <= w.count { openArchivedTask(launcher.archived[w.lowerBound + d - 1]) }
            return true
        }
        if e.key == .enter, launcher.archived.indices.contains(launcher.archPick) {
            openArchivedTask(launcher.archived[launcher.archPick])
            return true
        }
        if e.cmd, e.isChar("r"), launcher.archived.indices.contains(launcher.archPick) {
            restoreTask(launcher.archived[launcher.archPick].id)
            return true
        }
        return true
    }

    func openArchivedTask(_ s: TaskSummary) {
        var st = TaskState()
        st.id = s.id
        st.summary = s
        st.archived = true
        task = st
        go(.task)
        loadTask(s.id)
    }

    /// ⌘R：恢复归档的任务，恢复后就是普通任务页
    func restoreTask(_ id: String) {
        Task { @MainActor in
            do {
                let s = try await client.restore(id)
                launcher.archived.removeAll { $0.id == id }
                if let i = tasks.firstIndex(where: { $0.id == s.id }) { tasks[i] = s } else { tasks.insert(s, at: 0) }
                tasks.sort { $0.updated > $1.updated }
                launcher.archivedMode = false
                flash("已恢复「\(s.title)」")
                openTask(id: s.id, summary: s)
            } catch {
                handleError(error)
            }
        }
    }
}
