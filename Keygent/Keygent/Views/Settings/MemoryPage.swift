import SwiftUI

/// 记忆页：用户级（所有任务）和各个项目的记忆。一行一条：类型 + 名字 + 一句话说明；↵ 打开编辑整个文件。
struct MemoryPage: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        if let m = store.settings?.memory {
            if let ed = m.editing { editor(m, ed) } else { list(m) }
        }
    }

    @ViewBuilder
    private func list(_ m: MemoryPageState) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            let rows = m.rows
            if m.list == nil {
                SettingsEmpty(text: "正在读取…")
            } else if rows.isEmpty {
                SettingsEmpty(text: "还没有记忆。任务里说「记住…」，或者任务结束后 Weaver 会自己挑值得记的；也可以 ⌘N 自己写一条。")
            } else {
                let win = ListWindow.range(top: m.top, count: rows.count)
                ForEach(win, id: \.self) { i in
                    let r = rows[i]
                    if i == win.lowerBound || rows[i - 1].root != r.root || rows[i - 1].scope != r.scope {
                        HStack(spacing: 8) {
                            SectionLabel(r.label)
                            if !r.root.isEmpty {
                                Text(Workspaces.short(r.root)).font(KFont.mono(11)).foregroundStyle(K.text4).lineLimit(1).truncationMode(.head)
                            }
                        }
                        .padding(.horizontal, 12)
                        .padding(.top, i == win.lowerBound ? 2 : 8)
                    }
                    SettingsRow(slot: i - win.lowerBound, selected: i == m.cur, action: {
                        store.settings?.memory.cur = i
                        _ = store.handleKey(KeyEvent(key: .enter))
                    }) {
                        HStack(spacing: 10) {
                            Text(MemoryPageState.typeLabel[r.item.type] ?? r.item.type)
                                .font(KFont.sans(11, .medium))
                                .foregroundStyle(K.text2)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(K.chip))
                            Text(r.item.name).font(KFont.mono(13)).lineLimit(1)
                            Text(r.item.description)
                                .font(KFont.sans(12))
                                .foregroundStyle(K.text3)
                                .lineLimit(1)
                            Spacer(minLength: 0)
                        }
                    }
                }
                if rows.count > ListWindow.size {
                    WindowBar(window: win, total: rows.count, unit: "条", up: "↑", down: "↓") { store.settingsScroll($0) }
                        .padding(.horizontal, 12)
                        .padding(.top, 4)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private func editor(_ m: MemoryPageState, _ ed: (scope: String, root: String, label: String, name: String)) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text(ed.name.isEmpty ? "新的记忆" : ed.name).font(KFont.mono(15, .medium))
                Text(ed.label).font(KFont.sans(12)).foregroundStyle(K.text4)
                Spacer()
            }
            CodeEditor(text: Binding(get: { store.settings?.memory.text ?? "" },
                                     set: {
                                         store.settings?.memory.text = $0
                                         store.settings?.error = nil
                                         if store.settings?.armed == "discard" { store.settings?.armed = nil }
                                     }),
                       editable: true)
                .padding(.horizontal, 10)
                .frame(height: 300)
                .background(RoundedRectangle(cornerRadius: 8).fill(.white))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(store.focusedField == .settingsEditor ? K.ink : K.border))
            Text("type 可以是 user（你是谁、偏好）、feedback（纠正和认可）、project（项目背景）、reference（资料在哪）")
                .font(KFont.sans(12)).foregroundStyle(K.text4)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
    }
}
