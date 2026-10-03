import AppKit

// 设置页（design/settings.md）：模型 · MCP · Skills 三个分页，全键盘。
// weaverd 在运行时一切经 /v1/settings/*；没在运行时只能改模型（直接写 ~/.weaver/.env，同以前）。

enum SettingsTab: Int, CaseIterable {
    case model, mcp, skills, permissions, memory

    var title: String { ["模型", "MCP", "Skills", "权限", "记忆"][rawValue] }
}

/// 高级项：接口里的名字、显示的名字、没连上 weaverd 时的默认值说明（和 weaver/settings/model.py 一致）
enum AdvancedItem {
    static let keys = ["context_window", "compact_at", "max_running", "mcp_timeout"]
    static let labels = ["context_window": "上下文窗口", "compact_at": "压缩阈值", "max_running": "同时跑几个任务",
                         "mcp_timeout": "MCP 连接超时"]
    static let env = ["context_window": "WEAVER_CONTEXT_WINDOW", "compact_at": "WEAVER_COMPACT_AT",
                      "max_running": "WEAVER_MAX_RUNNING", "mcp_timeout": "WEAVER_MCP_TIMEOUT"]
    static let fallbackDefaults = ["context_window": "自动（按模型）", "compact_at": "自动", "max_running": "4",
                                   "mcp_timeout": "10"]
    static let units = ["context_window": "token", "compact_at": "token", "max_running": "个", "mcp_timeout": "秒"]
}

struct McpPageState {
    enum Mode: Equatable { case list, edit(String), addMenu, paste, presets, presetFill(String), importing }

    var list: McpList? = nil
    var cur = 0
    var top = 0
    var mode: Mode = .list
    // 编辑器
    var editName = ""
    var editText = ""
    var originalName = ""
    var originalText = ""
    var editTools: [String] = []
    // 添加：三选一
    var menuPick = 0
    // 粘贴
    var pasteText = ""
    var parsed: McpParsed? = nil
    var parseError: String? = nil
    // 常用服务器
    var presets: [McpPreset] = []
    var filter = ""
    var presetPick = 0
    var presetTop = 0
    var needs: [String: String] = [:]
    var needPick = 0
    // 从 Claude 导入
    var sources: [ImportSource] = []
    var importLoaded = false
    var checked: Set<String> = []
    var importPick = 0
    var importTop = 0
    // 设备码登录：列表上方那一块（码已经复制好了）
    var device: LoginInfo? = nil
    var deviceFor: String? = nil

    var servers: [McpServerItem] { list?.servers ?? [] }

    var dirty: Bool {
        switch mode {
        case .edit: return editText != originalText || editName != originalName
        case .paste: return !pasteText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        default: return false
        }
    }

    var filteredPresets: [McpPreset] {
        let q = filter.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return presets }
        return presets.filter { ($0.title + $0.name + $0.description).lowercased().contains(q) }
    }

    /// 导入列表摊平：(来源, 名字, 配置)
    var importRows: [(source: String, name: String, config: JSONValue)] {
        sources.flatMap { s in s.servers.keys.sorted().map { (s.from, $0, s.servers[$0]!) } }
    }

    static func importKey(_ source: String, _ name: String) -> String { "\(source)#\(name)" }
}

struct SkillsPageState {
    var list: SkillList? = nil
    var cur = 0
    var top = 0
    /// nil = 列表；"" = 新建；名字 = 编辑这个
    var editing: String? = nil
    var text = ""
    var original = ""
    var editable = true
    var source = "weaver"
    var files: [String] = []

    var skills: [SkillItem] { list?.skills ?? [] }
    var dirty: Bool { editing != nil && editable && text != original }

    static let template = "---\nname: \ndescription: \n---\n\n"
}

struct SettingsState {
    var tab: SettingsTab = .model
    /// weaverd 没在运行：只有模型页，直接写 .env
    var offline = false
    var model = ModelSettingsState()
    var advanced: [String: String] = [:]
    var advancedDefaults = AdvancedItem.fallbackDefaults
    /// 模型页里焦点在第几行：0 地址 1 模型 2 Key 3… 高级项
    var row = 0
    var mcp = McpPageState()
    var skills = SkillsPageState()
    var permissions = PermissionsPageState()
    var memory = MemoryPageState()
    var error: String? = nil
    /// 等第二次确认的操作：“delete:名字” / “discard”
    var armed: String? = nil
    var saving = false
    /// 这一次打开的设置页：异步请求回来时对一下，关了又开、换了一份就不再往里写
    var id = UUID()
    /// 高级项打开时的值：保存时只发改过的，别把命令行 / 手动改的其它项盖掉
    var advancedOriginal: [String: String] = [:]

    static let rowCount = 3 + AdvancedItem.keys.count
}

extension AppStore {
    // MARK: 打开 / 关闭 / 切换

    func openSettings(tab: SettingsTab? = nil) {
        editor = nil
        var st = SettingsState()
        let env = EnvFile.load(DaemonService.envFile)
        st.model = ModelSettingsState(env: env)
        for k in AdvancedItem.keys { st.advanced[k] = env[AdvancedItem.env[k]!] ?? "" }
        st.advancedOriginal = st.advanced
        st.offline = connection != .online
        st.tab = st.offline ? .model : (tab ?? lastSettingsTab)
        settings = st
        settingsEnter(st.tab)
        if !st.offline {
            Task { @MainActor in
                if let m = try? await client.settingsModel(), settings != nil {
                    settings?.advancedDefaults = m.defaults
                }
            }
        }
    }

    func closeSettings() {
        if let t = settings?.tab { lastSettingsTab = t }
        settings = nil
        stopMcpPolling()
        requestFocus(route == .launcher ? .launcher : nil)
    }

    func settingsSwitch(by delta: Int) {
        guard var st = settings else { return }
        if st.offline {
            settings?.error = "Weaver 没在运行，现在只能改模型"
            return
        }
        let n = SettingsTab.allCases.count
        st.tab = SettingsTab(rawValue: (st.tab.rawValue + delta + n) % n)!
        st.error = nil
        st.armed = nil
        settings = st
        settingsEnter(st.tab)
    }

    /// 进到某个分页：拉数据、放焦点
    private func settingsEnter(_ tab: SettingsTab) {
        stopMcpPolling()
        switch tab {
        case .model:
            requestFocus(.settingsRow(settings?.row ?? 0))
        case .mcp:
            requestFocus(mcpFocus)
            loadMcp()
            startMcpPolling()
        case .skills:
            requestFocus(settings?.skills.editing != nil ? .settingsEditor : nil)
            loadSkills()
        case .permissions:
            requestFocus(nil)
            loadPermissions()
        case .memory:
            requestFocus(settings?.memory.editing != nil ? .settingsEditor : nil)
            loadMemories()
        }
    }

    // MARK: 按键

    func settingsKey(_ e: KeyEvent) -> Bool {
        guard let st = settings else { return false }
        if e.cmd, e.isChar(",") {
            let dirty = (st.tab == .mcp && st.mcp.dirty) || (st.tab == .skills && st.skills.dirty)
                || (st.tab == .memory && st.memory.dirty)
            confirmDiscard(dirty: dirty) { settingsEscapeAll() }
            return true
        }
        if e.cmd, e.isChar("[") { settingsSwitch(by: -1); return true }
        if e.cmd, e.isChar("]") { settingsSwitch(by: 1); return true }
        if e.ctrl, e.key == .tab { settingsSwitch(by: e.shift ? -1 : 1); return true }
        let handled: Bool
        switch st.tab {
        case .model: handled = modelKey(e)
        case .mcp: handled = mcpKey(e)
        case .skills: handled = skillsKey(e)
        case .permissions: handled = permissionsKey(e)
        case .memory: handled = memoryKey(e)
        }
        // 按了别的键：取消“再按一次确认”
        if !handled, settings?.armed != nil, e.key != .escape { settings?.armed = nil }
        return handled
    }

    func settingsEscapeAll() {
        if settings?.model.firstRun == true, settings?.offline == true { hidePanel() } else { closeSettings() }
    }

    /// 编辑器里 esc：有改动先提示，再按一次才放弃
    func confirmDiscard(dirty: Bool, back: () -> Void) {
        if dirty, settings?.armed != "discard" {
            settings?.armed = "discard"
            return
        }
        settings?.armed = nil
        settings?.error = nil
        back()
    }

    /// ⌘⌫：第一次只是提示，第二次才真删
    func confirmDelete(_ name: String, run: () -> Void) {
        if settings?.armed == "delete:\(name)" {
            settings?.armed = nil
            run()
        } else {
            settings?.armed = "delete:\(name)"
        }
    }

    /// 发一个改东西的请求：同一时间只发一个（连按 ⌘↵ 不会发两次）；回来时设置页已经关了、换了一份、
    /// 切了分页，就什么都不做（不抢焦点、不改别的页）。出错写在底栏。
    func submit<T>(_ request: @escaping () async throws -> T, done: @escaping (T) -> Void) {
        guard let st = settings, !st.saving else { return }
        let id = st.id, tab = st.tab
        settings?.saving = true
        Task { @MainActor in
            let result: Result<T, Error>
            do { result = .success(try await request()) } catch { result = .failure(error) }
            guard settings?.id == id else { return }
            settings?.saving = false
            guard settings?.tab == tab else { return }
            switch result {
            case .success(let v): done(v)
            case .failure(let e): settings?.error = (e as? LocalizedError)?.errorDescription ?? e.localizedDescription
            }
        }
    }

    /// 读东西的请求：回来时还是同一份、同一个分页才用
    func stillHere(_ id: UUID, _ tab: SettingsTab) -> Bool { settings?.id == id && settings?.tab == tab }

    // MARK: 模型页

    private func modelKey(_ e: KeyEvent) -> Bool {
        guard settings != nil else { return false }
        if e.plain, e.key == .up || e.key == .down || e.key == .tab {
            let down = e.key == .down || (e.key == .tab && !e.shift)
            let r = max(0, min(SettingsState.rowCount - 1, settings!.row + (down ? 1 : -1)))
            settings!.row = r
            requestFocus(.settingsRow(r))
            return true
        }
        if e.key == .enter, !e.shift, !e.cmd { saveModelSettings(); return true }
        if e.key == .escape { settingsEscapeAll(); return true }
        return false
    }

    func saveModelSettings() {
        guard let st = settings, !st.saving else { return }
        let advanced = st.advanced.mapValues { $0.trimmingCharacters(in: .whitespaces) }
        if let bad = AdvancedItem.keys.first(where: { k in
            let v = advanced[k] ?? ""
            return !v.isEmpty && (Int(v) ?? 0) <= 0
        }) {
            settings?.error = "\(AdvancedItem.labels[bad]!)要填正整数，或者留空用默认"
            return
        }
        // 只发改过的：打开之后别人（命令行、手动）改的其它项不被这一份旧值盖掉
        let changed = advanced.filter { k, v in v != (st.advancedOriginal[k] ?? "") }
        if st.offline || connection != .online {
            saveModelLocally(st, changed)
            return
        }
        let typed = st.model.key.trimmingCharacters(in: .whitespaces)
        submit({ [client] in
            do {
                try await client.saveModel(url: st.model.url.trimmingCharacters(in: .whitespaces),
                                           model: st.model.model.trimmingCharacters(in: .whitespaces),
                                           key: typed.isEmpty ? nil : typed, advanced: changed)
                return true
            } catch let e as APIError where e.code == "not_found" {
                return false                       // 连着的是还没有设置接口的旧 weaverd：直接写本地 .env
            }
        }, done: { [weak self] ok in
            guard let self else { return }
            if ok { self.finishModelSave() } else { self.saveModelLocally(st, changed) }
        })
    }

    /// 不经 weaverd，直接写 ~/.weaver/.env（weaverd 没在运行、或者是旧版本时）
    private func saveModelLocally(_ st: SettingsState, _ changed: [String: String]) {
        var env = EnvFile.load(DaemonService.envFile)
        if let err = st.model.apply(to: &env) { settings?.error = err; return }
        for (k, v) in changed { env.set(AdvancedItem.env[k]!, v) }
        do {
            try env.write(to: DaemonService.envFile)
        } catch {
            settings?.error = "保存失败：\(error.localizedDescription)"
            return
        }
        finishModelSave()
    }

    private func finishModelSave() {
        closeSettings()
        restartDaemon()
        flash("已保存，正在重启 Weaver")
    }

    // MARK: MCP 页

    var mcpFocus: InputField? {
        switch settings?.mcp.mode {
        case .edit: return .settingsEditor
        case .paste: return .settingsEditor
        case .presets: return .settingsFilter
        case .presetFill: return .settingsNeed(settings?.mcp.needPick ?? 0)
        default: return nil
        }
    }

    private func setMcpMode(_ m: McpPageState.Mode) {
        settings?.mcp.mode = m
        settings?.error = nil
        settings?.armed = nil
        requestFocus(mcpFocus)
    }

    func loadMcp(select name: String? = nil) {
        Task { @MainActor in
            do {
                let l = try await client.mcpList()
                guard settings != nil else { return }
                // 先算好再写：同一个表达式里边改 settings 边读它，Swift 的独占访问检查会直接崩
                let n = l.servers.count
                var cur = settings!.mcp.cur
                if let name, let i = l.servers.firstIndex(where: { $0.name == name }) { cur = i }
                cur = max(0, min(cur, n - 1))
                let top = ListWindow.fit(cur, top: settings!.mcp.top, count: n)
                settings?.mcp.list = l
                settings?.mcp.cur = cur
                settings?.mcp.top = top
                if let who = settings?.mcp.deviceFor, l.servers.first(where: { $0.name == who })?.status != "logging_in" {
                    let ok = l.servers.first(where: { $0.name == who }).map { ["connected", "connecting"].contains($0.status) }
                    settings?.mcp.device = nil
                    settings?.mcp.deviceFor = nil
                    if ok == true { flash("\(who) 登录好了") }
                }
            } catch {
                settings?.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            }
        }
    }

    func startMcpPolling() {
        stopMcpPolling()
        mcpTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self, self.settings?.tab == .mcp, self.settings?.mcp.mode == .list else { return }
            self.loadMcp()
        }
    }

    func stopMcpPolling() {
        mcpTimer?.invalidate()
        mcpTimer = nil
    }

    private func mcpKey(_ e: KeyEvent) -> Bool {
        guard let m = settings?.mcp else { return false }
        switch m.mode {
        case .list: return mcpListKey(e, m)
        case .edit(let name): return mcpEditKey(e, name)
        case .addMenu: return mcpMenuKey(e)
        case .paste: return mcpPasteKey(e)
        case .presets: return mcpPresetsKey(e)
        case .presetFill(let id): return mcpFillKey(e, id)
        case .importing: return mcpImportKey(e)
        }
    }

    private func mcpListKey(_ e: KeyEvent, _ m: McpPageState) -> Bool {
        let n = m.servers.count
        if e.plain, e.key == .up || e.key == .down {
            guard n > 0 else { return true }
            let c = max(0, min(n - 1, m.cur + (e.key == .down ? 1 : -1)))
            settings?.mcp.cur = c
            settings?.mcp.top = ListWindow.fit(c, top: m.top, count: n)
            settings?.armed = nil
            return true
        }
        if e.cmd, let d = e.digit {
            let i = ListWindow.clamp(m.top, count: n) + d - 1
            if i < n { settings?.mcp.cur = i; settings?.armed = nil }
            return true
        }
        if e.cmd, e.isChar("n") { openMcpAdd(); return true }
        let item = n > 0 ? m.servers[min(m.cur, n - 1)] : nil
        if e.cmd, e.isChar("l"), let item { startMcpLogin(item.name); return true }
        if e.cmd, e.isChar("o") {
            let url = m.device?.verificationURI ?? item?.login?.verificationURI ?? item?.login?.url
            if let url, let u = URL(string: url) { NSWorkspace.shared.open(u) }
            return true
        }
        if e.key == .escape, m.device != nil {
            settings?.mcp.device = nil             // 只是收起来，登录在后台照样等你
            settings?.mcp.deviceFor = nil
            return true
        }
        if e.key == .enter, !e.cmd, let item { openMcpEditor(item); return true }
        if e.cmd, e.isChar("r"), let item {
            submit({ [client] in try await client.mcpReconnect(item.name) }, done: { [weak self] in
                self?.flash("正在重新连接 \(item.name)")
                self?.loadMcp()
            })
            return true
        }
        if e.cmd, e.key == .delete, let item {
            confirmDelete(item.name) {
                submit({ [client] in try await client.mcpRemove(item.name) }, done: { [weak self] in
                    self?.flash("已删除 \(item.name)（mcp-archive.json 里还能找回）")
                    self?.loadMcp()
                })
            }
            return true
        }
        if e.key == .escape { settingsEscapeAll(); return true }
        return false
    }

    /// ⌘L：weaverd 挑一条路（gh / 浏览器授权 / 设备码），这里只负责告诉你接下来做什么
    func startMcpLogin(_ name: String) {
        submit({ [client] in try await client.mcpLogin(name) }, done: { [weak self] info in
            guard let self else { return }
            switch info.kind {
            case "done":
                self.flash("已用本机 gh 的登录，正在连接 \(name)")
            case "device":
                self.settings?.mcp.device = info
                self.settings?.mcp.deviceFor = name
                if let code = info.userCode {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                }
                self.flash("码已复制，在打开的网页里粘贴")
            default:
                if let e = info.error { self.settings?.error = e } else { self.flash("已在浏览器打开授权页，授权完会自动连上") }
            }
            self.loadMcp(select: name)
        })
    }

    func openMcpEditor(_ item: McpServerItem) {
        settings?.mcp.editName = item.name
        settings?.mcp.originalName = item.name
        let text = item.config.prettyJSON
        settings?.mcp.editText = text
        settings?.mcp.originalText = text
        settings?.mcp.editTools = item.tools
        setMcpMode(.edit(item.name))
    }

    private func mcpEditKey(_ e: KeyEvent, _ name: String) -> Bool {
        if e.cmd, e.key == .enter { saveMcpEditor(name); return true }
        if e.plain, e.key == .tab {          // 名字 ⇄ JSON
            requestFocus(focusedField == .settingsName ? .settingsEditor : .settingsName)
            return true
        }
        if e.key == .escape {
            confirmDiscard(dirty: settings?.mcp.dirty == true) { setMcpMode(.list); loadMcp() }
            return true
        }
        return false
    }

    private func saveMcpEditor(_ name: String) {
        guard let m = settings?.mcp else { return }
        let obj: Any
        do {
            obj = try JSONSerialization.jsonObject(with: Data(m.editText.utf8))
        } catch {
            settings?.error = "JSON 写错了：\(Self.jsonError(error))"
            return
        }
        guard obj is [String: Any] else { settings?.error = "最外层要是一个 { } 对象（这个服务器的配置）"; return }
        let newName = m.editName.trimmingCharacters(in: .whitespaces)
        nonisolated(unsafe) let config = obj
        submit({ [client] in try await client.mcpReplace(name, config: config, rename: newName) }, done: { [weak self] final in
            self?.flash("已保存 \(final)，正在重新连接")
            self?.setMcpMode(.list)
            self?.loadMcp(select: final)
        })
    }

    static func jsonError(_ error: Error) -> String {
        let ns = error as NSError
        return (ns.userInfo[NSDebugDescriptionErrorKey] as? String) ?? ns.localizedDescription
    }

    func openMcpAdd() {
        settings?.mcp.menuPick = 0
        setMcpMode(.addMenu)
    }

    private func mcpMenuKey(_ e: KeyEvent) -> Bool {
        if e.plain, e.key == .up || e.key == .down {
            let pick = max(0, min(2, settings!.mcp.menuPick + (e.key == .down ? 1 : -1)))
            settings?.mcp.menuPick = pick
            return true
        }
        if e.cmd, let d = e.digit, d <= 3 { mcpChooseAdd(d - 1); return true }
        if e.key == .enter { mcpChooseAdd(settings!.mcp.menuPick); return true }
        if e.key == .escape { setMcpMode(.list); return true }
        return false
    }

    private func mcpChooseAdd(_ i: Int) {
        switch i {
        case 0:
            settings?.mcp.pasteText = ""
            settings?.mcp.parsed = nil
            settings?.mcp.parseError = nil
            setMcpMode(.paste)
        case 1:
            settings?.mcp.filter = ""
            settings?.mcp.presetPick = 0
            settings?.mcp.presetTop = 0
            setMcpMode(.presets)
            Task { @MainActor in
                do { settings?.mcp.presets = try await client.mcpPresets() }
                catch { settings?.error = (error as? LocalizedError)?.errorDescription }
            }
        default:
            settings?.mcp.importLoaded = false
            settings?.mcp.importPick = 0
            settings?.mcp.importTop = 0
            setMcpMode(.importing)
            Task { @MainActor in
                do {
                    let sources = try await client.mcpImport()
                    let existing = Set(settings?.mcp.servers.map(\.name) ?? [])
                    var seen = Set<String>()
                    var checked = Set<String>()
                    for s in sources {
                        for name in s.servers.keys.sorted() where !existing.contains(name) && !seen.contains(name) {
                            seen.insert(name)
                            checked.insert(McpPageState.importKey(s.from, name))
                        }
                    }
                    settings?.mcp.sources = sources
                    settings?.mcp.checked = checked
                    settings?.mcp.importLoaded = true
                } catch { settings?.error = (error as? LocalizedError)?.errorDescription }
            }
        }
    }

    // 粘贴：文字变了 0.3 秒后解析一次（视图里的 onChange 调）
    func mcpPasteChanged() {
        settings?.armed = nil
        mcpParseWork?.cancel()
        let text = settings?.mcp.pasteText ?? ""
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            settings?.mcp.parsed = nil
            settings?.mcp.parseError = nil
            return
        }
        let w = DispatchWorkItem { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                do {
                    let p = try await self.client.mcpParse(text)
                    guard self.settings?.mcp.pasteText == text else { return }
                    self.settings?.mcp.parsed = p
                    self.settings?.mcp.parseError = nil
                } catch {
                    guard self.settings?.mcp.pasteText == text else { return }
                    self.settings?.mcp.parsed = nil
                    self.settings?.mcp.parseError = (error as? LocalizedError)?.errorDescription
                }
            }
        }
        mcpParseWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: w)
    }

    private func mcpPasteKey(_ e: KeyEvent) -> Bool {
        if e.cmd, e.key == .enter {
            guard let p = settings?.mcp.parsed, !p.servers.isEmpty else {
                let why = settings?.mcp.parseError ?? "先贴一段 MCP 服务器的 JSON 配置"
                settings?.error = why
                return true
            }
            mcpAddServers(p.servers, overwrite: true)
            return true
        }
        if e.key == .escape {
            confirmDiscard(dirty: settings?.mcp.dirty == true) { setMcpMode(.addMenu) }
            return true
        }
        return false
    }

    private func mcpAddServers(_ servers: [String: JSONValue], overwrite: Bool) {
        submit({ [client] in try await client.mcpAdd(servers, overwrite: overwrite) }, done: { [weak self] added in
            self?.flash("已加入 \(added.joined(separator: "、"))，正在连接")
            self?.setMcpMode(.list)
            self?.loadMcp(select: added.first)
        })
    }

    private func mcpPresetsKey(_ e: KeyEvent) -> Bool {
        let list = settings?.mcp.filteredPresets ?? []
        if e.plain, e.key == .up || e.key == .down {
            guard !list.isEmpty else { return true }
            let p = max(0, min(list.count - 1, settings!.mcp.presetPick + (e.key == .down ? 1 : -1)))
            let top = ListWindow.fit(p, top: settings!.mcp.presetTop, count: list.count)
            settings?.mcp.presetPick = p
            settings?.mcp.presetTop = top
            return true
        }
        if e.cmd, let d = e.digit {
            let i = ListWindow.clamp(settings!.mcp.presetTop, count: list.count) + d - 1
            if i < list.count { settings?.mcp.presetPick = i; mcpChoosePreset(list[i]) }
            return true
        }
        if e.key == .enter, !list.isEmpty {
            mcpChoosePreset(list[min(settings!.mcp.presetPick, list.count - 1)])
            return true
        }
        if e.key == .escape { setMcpMode(.addMenu); return true }
        return false
    }

    /// 筛选文字变了：高亮回到第一条
    func mcpFilterChanged() {
        settings?.mcp.presetPick = 0
        settings?.mcp.presetTop = 0
    }

    private func mcpChoosePreset(_ p: McpPreset) {
        if p.needs.isEmpty {
            mcpSubmitPreset(p.id, values: [:])
        } else {
            settings?.mcp.needs = [:]
            settings?.mcp.needPick = 0
            setMcpMode(.presetFill(p.id))
        }
    }

    private func mcpSubmitPreset(_ id: String, values: [String: String]) {
        let needsLogin = settings?.mcp.presets.first(where: { $0.id == id })?.login != nil
        submit({ [client] in try await client.mcpAddPreset(id, values: values) }, done: { [weak self] name in
            guard let self else { return }
            self.flash("已加入 \(name)，正在连接")
            self.setMcpMode(.list)
            self.loadMcp(select: name)
            if needsLogin { self.startMcpLogin(name) }      // 有 gh 就直接好了；没有就开始登录
        })
    }

    private func mcpFillKey(_ e: KeyEvent, _ id: String) -> Bool {
        guard let p = settings?.mcp.presets.first(where: { $0.id == id }) else { return false }
        if e.plain, e.key == .up || e.key == .down || e.key == .tab {
            let down = e.key == .down || (e.key == .tab && !e.shift)
            let i = max(0, min(p.needs.count - 1, settings!.mcp.needPick + (down ? 1 : -1)))
            settings?.mcp.needPick = i
            requestFocus(.settingsNeed(i))
            return true
        }
        if e.cmd, e.isChar("o") {
            if let s = p.needs[settings!.mcp.needPick].url, let url = URL(string: s) { NSWorkspace.shared.open(url) }
            return true
        }
        if e.key == .enter, !e.cmd {
            mcpSubmitPreset(id, values: settings!.mcp.needs)
            return true
        }
        if e.key == .escape { setMcpMode(.presets); return true }
        return false
    }

    private func mcpImportKey(_ e: KeyEvent) -> Bool {
        let rows = settings?.mcp.importRows ?? []
        if e.plain, e.key == .up || e.key == .down {
            guard !rows.isEmpty else { return true }
            let p = max(0, min(rows.count - 1, settings!.mcp.importPick + (e.key == .down ? 1 : -1)))
            let top = ListWindow.fit(p, top: settings!.mcp.importTop, count: rows.count)
            settings?.mcp.importPick = p
            settings?.mcp.importTop = top
            return true
        }
        if e.plain, e.key == .space, !rows.isEmpty {
            let r = rows[min(settings!.mcp.importPick, rows.count - 1)]
            let key = McpPageState.importKey(r.source, r.name)
            if settings!.mcp.checked.contains(key) { settings?.mcp.checked.remove(key) } else { settings?.mcp.checked.insert(key) }
            return true
        }
        if e.cmd, e.key == .enter {
            var picked: [String: JSONValue] = [:]
            for r in rows where settings!.mcp.checked.contains(McpPageState.importKey(r.source, r.name)) {
                if picked[r.name] == nil { picked[r.name] = r.config }
            }
            if picked.isEmpty { settings?.error = "一个都没勾选（空格勾选）"; return true }
            mcpAddServers(picked, overwrite: false)
            return true
        }
        if e.key == .escape { setMcpMode(.addMenu); return true }
        return false
    }

    // MARK: Skills 页

    func loadSkills(select name: String? = nil) {
        Task { @MainActor in
            do {
                let l = try await client.skills()
                guard settings != nil else { return }
                let n = l.skills.count
                var cur = settings!.skills.cur
                if let name, let i = l.skills.firstIndex(where: { $0.name == name && $0.shadowed != true }) { cur = i }
                cur = max(0, min(cur, n - 1))
                let top = ListWindow.fit(cur, top: settings!.skills.top, count: n)
                settings?.skills.list = l
                settings?.skills.cur = cur
                settings?.skills.top = top
            } catch {
                settings?.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            }
        }
    }

    private func skillsKey(_ e: KeyEvent) -> Bool {
        guard let s = settings?.skills else { return false }
        if let editing = s.editing { return skillEditKey(e, editing) }
        let n = s.skills.count
        if e.plain, e.key == .up || e.key == .down {
            guard n > 0 else { return true }
            let c = max(0, min(n - 1, s.cur + (e.key == .down ? 1 : -1)))
            settings?.skills.cur = c
            settings?.skills.top = ListWindow.fit(c, top: s.top, count: n)
            settings?.armed = nil
            return true
        }
        if e.cmd, let d = e.digit {
            let i = ListWindow.clamp(s.top, count: n) + d - 1
            if i < n { settings?.skills.cur = i; settings?.armed = nil }
            return true
        }
        if e.cmd, e.isChar("n") {
            openSkillEditor(name: "", text: SkillsPageState.template, editable: true, source: "weaver", files: [])
            return true
        }
        let item = n > 0 ? s.skills[min(s.cur, n - 1)] : nil
        if e.key == .enter, !e.cmd, let item {
            if item.shadowed == true {          // 被同名的盖住了：按名字取会拿到上面那份，直接读它自己的文件（只读）
                let text = (try? String(contentsOfFile: item.path + "/SKILL.md", encoding: .utf8)) ?? ""
                openSkillEditor(name: item.name, text: text, editable: false, source: item.source, files: [])
                return true
            }
            let id = settings!.id
            Task { @MainActor in
                do {
                    let d = try await client.skill(item.name)
                    guard stillHere(id, .skills), settings?.skills.editing == nil else { return }
                    openSkillEditor(name: d.name, text: d.text, editable: d.editable, source: item.source, files: d.files)
                } catch {
                    if stillHere(id, .skills) { settings?.error = (error as? LocalizedError)?.errorDescription }
                }
            }
            return true
        }
        if e.cmd, e.key == .delete, let item {
            guard item.editable, item.shadowed != true else {
                settings?.error = item.source == "builtin" ? "内置的 skill 只能看，不能删" : "来自 Claude Code 的 skill 只能看，不能删"
                return true
            }
            confirmDelete(item.name) {
                submit({ [client] in try await client.skillRemove(item.name) }, done: { [weak self] in
                    self?.flash("已删除 \(item.name)（在 skills/.archive 里还能找回）")
                    self?.loadSkills()
                })
            }
            return true
        }
        if e.key == .escape { settingsEscapeAll(); return true }
        return false
    }

    private func openSkillEditor(name: String, text: String, editable: Bool, source: String, files: [String]) {
        settings?.skills.editing = name
        settings?.skills.text = text
        settings?.skills.original = text
        settings?.skills.editable = editable
        settings?.skills.source = source
        settings?.skills.files = files
        settings?.error = nil
        settings?.armed = nil
        requestFocus(.settingsEditor)
    }

    private func skillEditKey(_ e: KeyEvent, _ name: String) -> Bool {
        if e.cmd, e.key == .enter {
            guard settings?.skills.editable == true else { return true }
            let text = settings!.skills.text
            submit({ [client] in
                name.isEmpty ? try await client.skillCreate(text) : try await client.skillSave(name, text: text)
            }, done: { [weak self] final in
                guard let self, self.settings?.skills.editing == name else { return }
                self.flash(name.isEmpty ? "已新建 \(final)" : (final == name ? "已保存 \(final)" : "已保存，改名为 \(final)"))
                self.closeSkillEditor()
                self.loadSkills(select: final)
            })
            return true
        }
        if e.key == .escape {
            confirmDiscard(dirty: settings?.skills.dirty == true) { closeSkillEditor() }
            return true
        }
        return false
    }

    private func closeSkillEditor() {
        settings?.skills.editing = nil
        settings?.error = nil
        settings?.armed = nil
        requestFocus(nil)
    }

    // MARK: 滚轮

    func settingsScroll(_ rows: Int) {
        guard let st = settings else { return }
        switch st.tab {
        case .mcp where st.mcp.mode == .list:
            settings?.mcp.top = ListWindow.clamp(st.mcp.top + rows, count: st.mcp.servers.count)
        case .mcp where st.mcp.mode == .presets:
            settings?.mcp.presetTop = ListWindow.clamp(st.mcp.presetTop + rows, count: st.mcp.filteredPresets.count)
        case .mcp where st.mcp.mode == .importing:
            settings?.mcp.importTop = ListWindow.clamp(st.mcp.importTop + rows, count: st.mcp.importRows.count)
        case .skills where st.skills.editing == nil:
            settings?.skills.top = ListWindow.clamp(st.skills.top + rows, count: st.skills.skills.count)
        case .permissions:
            settings?.permissions.top = ListWindow.clamp(st.permissions.top + rows, count: st.permissions.rows.count)
        case .memory where st.memory.editing == nil:
            settings?.memory.top = ListWindow.clamp(st.memory.top + rows, count: st.memory.rows.count)
        default:
            break
        }
    }
}
