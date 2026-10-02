import SwiftUI

/// 权限页：点过「总是允许」的命令 / MCP 工具（按任务记），信任过的项目，以及改不了的默认规则。
struct PermissionsPage: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        if let p = store.settings?.permissions {
            VStack(alignment: .leading, spacing: 4) {
                if p.list == nil {
                    SettingsEmpty(text: "正在读取…")
                } else {
                    let rows = p.rows
                    if rows.isEmpty {
                        SettingsEmpty(text: "还没有点过「总是允许」，也没有信任过哪个项目的 skill、MCP 服务器。")
                    } else {
                        let win = ListWindow.range(top: p.top, count: rows.count)
                        ForEach(win, id: \.self) { i in
                            if i == win.lowerBound || kind(rows[i]) != kind(rows[i - 1]) {
                                SectionLabel(kind(rows[i]) == 0 ? "总是允许" : "信任过的项目")
                                    .padding(.horizontal, 12)
                                    .padding(.top, i == win.lowerBound ? 2 : 8)
                            }
                            SettingsRow(slot: i - win.lowerBound, selected: i == p.cur, action: {
                                store.settings?.permissions.cur = i
                            }) {
                                row(rows[i])
                            }
                        }
                        if rows.count > ListWindow.size {
                            WindowBar(window: win, total: rows.count, unit: "条", up: "↑", down: "↓") { store.settingsScroll($0) }
                                .padding(.horizontal, 12)
                                .padding(.top, 4)
                        }
                    }
                    builtin(p.list?.builtin ?? [], open: p.showBuiltin)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
    }

    private func kind(_ r: PermissionsPageState.Row) -> Int {
        if case .rule = r { return 0 }
        return 1
    }

    @ViewBuilder
    private func row(_ r: PermissionsPageState.Row) -> some View {
        switch r {
        case .rule(let x):
            HStack(spacing: 10) {
                Text(x.kind == "bash" ? "跑" : "MCP")
                    .font(KFont.sans(12, .medium))
                    .foregroundStyle(K.text3)
                    .frame(width: 30, alignment: .leading)
                Text(x.key)
                    .font(KFont.mono(13))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 8)
                Text("\(x.taskTitle) · \(TimeText.relative(x.ts))")
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text4)
                    .lineLimit(1)
            }
        case .project(let x):
            HStack(spacing: 10) {
                Image(systemName: "folder")
                    .font(.system(size: 11))
                    .foregroundStyle(K.text3)
                    .frame(width: 30, alignment: .leading)
                Text(Workspaces.short(x.root))
                    .font(KFont.mono(13))
                    .lineLimit(1)
                    .truncationMode(.head)
                Text(x.what.joined(separator: "、"))
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text3)
                    .lineLimit(1)
                Spacer(minLength: 8)
                HStack(spacing: 6) {
                    Dot(color: x.state == "trusted" ? K.green : K.dash, size: 6)
                    Text(x.state == "trusted" ? "信任" : "不信任")
                }
                .font(KFont.sans(12))
                .foregroundStyle(K.text3)
            }
        }
    }

    /// 改不了的默认规则：平时折成一行，空格或点一下展开
    private func builtin(_ items: [String], open: Bool) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Button { store.settings?.permissions.showBuiltin.toggle() } label: {
                HStack(spacing: 6) {
                    SectionLabel("默认规则 · \(items.count) 条")
                    Image(systemName: open ? "chevron.up" : "chevron.down")
                        .font(.system(size: 9))
                        .foregroundStyle(K.text4)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(PressableStyle())
            if open {
                ForEach(items, id: \.self) { t in
                    HStack(alignment: .top, spacing: 8) {
                        Text("·")
                        Text(t)
                    }
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text3)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 14)
        .padding(.bottom, 4)
    }
}
