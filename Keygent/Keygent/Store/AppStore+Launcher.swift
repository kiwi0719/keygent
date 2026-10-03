import AppKit

// MARK: - ① 启动器

extension AppStore {
    /// 最近任务：一屏 5 条的窗口，↑↓ / 滚轮挪
    var launcherWindow: (top: Int, count: Int) {
        let top = min(launcher.top, max(0, tasks.count - LauncherState.viewCount))
        return (top, min(LauncherState.viewCount, tasks.count - top))
    }

    var launcherRows: [(index: Int, slot: Int, task: TaskSummary)] {
        let w = launcherWindow
        return (0..<w.count).map { j in (w.top + j, j, tasks[w.top + j]) }
    }

    var pickedTask: TaskSummary? {
        guard let p = launcher.pick, tasks.indices.contains(p) else { return nil }
        return tasks[p]
    }

    func clampLauncher() {
        if let p = launcher.pick, !tasks.indices.contains(p) { launcher.pick = nil }
        launcher.top = max(0, min(launcher.top, tasks.count - LauncherState.viewCount))
    }

    func pickLauncher(_ i: Int) {
        if launcher.pick == i { launcherSubmit() } else { launcher.pick = i }
    }

    func launcherScroll(_ d: Int) {
        launcher.top = max(0, min(tasks.count - LauncherState.viewCount, launcher.top + d))
    }

    // MARK: 搜索（输入以 ? 开头：搜以前所有任务的记录，api.md v1.4）

    var launcherSearchMode: Bool { launcher.query.hasPrefix("?") || launcher.query.hasPrefix("？") }

    var launcherSearchTerms: String {
        String(launcher.query.dropFirst()).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 输入变了：搜索模式下停 0.3 秒再搜，免得每敲一个字发一次
    func launcherQueryChanged() {
        if launcherSlashMode {
            launcher.pick = nil
            loadPrompts()
            launcher.promptPick = 0
            launcher.promptTop = 0
            return
        }
        guard launcherSearchMode else {
            if !launcher.results.isEmpty || launcher.searching { launcher.results = []; launcher.searching = false }
            launcher.resultPick = nil
            return
        }
        launcher.pick = nil
        let q = launcherSearchTerms
        guard !q.isEmpty else { launcher.results = []; launcher.searchedFor = ""; return }
        launcher.searching = true
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard launcherSearchMode, launcherSearchTerms == q else { return }
            do {
                let hits = try await client.search(q, archived: true)
                guard launcherSearchTerms == q else { return }
                launcher.results = hits
                launcher.searchedFor = q
                launcher.resultPick = hits.isEmpty ? nil : 0
                launcher.resultTop = 0
            } catch {
                handleError(error)
            }
            if launcherSearchTerms == q { launcher.searching = false }
        }
    }

    func openSearchHit(_ h: SearchHit) {
        if h.archived {
            openArchivedTask(TaskSummary(id: h.task, title: h.taskTitle, status: "done", note: "", now: "",
                                         created: h.ts, updated: h.ts, waiting: 0, workdir: h.workdir, archived: true))
        } else {
            openTask(id: h.task)
        }
        if h.sub.isEmpty {
            task.focusSeq = h.seq
        } else {                                      // 命中在子 Agent 的账本里：打开主任务，直接进这个子 Agent
            openAgent(h.sub, focusSeq: h.seq)
        }
    }

    var resultWindow: Range<Int> { ListWindow.range(top: launcher.resultTop, count: launcher.results.count) }

    func resultScroll(_ d: Int) {
        launcher.resultTop = ListWindow.clamp(launcher.resultTop + d, count: launcher.results.count)
    }

    private func searchKey(_ e: KeyEvent) -> Bool {
        let n = launcher.results.count
        if e.plain, e.key == .down || e.key == .up {
            guard n > 0 else { return true }
            let cur = launcher.resultPick ?? -1
            let p = max(0, min(n - 1, cur + (e.key == .down ? 1 : -1)))
            launcher.resultPick = p
            launcher.resultTop = ListWindow.fit(p, top: launcher.resultTop, count: n)
            return true
        }
        if e.cmd, let d = e.digit {
            let w = resultWindow
            if d <= w.count { openSearchHit(launcher.results[w.lowerBound + d - 1]) }
            return true
        }
        if e.key == .enter, !e.cmd {
            if let p = launcher.resultPick, launcher.results.indices.contains(p) { openSearchHit(launcher.results[p]) }
            return true
        }
        if e.key == .escape {
            launcher.query = ""
            launcherQueryChanged()
            return true
        }
        return false
    }

    // MARK: 附件

    var recentFiles: [URL] { RecentFiles.load() }

    var fileWindow: Range<Int> { ListWindow.range(top: launcher.fileTop, count: recentFiles.count) }

    func openFilePicker() {
        launcher.wsPicker = false
        launcher.filePick = 0
        launcher.fileTop = 0
        launcher.picker = true
    }

    func fileScroll(_ d: Int) {
        launcher.fileTop = ListWindow.clamp(launcher.fileTop + d, count: recentFiles.count)
    }

    /// 添加文件面板开着：↑↓ 挪高亮、空格 选中/取消高亮那个、⌘数字 = 眼前第几个、↵ / esc 完成
    private func fileKey(_ e: KeyEvent) -> Bool {
        let recent = recentFiles
        let n = recent.count
        if e.cmd, e.shift, e.isChar("o") { openFinder(); return true }
        if e.cmd, let d = e.digit {
            let w = fileWindow
            if d <= w.count {
                launcher.filePick = w.lowerBound + d - 1
                toggleFile(recent[w.lowerBound + d - 1])
            }
            return true
        }
        if e.plain, e.key == .up || e.key == .down {
            guard n > 0 else { return true }
            let p = max(0, min(n - 1, launcher.filePick + (e.key == .down ? 1 : -1)))
            launcher.filePick = p
            launcher.fileTop = ListWindow.fit(p, top: launcher.fileTop, count: n)
            return true
        }
        if e.plain, e.key == .space {
            if recent.indices.contains(launcher.filePick) { toggleFile(recent[launcher.filePick]) }
            return true
        }
        if e.key == .enter || e.key == .escape || (e.cmd && e.isChar("o")) {
            launcher.picker = false
            requestFocus(.launcher)
            return true
        }
        return false
    }

    func isAttached(_ url: URL) -> Bool {
        launcher.attachments.contains { $0.url == url }
    }

    func toggleFile(_ url: URL) {
        if let i = launcher.attachments.firstIndex(where: { $0.url == url }) {
            launcher.attachments.remove(at: i)
        } else {
            launcher.attachments.append(Attachment(source: .file(url)))
        }
    }

    func addFiles(_ urls: [URL]) {
        for u in urls where !isAttached(u) {
            if Workspaces.isDir(u) { setWorkspace(u.path); continue }   // 拖进来的是文件夹：当工作区
            launcher.attachments.append(Attachment(source: .file(u)))
        }
    }

    func removeAttachment(_ a: Attachment) {
        launcher.attachments.removeAll { $0.id == a.id }
    }

    // MARK: 工作区（⌘E）

    var recentWorkspaces: [String] { Workspaces.recent(from: tasks) }

    /// nil = 临时目录。记下来，下次呼出还是它
    func setWorkspace(_ path: String?) {
        launcher.workdir = path
        Workspaces.current = path
        launcher.wsPicker = false
        requestFocus(.launcher)
    }

    /// 「最近用过的」那段的窗口（「临时」固定在最上面，是 ⌘0）
    var wsWindow: Range<Int> { ListWindow.range(top: launcher.wsTop, count: recentWorkspaces.count) }

    func openWorkspacePicker() {
        let recent = recentWorkspaces
        let i = launcher.workdir.flatMap { recent.firstIndex(of: $0) }
        launcher.wsPick = i.map { $0 + 1 } ?? 0
        launcher.wsTop = ListWindow.fit(i ?? 0, top: 0, count: recent.count)
        launcher.picker = false
        launcher.wsPicker = true
    }

    func wsScroll(_ d: Int) {
        launcher.wsTop = ListWindow.clamp(launcher.wsTop + d, count: recentWorkspaces.count)
    }

    private func workspaceKey(_ e: KeyEvent) -> Bool {
        let recent = recentWorkspaces
        if e.cmd, e.isChar("o") { openFolder(); return true }
        if e.cmd, e.isChar("0") { setWorkspace(nil); return true }      // ⌘0 不算 digit（KeyEvent 只认 1–9）
        if e.cmd, let d = e.digit {
            let w = wsWindow
            if d <= w.count { setWorkspace(recent[w.lowerBound + d - 1]) }
            return true
        }
        if e.plain, e.key == .down || e.key == .up {
            let p = max(0, min(recent.count, launcher.wsPick + (e.key == .down ? 1 : -1)))
            launcher.wsPick = p
            if p > 0 { launcher.wsTop = ListWindow.fit(p - 1, top: launcher.wsTop, count: recent.count) }
            return true
        }
        if e.key == .enter {
            let i = launcher.wsPick
            setWorkspace(i == 0 ? nil : recent.indices.contains(i - 1) ? recent[i - 1] : launcher.workdir)
            return true
        }
        if e.key == .escape || (e.cmd && e.isChar("e")) {
            launcher.wsPicker = false
            requestFocus(.launcher)
            return true
        }
        return true                 // 面板开着：别的键都不漏到输入框
    }

    /// 上传全部附件，返回 blob id
    private func uploadAttachments(_ items: [Attachment]) async throws -> [String] {
        var ids: [String] = []
        for a in items {
            switch a.source {
            case .file(let u): ids.append(try await client.uploadFile(u).id)
            case .text(let t): ids.append(try await client.uploadText(t).id)
            }
        }
        RecentFiles.remember(items.compactMap(\.url))
        return ids
    }

    // MARK: 提交

    func launcherSubmit() {
        guard !launcher.submitting else { return }
        let q = launcher.query.trimmingCharacters(in: .whitespacesAndNewlines)
        let items = launcher.attachments

        if let t = pickedTask {
            if q.isEmpty && items.isEmpty {
                openTask(id: t.id, summary: t)
                return
            }
            submit { [self] in
                let ids = try await uploadAttachments(items)
                let s = try await client.input(t.id, text: q.isEmpty ? "（见附件）" : q, attachments: ids)
                return s
            }
        } else if !q.isEmpty || !items.isEmpty {
            createTaskOptimistically(q, items)
        } else if connection == .offline {
            flash("Weaver 没在运行")
        }
    }

    /// 新建任务：不在启动器里转圈，立刻跳到任务页，后台创建好再接上真实 id
    private func createTaskOptimistically(_ q: String, _ items: [Attachment]) {
        let text = q.isEmpty ? "（见附件）" : q
        let tmp = "pending-\(UUID().uuidString)"
        let workdir = launcher.workdir
        let now = Date().timeIntervalSince1970
        var s = TaskState()
        s.id = tmp
        s.summary = TaskSummary(id: tmp, title: text, status: "running", note: "", now: "",
                                created: now, updated: now, waiting: 0, workdir: launcher.workdir ?? "")
        s.loading = true
        task = s
        launcher.query = ""
        launcher.pick = nil
        launcher.attachments = []
        launcher.submitting = true
        go(.task)
        Task { @MainActor in
            defer { launcher.submitting = false }
            do {
                let ids = try await uploadAttachments(items)
                let s = try await client.createTask(text: text, workdir: workdir, attachments: ids)
                if let i = tasks.firstIndex(where: { $0.id == s.id }) { tasks[i] = s } else { tasks.insert(s, at: 0) }
                tasks.sort { $0.updated > $1.updated }
                if task.id == tmp && route == .task { openTask(id: s.id, summary: s) }
                refreshStatusSoon()
            } catch {
                if task.id == tmp {
                    launcher.query = q
                    launcher.attachments = items
                    go(.launcher)
                }
                handleError(error)
            }
        }
    }

    private func submit(_ work: @escaping () async throws -> TaskSummary) {
        launcher.submitting = true
        Task { @MainActor in
            defer { launcher.submitting = false }
            do {
                let s = try await work()
                if let i = tasks.firstIndex(where: { $0.id == s.id }) { tasks[i] = s } else { tasks.insert(s, at: 0) }
                tasks.sort { $0.updated > $1.updated }
                launcher.query = ""
                launcher.pick = nil
                launcher.attachments = []
                openTask(id: s.id, summary: s)
                refreshStatusSoon()
            } catch {
                handleError(error)
            }
        }
    }

    // MARK: 键盘

    func launcherKey(_ e: KeyEvent) -> Bool {
        // 粘贴一大段：不进输入框，变成「长文本」附件（只在按 ⌘V 时看剪贴板）
        if e.cmd, !e.shift, e.isChar("v"), e.inInput, !launcher.picker,
           let s = NSPasteboard.general.string(forType: .string), Attachment.isLongText(s) {
            launcher.attachments.append(Attachment(source: .text(s)))
            return true
        }
        if launcher.wsPicker { return workspaceKey(e) }
        if launcher.picker { return fileKey(e) }
        if launcher.archivedMode { return archivedKey(e) }
        if launcher.promptArgs != nil { return promptArgsKey(e) }
        if launcherSlashMode, !(e.cmd && (e.isChar("o") || e.isChar("e"))) { return slashKey(e) }
        if e.cmd, e.shift, e.isChar("a") { openArchived(); return true }

        if e.cmd, e.shift, e.isChar("o") { openFinder(); return true }
        if e.cmd, e.isChar("o") { openFilePicker(); return true }
        if e.cmd, e.isChar("e") { openWorkspacePicker(); return true }
        if launcherSearchMode { return searchKey(e) }

        let w = launcherWindow
        if e.cmd, let d = e.digit {
            if d <= w.count { launcher.pick = w.top + d - 1 }
            return true
        }

        if e.plain, e.key == .down || e.key == .up {
            guard !tasks.isEmpty else { return true }
            let down = e.key == .down
            let p = launcher.pick.map { max(0, min(tasks.count - 1, $0 + (down ? 1 : -1))) } ?? w.top
            launcher.pick = p
            launcher.top = ListWindow.fit(p, top: launcher.top, count: tasks.count)
            return true
        }

        if e.key == .enter, !e.cmd {
            launcherSubmit()
            return true
        }

        if e.key == .escape {
            if launcher.pick != nil {
                launcher.pick = nil
            } else if !launcher.query.isEmpty {
                launcher.query = ""
            } else {
                hidePanel()
            }
            return true
        }
        return false
    }
}
