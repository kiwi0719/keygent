import AppKit

// 设置页的权限、记忆两页（design/keygent-gaps.md 第三、四节）。按键习惯同 Skills 页：
// ↑↓ / ⌘数字 选，↵ 打开，⌘N 新建，⌘⌫ 按两次删除 / 撤销，编辑器里 ⌘↵ 保存、esc 返回。

struct PermissionsPageState {
    enum Row: Equatable {
        case rule(PermissionList.Rule)
        case project(PermissionList.Project)
    }

    var list: PermissionList? = nil
    var cur = 0
    var top = 0
    /// 默认规则（改不了的那几条）展开了没有
    var showBuiltin = false

    /// 能选中的行：先“总是允许”，再信任过的项目
    var rows: [Row] {
        guard let l = list else { return [] }
        return l.rules.map { .rule($0) } + l.projects.map { .project($0) }
    }
}

struct MemoryPageState {
    struct Row: Equatable {
        var scope: String
        var root: String
        var label: String
        var item: MemoryList.Item
    }

    var list: MemoryList? = nil
    var cur = 0
    var top = 0
    /// 正在编辑哪一条：nil = 列表；name 为空 = 新建
    var editing: (scope: String, root: String, label: String, name: String)? = nil
    var text = ""
    var original = ""

    var rows: [Row] {
        (list?.scopes ?? []).flatMap { s in s.items.map { Row(scope: s.scope, root: s.root, label: s.label, item: $0) } }
    }

    var dirty: Bool { editing != nil && text != original }

    static let typeLabel = ["user": "你是谁", "feedback": "纠正", "project": "项目", "reference": "资料"]
}

extension AppStore {
    // MARK: 权限页

    func loadPermissions() {
        guard let id = settings?.id else { return }
        Task { @MainActor in
            do {
                let l = try await client.permissions()
                guard stillHere(id, .permissions) else { return }
                // 先算好再一次写回：同一个表达式里边改边读 settings 会触发独占访问检查
                var p = settings!.permissions
                p.list = l
                let n = p.rows.count
                p.cur = max(0, min(p.cur, n - 1))
                p.top = ListWindow.fit(p.cur, top: p.top, count: n)
                settings?.permissions = p
            } catch {
                if stillHere(id, .permissions) { settings?.error = (error as? LocalizedError)?.errorDescription }
            }
        }
    }

    func permissionsKey(_ e: KeyEvent) -> Bool {
        guard let p = settings?.permissions else { return false }
        let rows = p.rows, n = rows.count
        if e.plain, e.key == .up || e.key == .down {
            guard n > 0 else { return true }
            let c = max(0, min(n - 1, p.cur + (e.key == .down ? 1 : -1)))
            settings?.permissions.cur = c
            settings?.permissions.top = ListWindow.fit(c, top: p.top, count: n)
            settings?.armed = nil
            return true
        }
        if e.cmd, let d = e.digit {
            let i = ListWindow.clamp(p.top, count: n) + d - 1
            if i < n { settings?.permissions.cur = i; settings?.armed = nil }
            return true
        }
        if e.cmd, e.key == .delete, n > 0 {
            switch rows[min(p.cur, n - 1)] {
            case .rule(let r):
                confirmDelete(r.key) {
                    submit({ [client] in try await client.revokeRule(task: r.task, key: r.key) }, done: { [weak self] in
                        self?.flash("已撤销「\(r.key)」· 下次会再问你")
                        self?.loadPermissions()
                    })
                }
            case .project(let pr):
                confirmDelete(Workspaces.name(pr.root)) {
                    submit({ [client] in try await client.forgetProject(pr.root) }, done: { [weak self] in
                        self?.flash(pr.state == "denied" ? "已清除，下次会再问你" : "已不再信任 \(Workspaces.name(pr.root))，下次会再问你")
                        self?.loadPermissions()
                    })
                }
            }
            return true
        }
        if e.plain, e.key == .space { settings?.permissions.showBuiltin.toggle(); return true }
        if e.key == .escape { settingsEscapeAll(); return true }
        return false
    }

    // MARK: 记忆页

    func loadMemories(select: (scope: String, root: String, name: String)? = nil) {
        guard let id = settings?.id else { return }
        Task { @MainActor in
            do {
                let l = try await client.memories()
                guard stillHere(id, .memory) else { return }
                var m = settings!.memory
                m.list = l
                let rows = m.rows
                var cur = m.cur
                if let s = select, let i = rows.firstIndex(where: { $0.scope == s.scope && $0.root == s.root && $0.item.name == s.name }) {
                    cur = i
                }
                m.cur = max(0, min(cur, rows.count - 1))
                m.top = ListWindow.fit(m.cur, top: m.top, count: rows.count)
                settings?.memory = m
            } catch {
                if stillHere(id, .memory) { settings?.error = (error as? LocalizedError)?.errorDescription }
            }
        }
    }

    func memoryKey(_ e: KeyEvent) -> Bool {
        guard let m = settings?.memory else { return false }
        if m.editing != nil { return memoryEditKey(e) }
        let rows = m.rows, n = rows.count
        if e.plain, e.key == .up || e.key == .down {
            guard n > 0 else { return true }
            let c = max(0, min(n - 1, m.cur + (e.key == .down ? 1 : -1)))
            settings?.memory.cur = c
            settings?.memory.top = ListWindow.fit(c, top: m.top, count: n)
            settings?.armed = nil
            return true
        }
        if e.cmd, let d = e.digit {
            let i = ListWindow.clamp(m.top, count: n) + d - 1
            if i < n { settings?.memory.cur = i; settings?.armed = nil }
            return true
        }
        let row = n > 0 ? rows[min(m.cur, n - 1)] : nil
        if e.cmd, e.isChar("n") {
            // 新建在选中那一条的范围里（项目级 / 用户级）；没有记忆时是用户级
            let scope = row?.scope ?? "user", root = row?.root ?? "", label = row?.label ?? "所有任务"
            openMemoryEditor(scope: scope, root: root, label: label, name: "",
                             text: settings?.memory.list?.template ?? "---\nname: \ndescription: \ntype: feedback\n---\n\n")
            return true
        }
        if e.key == .enter, !e.cmd, let row {
            let id = settings!.id
            Task { @MainActor in
                do {
                    let d = try await client.memory(row.item.name, scope: row.scope, root: row.root)
                    guard stillHere(id, .memory), settings?.memory.editing == nil else { return }
                    openMemoryEditor(scope: row.scope, root: row.root, label: row.label, name: d.name, text: d.text)
                } catch {
                    if stillHere(id, .memory) { settings?.error = (error as? LocalizedError)?.errorDescription }
                }
            }
            return true
        }
        if e.cmd, e.key == .delete, let row {
            confirmDelete(row.item.name) {
                submit({ [client] in try await client.memoryRemove(row.item.name, scope: row.scope, root: row.root) },
                       done: { [weak self] in
                    self?.flash("已删除 \(row.item.name)（在 memory/.archive 里还能找回）")
                    self?.loadMemories()
                })
            }
            return true
        }
        if e.key == .escape { settingsEscapeAll(); return true }
        return false
    }

    private func openMemoryEditor(scope: String, root: String, label: String, name: String, text: String) {
        settings?.memory.editing = (scope, root, label, name)
        settings?.memory.text = text
        settings?.memory.original = text
        settings?.error = nil
        settings?.armed = nil
        requestFocus(.settingsEditor)
    }

    private func memoryEditKey(_ e: KeyEvent) -> Bool {
        guard let ed = settings?.memory.editing else { return false }
        if e.cmd, e.key == .enter {
            let text = settings!.memory.text
            submit({ [client] in
                ed.name.isEmpty ? try await client.memoryCreate(scope: ed.scope, root: ed.root, text: text)
                    : try await client.memorySave(ed.name, scope: ed.scope, root: ed.root, text: text)
            }, done: { [weak self] final in
                guard let self, self.settings?.memory.editing?.name == ed.name else { return }
                self.flash(ed.name.isEmpty ? "已记下 \(final) · 下个任务开始时用上" : "已保存 \(final)")
                self.closeMemoryEditor()
                self.loadMemories(select: (ed.scope, ed.root, final))
            })
            return true
        }
        if e.key == .escape {
            confirmDiscard(dirty: settings?.memory.dirty == true) { closeMemoryEditor() }
            return true
        }
        return false
    }

    private func closeMemoryEditor() {
        settings?.memory.editing = nil
        settings?.error = nil
        settings?.armed = nil
        requestFocus(nil)
    }
}
