import SwiftUI

/// Skills 页：列表（Weaver 的可改，Claude Code 的、内置的只读）+ SKILL.md 编辑器。
struct SkillsPage: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        if let s = store.settings?.skills {
            if let editing = s.editing { editor(s, editing) } else { list(s) }
        }
    }

    private static let sourceLabel = ["weaver": "Weaver", "claude": "Claude Code", "builtin": "内置"]

    @ViewBuilder
    private func list(_ s: SkillsPageState) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if s.list == nil {
                SettingsEmpty(text: "正在读取…")
            } else if s.skills.isEmpty {
                SettingsEmpty(text: "还没有 skill。⌘N 新建一个：写清楚它是干什么的、什么时候用，正文写做法。")
            } else {
                let win = ListWindow.range(top: s.top, count: s.skills.count)
                ForEach(win, id: \.self) { i in
                    let k = s.skills[i]
                    SettingsRow(slot: i - win.lowerBound, selected: i == s.cur, action: {
                        store.settings?.skills.cur = i
                        _ = store.handleKey(KeyEvent(key: .enter))
                    }) {
                        HStack(spacing: 10) {
                            Text(k.name).font(KFont.mono(14))
                                .foregroundStyle(k.shadowed == true ? K.text4 : K.ink).lineLimit(1)
                            Text(k.description.isEmpty ? "（开头写坏了，打开修一下）" : k.description)
                                .font(KFont.sans(12))
                                .foregroundStyle(k.description.isEmpty ? K.red : K.text3)
                                .lineLimit(1)
                            Spacer(minLength: 8)
                            Text((Self.sourceLabel[k.source] ?? k.source) + (k.shadowed == true ? " · 被同名的盖住" : ""))
                                .font(KFont.sans(11))
                                .foregroundStyle(k.source == "weaver" ? K.text3 : K.text4)
                        }
                    }
                }
                if s.skills.count > ListWindow.size {
                    WindowBar(window: win, total: s.skills.count, unit: "个", up: "↑", down: "↓") { store.settingsScroll($0) }
                        .padding(.horizontal, 12)
                        .padding(.top, 4)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private func editor(_ s: SkillsPageState, _ name: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text(name.isEmpty ? "新建 skill" : name).font(KFont.mono(15, .medium))
                if !s.editable {
                    Text("只读 · " + (s.source == "builtin" ? "内置" : "来自 Claude Code"))
                        .font(KFont.sans(12)).foregroundStyle(K.text4)
                }
                Spacer()
            }
            CodeEditor(text: Binding(get: { store.settings?.skills.text ?? "" },
                                     set: {
                                         store.settings?.skills.text = $0
                                         store.settings?.error = nil
                                         if store.settings?.armed == "discard" { store.settings?.armed = nil }
                                     }),
                       editable: s.editable)
                .padding(.horizontal, 10)
                .frame(height: 300)
                .background(RoundedRectangle(cornerRadius: 8).fill(s.editable ? .white : K.line3))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(store.focusedField == .settingsEditor && s.editable ? K.ink : K.border))
            if !s.files.isEmpty {
                Text("同目录里还有：" + s.files.joined(separator: "、"))
                    .font(KFont.sans(12)).foregroundStyle(K.text4).lineLimit(2)
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
    }
}
