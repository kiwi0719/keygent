import AppKit

// 启动器里打 / ：MCP 服务器提供的现成提示词（design/mcp2.md 第四节）。
// 打字筛选，↑↓ / ⌘数字 选，↵ 选中；有参数就在下面一行一个参数，tab 换行，↵ 交出去（新建一个任务）。

extension AppStore {
    var launcherSlashMode: Bool { launcher.query.hasPrefix("/") }

    var promptFilter: String { String(launcher.query.dropFirst()).trimmingCharacters(in: .whitespaces).lowercased() }

    var filteredPrompts: [PromptItem] {
        let q = promptFilter
        guard !q.isEmpty else { return launcher.prompts }
        return launcher.prompts.filter { ($0.command + " " + $0.title + " " + $0.description).lowercased().contains(q) }
    }

    var promptWindow: Range<Int> { ListWindow.range(top: launcher.promptTop, count: filteredPrompts.count) }

    func promptScroll(_ d: Int) {
        launcher.promptTop = ListWindow.clamp(launcher.promptTop + d, count: filteredPrompts.count)
    }

    /// 按当前工作区拉一次（项目级服务器只有信任过的才算）
    func loadPrompts() {
        let key = launcher.workdir ?? ""
        guard launcher.promptsFor != key, !launcher.promptsLoading else { return }
        launcher.promptsLoading = true
        Task { @MainActor in
            defer { launcher.promptsLoading = false }
            do {
                launcher.prompts = try await client.prompts(workdir: launcher.workdir)
                launcher.promptsFor = key
            } catch {
                handleError(error)
            }
        }
    }

    func slashKey(_ e: KeyEvent) -> Bool {
        let list = filteredPrompts, n = list.count
        if e.plain, e.key == .up || e.key == .down {
            guard n > 0 else { return true }
            let p = max(0, min(n - 1, launcher.promptPick + (e.key == .down ? 1 : -1)))
            launcher.promptPick = p
            launcher.promptTop = ListWindow.fit(p, top: launcher.promptTop, count: n)
            return true
        }
        if e.cmd, let d = e.digit {
            let w = promptWindow
            if d <= w.count { choosePrompt(list[w.lowerBound + d - 1]) }
            return true
        }
        if e.key == .enter, !e.cmd {
            if list.indices.contains(launcher.promptPick) { choosePrompt(list[launcher.promptPick]) }
            return true
        }
        if e.key == .escape {
            launcher.query = ""
            launcherQueryChanged()
            return true
        }
        return false
    }

    func choosePrompt(_ p: PromptItem) {
        if p.arguments.isEmpty {
            submitPrompt(p, [:])
            return
        }
        launcher.promptArgs = p
        launcher.argValues = Array(repeating: "", count: p.arguments.count)
        launcher.argCur = 0
        requestFocus(.promptArg(0))
    }

    func promptArgsKey(_ e: KeyEvent) -> Bool {
        guard let p = launcher.promptArgs else { return false }
        let n = p.arguments.count
        if e.key == .escape {
            launcher.promptArgs = nil
            requestFocus(.launcher)
            return true
        }
        if e.key == .tab || (e.plain && !e.inInput && (e.key == .up || e.key == .down)) {
            let back = e.shift || e.key == .up
            launcher.argCur = (launcher.argCur + (back ? -1 : 1) + n) % n
            requestFocus(.promptArg(launcher.argCur))
            return true
        }
        if e.key == .enter {
            // 不是最后一个：↵ 去下一个；最后一个（或 ⌘↵）：必填的都填了就交出去
            if !e.cmd, launcher.argCur < n - 1 {
                launcher.argCur += 1
                requestFocus(.promptArg(launcher.argCur))
                return true
            }
            if let i = p.arguments.indices.first(where: {
                p.arguments[$0].required && launcher.argValues[$0].trimmingCharacters(in: .whitespaces).isEmpty }) {
                launcher.argCur = i
                requestFocus(.promptArg(i))
                flash("「\(p.arguments[i].name)」必须填")
                return true
            }
            var args: [String: String] = [:]
            for (i, a) in p.arguments.enumerated() where !launcher.argValues[i].isEmpty { args[a.name] = launcher.argValues[i] }
            submitPrompt(p, args)
            return true
        }
        return false
    }

    private func submitPrompt(_ p: PromptItem, _ args: [String: String]) {
        guard !launcher.submitting else { return }
        launcher.submitting = true
        let workdir = launcher.workdir
        Task { @MainActor in
            defer { launcher.submitting = false }
            do {
                let s = try await client.createTask(prompt: p, arguments: args, workdir: workdir)
                if let i = tasks.firstIndex(where: { $0.id == s.id }) { tasks[i] = s } else { tasks.insert(s, at: 0) }
                launcher.query = ""
                launcher.promptArgs = nil
                openTask(id: s.id, summary: s)
                refreshStatusSoon()
            } catch {
                handleError(error)
            }
        }
    }
}
