import SwiftUI

/// 设置页外框：顶上分页（⌘[ ⌘] 切换），中间是当前分页，底下一行写着现在能按什么。
struct SettingsView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        if let s = store.settings {
            VStack(spacing: 0) {
                header(s)
                HLine()
                Group {
                    switch s.tab {
                    case .model: ModelPage()
                    case .mcp: McpPage()
                    case .skills: SkillsPage()
                    case .permissions: PermissionsPage()
                    case .memory: MemoryPage()
                    }
                }
                .frame(maxWidth: .infinity, alignment: .topLeading)
                footer(s)
            }
            .frame(width: 780)
        }
    }

    @ViewBuilder
    private func header(_ s: SettingsState) -> some View {
        HStack(spacing: 14) {
            if s.offline && s.model.firstRun {
                PromptGlyph(size: 34)
                Text("先接上一个模型").font(KFont.sans(20, .medium))
                Spacer()
            } else {
                Text("设置").font(KFont.sans(20, .medium))
                HStack(spacing: 4) {
                    ForEach(SettingsTab.allCases, id: \.self) { t in
                        tabChip(t, selected: t == s.tab, disabled: s.offline && t != .model)
                    }
                }
                .padding(.leading, 8)
                Spacer()
                if s.offline {
                    Text("Weaver 没在运行，只能改模型").font(KFont.sans(12)).foregroundStyle(K.text4)
                } else {
                    HStack(spacing: 6) {
                        Kbd("⌘[")
                        Kbd("⌘]")
                        Text("切换")
                    }
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text3)
                }
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .padding(.bottom, 14)
    }

    private func tabChip(_ t: SettingsTab, selected: Bool, disabled: Bool) -> some View {
        Button {
            guard let cur = store.settings?.tab, cur != t else { return }
            store.settingsSwitch(by: t.rawValue - cur.rawValue)
        } label: {
            Text(t.title)
                .font(KFont.sans(13, selected ? .semibold : .regular))
                .foregroundStyle(selected ? K.paper : (disabled ? K.dash : K.text2))
                .padding(.horizontal, 12)
                .frame(height: 28)
                .background(RoundedRectangle(cornerRadius: 7).fill(selected ? K.ink : .clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .disabled(disabled)
    }

    @ViewBuilder
    private func footer(_ s: SettingsState) -> some View {
        FooterBar {
            if let e = s.error {
                Text(e).foregroundStyle(K.red).lineLimit(2)
            } else if let a = s.armed {
                Text(a == "discard" ? "有改动没保存：再按一次 esc 放弃"
                     : "再按一次 ⌘⌫ \(s.tab == .permissions ? "撤销" : "删除") \(a.dropFirst("delete:".count))，按别的键取消")
                    .foregroundStyle(K.amber)
            } else {
                Text(note(s)).foregroundStyle(K.text3).lineLimit(1)
            }
        } trailing: {
            HStack(spacing: 14) {
                ForEach(hints(s)) { h in
                    HStack(spacing: 5) {
                        Kbd(h.k, size: 12, weight: .medium)
                        Text(h.t)
                    }
                }
            }
            .fixedSize()
        }
    }

    private func note(_ s: SettingsState) -> String {
        switch s.tab {
        case .model: return s.saving ? "正在保存…" : "OpenAI 或 Anthropic 兼容的接口都行；保存后 Weaver 会重启"
        case .mcp:
            switch s.mcp.mode {
            case .list: return "~/.weaver/mcp.json · 所有任务都能用"
            case .edit: return "Tab 在名字和配置之间切换"
            case .addMenu: return "加一个 MCP 服务器"
            case .paste: return "服务器说明里给的 JSON 配置，直接 ⌘V 贴进来"
            case .presets: return "打字筛选"
            case .presetFill: return "填完按 ↵ 加入"
            case .importing: return "空格勾选"
            }
        case .skills:
            if s.skills.editing == nil { return "~/.weaver/skills · 按需加载的说明书" }
            return s.skills.editable ? "开头 --- 之间写 name 和 description" : "只读"
        case .permissions:
            return "「总是允许」只对点它的那个任务有效 · 撤销后下次会再问你"
        case .memory:
            if s.memory.editing == nil { return "新的记忆从下一个任务开始用上" }
            return "开头 --- 之间写 name、description、type"
        }
    }

    private func hints(_ s: SettingsState) -> [KeyHint] {
        switch s.tab {
        case .model:
            return [KeyHint("↑↓", "换行"), KeyHint("↵", "保存"), KeyHint("esc", s.offline && s.model.firstRun ? "收起" : "关闭")]
        case .mcp:
            switch s.mcp.mode {
            case .list:
                if s.mcp.servers.isEmpty { return [KeyHint("⌘N", "添加"), KeyHint("esc", "关闭")] }
                let cur = s.mcp.servers[min(s.mcp.cur, s.mcp.servers.count - 1)]
                let login = ["needs_login", "logging_in"].contains(cur.status) ? [KeyHint("⌘L", "登录")] : []
                return login + [KeyHint("↵", "编辑"), KeyHint("⌘N", "添加"), KeyHint("⌘R", "重连"), KeyHint("⌘⌫", "删除"),
                                KeyHint("esc", "关闭")]
            case .edit: return [KeyHint("⌘↵", "保存"), KeyHint("esc", "返回")]
            case .addMenu: return [KeyHint("⌘1–3", "选"), KeyHint("↵", "选"), KeyHint("esc", "返回")]
            case .paste: return [KeyHint("⌘↵", "加入"), KeyHint("esc", "返回")]
            case .presets: return [KeyHint("↑↓", "选"), KeyHint("↵", "加入"), KeyHint("esc", "返回")]
            case .presetFill: return [KeyHint("⌘O", "去哪拿"), KeyHint("↵", "加入"), KeyHint("esc", "返回")]
            case .importing: return [KeyHint("空格", "勾选"), KeyHint("⌘↵", "导入"), KeyHint("esc", "返回")]
            }
        case .skills:
            if s.skills.editing == nil {
                return s.skills.skills.isEmpty ? [KeyHint("⌘N", "新建"), KeyHint("esc", "关闭")]
                    : [KeyHint("↵", "打开"), KeyHint("⌘N", "新建"), KeyHint("⌘⌫", "删除"), KeyHint("esc", "关闭")]
            }
            return s.skills.editable ? [KeyHint("⌘↵", "保存"), KeyHint("esc", "返回")] : [KeyHint("esc", "返回")]
        case .permissions:
            return s.permissions.rows.isEmpty ? [KeyHint("esc", "关闭")]
                : [KeyHint("↑↓", "选"), KeyHint("⌘⌫", "撤销"), KeyHint("esc", "关闭")]
        case .memory:
            if s.memory.editing == nil {
                return s.memory.rows.isEmpty ? [KeyHint("⌘N", "新建"), KeyHint("esc", "关闭")]
                    : [KeyHint("↵", "打开"), KeyHint("⌘N", "新建"), KeyHint("⌘⌫", "删除"), KeyHint("esc", "关闭")]
            }
            return [KeyHint("⌘↵", "保存"), KeyHint("esc", "返回")]
        }
    }
}

/// 设置页的列表行：左边内容，右边 ⌘编号；选中时白底黑框（同启动器的最近任务）。
struct SettingsRow<Content: View>: View {
    let slot: Int?
    let selected: Bool
    var action: () -> Void = {}
    @ViewBuilder var content: Content

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                content.frame(maxWidth: .infinity, alignment: .leading)
                if let slot { Kbd("⌘\(slot + 1)", active: selected, size: 12, weight: .medium, minWidth: 34) }
            }
            .padding(.vertical, 9)
            .padding(.horizontal, 12)
        }
        .buttonStyle(RowStyle(selected: selected, selectedFill: .white, radius: 8, selectedStroke: K.ink))
    }
}

/// 列表为空、加载中这类一句话
struct SettingsEmpty: View {
    let text: String
    var body: some View {
        Text(text)
            .font(KFont.sans(13))
            .foregroundStyle(K.text4)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 24)
            .padding(.vertical, 22)
    }
}

extension View {
    /// 切模式时输入框和“请求聚焦”是同一拍出来的，syncFocus 那时还找不到它：出现时自己再接一次焦点。
    func focusWhenRequested(_ store: AppStore, _ binding: FocusState<InputField?>.Binding, _ field: InputField) -> some View {
        onAppear {
            DispatchQueue.main.async {
                if store.focusRequest == field { binding.wrappedValue = field }
            }
        }
    }
}
