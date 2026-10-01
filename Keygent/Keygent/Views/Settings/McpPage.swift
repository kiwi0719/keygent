import SwiftUI

/// MCP 页：列表（状态 + 工具数）、编辑一个服务器的 JSON、添加（粘贴 / 常用服务器 / 从 Claude 导入）。
struct McpPage: View {
    @Environment(AppStore.self) private var store
    @FocusState private var focused: InputField?

    var body: some View {
        if let m = store.settings?.mcp {
            Group {
                switch m.mode {
                case .list: list(m)
                case .edit: edit(m)
                case .addMenu: addMenu(m)
                case .paste: paste(m)
                case .presets: presets(m)
                case .presetFill(let id): presetFill(m, id)
                case .importing: importing(m)
                }
            }
            .syncFocus(store, $focused)
        }
    }

    // MARK: 列表

    @ViewBuilder
    private func list(_ m: McpPageState) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if let d = m.device { devicePanel(d, for: m.deviceFor ?? "") }
            if let p = m.list?.problem {
                Text(p).font(KFont.sans(13)).foregroundStyle(K.red).padding(.horizontal, 12).padding(.vertical, 8)
            }
            if m.list == nil {
                SettingsEmpty(text: "正在读取…")
            } else if m.servers.isEmpty && m.list?.problem == nil {
                SettingsEmpty(text: "还没有 MCP 服务器。⌘N 添加：贴一段配置、挑一个常用的，或者从 Claude Code 导入。")
            } else {
                let win = ListWindow.range(top: m.top, count: m.servers.count)
                ForEach(win, id: \.self) { i in
                    let s = m.servers[i]
                    SettingsRow(slot: i - win.lowerBound, selected: i == m.cur,
                                action: { store.settings?.mcp.cur = i; store.openMcpEditor(s) }) {
                        HStack(spacing: 10) {
                            statusMark(s.status)
                            Text(s.name).font(KFont.mono(14)).foregroundStyle(K.ink).lineLimit(1)
                            Text(statusText(s)).font(KFont.sans(12))
                                .foregroundStyle(["failed", "invalid"].contains(s.status) ? K.red : K.text3)
                                .lineLimit(1)
                            Spacer(minLength: 8)
                            Text(s.transport == "http" ? "远程" : "本地").font(KFont.sans(11)).foregroundStyle(K.text4)
                        }
                    }
                }
                if m.servers.count > ListWindow.size {
                    WindowBar(window: win, total: m.servers.count, unit: "个", up: "↑", down: "↓") { store.settingsScroll($0) }
                        .padding(.horizontal, 12)
                        .padding(.top, 4)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    /// 设备码登录：大号的码（已复制）+ 去哪粘贴
    private func devicePanel(_ d: LoginInfo, for name: String) -> some View {
        HStack(spacing: 18) {
            Text(d.userCode ?? "").font(KFont.mono(26, .semibold)).tracking(3)
            VStack(alignment: .leading, spacing: 4) {
                Text("登录 \(name)：码已复制，在打开的网页里粘贴，然后点授权").font(KFont.sans(13))
                HStack(spacing: 6) {
                    Kbd("⌘O")
                    Text("再开一次网页")
                    Kbd("esc").padding(.leading, 8)
                    Text("收起（登录照样等你）")
                }
                .font(KFont.sans(12)).foregroundStyle(K.text3)
            }
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(RoundedRectangle(cornerRadius: 10).fill(K.amberBg2))
        .padding(.bottom, 6)
    }

    private func statusMark(_ status: String) -> some View {
        let (sym, color): (String, Color) = switch status {
        case "connected": ("●", K.green)
        case "connecting", "logging_in": ("◌", K.amber)
        case "needs_login": ("○", K.amber)
        case "failed", "invalid": ("✕", K.red)
        default: ("○", K.text4)
        }
        return Text(sym).font(KFont.sans(13)).foregroundStyle(color).frame(width: 14)
    }

    private func statusText(_ s: McpServerItem) -> String {
        switch s.status {
        case "connected": return "已连接 · \(s.tools.count) 个工具"
        case "connecting": return "连接中…"
        case "needs_login": return s.error.hasPrefix("登录没完成") ? s.error : "要登录 · ⌘L"
        case "logging_in":
            return s.login?.kind == "device" ? "等你在网页上输入码…" : "等你在浏览器里授权… · ⌘O 再开一次"
        case "failed": return "连不上：\(s.error)"
        case "invalid": return "配置有问题：\(s.error)"
        default: return s.error.isEmpty ? "还没连" : s.error
        }
    }

    // MARK: 编辑

    @ViewBuilder
    private func edit(_ m: McpPageState) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Text("名字").font(KFont.sans(13)).foregroundStyle(K.text3).frame(width: 40, alignment: .leading)
                TextField("", text: bind(\.mcp.editName))
                    .textFieldStyle(.plain)
                    .font(KFont.mono(14))
                    .focused($focused, equals: .settingsName)
                    .focusWhenRequested(store, $focused, .settingsName)
            }
            .padding(.horizontal, 12)
            .frame(height: 34)
            .background(RoundedRectangle(cornerRadius: 6).strokeBorder(focused == .settingsName ? K.ink : K.border))

            editorBox(bind(\.mcp.editText), height: 240)

            if !m.editTools.isEmpty {
                Text("工具（\(m.editTools.count)）：" + m.editTools.joined(separator: "、"))
                    .font(KFont.sans(12)).foregroundStyle(K.text3).lineLimit(3)
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
    }

    private func editorBox(_ text: Binding<String>, height: CGFloat) -> some View {
        CodeEditor(text: text)
            .padding(.horizontal, 10)
            .frame(height: height)
            .background(RoundedRectangle(cornerRadius: 8).fill(.white))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .strokeBorder(store.focusedField == .settingsEditor ? K.ink : K.border))
    }

    // MARK: 添加：三选一

    private static let addItems = [("粘贴 JSON", "服务器说明里给的配置，贴进来就认"),
                                   ("常用服务器", "文件、抓网页、浏览器、GitHub……挑一个"),
                                   ("从 Claude Code 导入", "Claude Code、Claude 桌面版里配过的")]

    private func addMenu(_ m: McpPageState) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(Self.addItems.enumerated()), id: \.offset) { i, item in
                SettingsRow(slot: i, selected: i == m.menuPick, action: {
                    store.settings?.mcp.menuPick = i
                    _ = store.handleKey(KeyEvent(key: .enter))
                }) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.0).font(KFont.sans(14, .medium))
                        Text(item.1).font(KFont.sans(12)).foregroundStyle(K.text3)
                    }
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    // MARK: 粘贴

    @ViewBuilder
    private func paste(_ m: McpPageState) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            editorBox(Binding(get: { store.settings?.mcp.pasteText ?? "" },
                              set: { store.settings?.mcp.pasteText = $0; store.settings?.error = nil; store.mcpPasteChanged() }),
                      height: 220)
            Group {
                if let p = m.parsed {
                    let names = p.servers.keys.sorted().map { p.conflicts.contains($0) ? "\($0)（会覆盖已有的）" : $0 }
                    Text("认出了 \(p.servers.count) 个服务器：" + names.joined(separator: "、")).foregroundStyle(K.green)
                } else if let e = m.parseError {
                    Text(e).foregroundStyle(K.red)
                } else {
                    Text("支持 {\"mcpServers\": {…}}、{名字: 配置}，或者单个服务器的配置").foregroundStyle(K.text4)
                }
            }
            .font(KFont.sans(12))
            .lineLimit(2)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
    }

    // MARK: 常用服务器

    @ViewBuilder
    private func presets(_ m: McpPageState) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            TextField("", text: Binding(get: { store.settings?.mcp.filter ?? "" },
                                        set: { store.settings?.mcp.filter = $0; store.mcpFilterChanged() }),
                      prompt: Text("筛选：文件、浏览器、github…").foregroundColor(K.text4))
                .textFieldStyle(.plain)
                .font(KFont.sans(14))
                .focused($focused, equals: .settingsFilter)
                .focusWhenRequested(store, $focused, .settingsFilter)
                .padding(.horizontal, 12)
                .frame(height: 34)
                .background(RoundedRectangle(cornerRadius: 6).strokeBorder(K.ink))
                .padding(.horizontal, 12)
                .padding(.bottom, 4)
            let list = m.filteredPresets
            if m.presets.isEmpty {
                SettingsEmpty(text: "正在读取…")
            } else if list.isEmpty {
                SettingsEmpty(text: "没有对得上的")
            } else {
                let win = ListWindow.range(top: m.presetTop, count: list.count)
                ForEach(win, id: \.self) { i in
                    let p = list[i]
                    SettingsRow(slot: i - win.lowerBound, selected: i == m.presetPick) {
                        HStack(spacing: 10) {
                            Text(p.title).font(KFont.sans(14, .medium))
                            Text(p.name).font(KFont.mono(12)).foregroundStyle(K.text4)
                            Text(p.description).font(KFont.sans(12)).foregroundStyle(K.text3).lineLimit(1)
                            Spacer(minLength: 6)
                            if m.servers.contains(where: { $0.name == p.name }) {
                                Text("已有").font(KFont.sans(11)).foregroundStyle(K.text4)
                            } else if !p.needs.isEmpty {
                                Text("要填：" + p.needs.map(\.label).joined(separator: "、"))
                                    .font(KFont.sans(11)).foregroundStyle(K.amber)
                            }
                        }
                    }
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private func presetFill(_ m: McpPageState, _ id: String) -> some View {
        if let p = m.presets.first(where: { $0.id == id }) {
            VStack(alignment: .leading, spacing: 10) {
                Text("加入「\(p.title)」").font(KFont.sans(15, .medium))
                Text(p.description).font(KFont.sans(12)).foregroundStyle(K.text3)
                ForEach(Array(p.needs.enumerated()), id: \.offset) { i, need in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 10) {
                            Text(need.label).font(KFont.sans(13)).foregroundStyle(K.text3)
                                .frame(width: 120, alignment: .leading)
                            Group {
                                if need.kind == "env" {
                                    SecureField("", text: needBinding(need.id))
                                } else {
                                    TextField("", text: needBinding(need.id),
                                              prompt: Text("~/Documents").foregroundColor(K.text4))
                                }
                            }
                            .textFieldStyle(.plain)
                            .font(KFont.mono(14))
                            .focused($focused, equals: .settingsNeed(i))
                            .focusWhenRequested(store, $focused, .settingsNeed(i))
                        }
                        .padding(.horizontal, 12)
                        .frame(height: 36)
                        .background(RoundedRectangle(cornerRadius: 6)
                            .strokeBorder(focused == .settingsNeed(i) ? K.ink : K.border))
                        if let url = need.url {
                            HStack(spacing: 6) {
                                Kbd("⌘O")
                                Text("去拿：\(url)")
                            }
                            .font(KFont.sans(12)).foregroundStyle(K.text4)
                        }
                    }
                }
                if p.needs.contains(where: { $0.kind == "env" }) {
                    Text("令牌存在 ~/.weaver/.env，配置里只写变量名").font(KFont.sans(12)).foregroundStyle(K.text4)
                }
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 14)
        }
    }

    private func needBinding(_ id: String) -> Binding<String> {
        Binding(get: { store.settings?.mcp.needs[id] ?? "" },
                set: { store.settings?.mcp.needs[id] = $0; store.settings?.error = nil })
    }

    // MARK: 从 Claude 导入

    @ViewBuilder
    private func importing(_ m: McpPageState) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            let rows = m.importRows
            if !m.importLoaded {
                SettingsEmpty(text: "正在找…")
            } else if rows.isEmpty {
                SettingsEmpty(text: "没找到 Claude Code / Claude 桌面版配过的 MCP 服务器。")
            } else {
                let win = ListWindow.range(top: m.importTop, count: rows.count)
                ForEach(win, id: \.self) { i in
                    let r = rows[i]
                    let on = m.checked.contains(McpPageState.importKey(r.source, r.name))
                    SettingsRow(slot: nil, selected: i == m.importPick, action: {
                        store.settings?.mcp.importPick = i
                        _ = store.handleKey(KeyEvent(key: .space))
                    }) {
                        HStack(spacing: 10) {
                            Text(on ? "☑" : "☐").font(KFont.sans(15)).foregroundStyle(on ? K.ink : K.text4)
                            Text(r.name).font(KFont.mono(14))
                            if m.servers.contains(where: { $0.name == r.name }) {
                                Text("已有").font(KFont.sans(11)).foregroundStyle(K.text4)
                            }
                            Spacer()
                            Text(r.source).font(KFont.sans(12)).foregroundStyle(K.text3)
                        }
                    }
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private func bind(_ kp: WritableKeyPath<SettingsState, String>) -> Binding<String> {
        Binding(get: { store.settings?[keyPath: kp] ?? "" },
                set: {
                    guard store.settings != nil else { return }
                    store.settings![keyPath: kp] = $0
                    store.settings!.error = nil
                    if store.settings!.armed == "discard" { store.settings!.armed = nil }
                })
    }
}
