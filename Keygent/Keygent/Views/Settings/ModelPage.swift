import SwiftUI

/// 模型页：地址、模型、Key，分隔线下是高级项（空着 = 默认，灰字写着默认值）。
struct ModelPage: View {
    @Environment(AppStore.self) private var store
    @FocusState private var focused: InputField?

    var body: some View {
        if let s = store.settings {
            VStack(spacing: 0) {
                row("地址") {
                    TextField("", text: bind(\.model.url), prompt: prompt("https://openrouter.ai/api/v1"))
                        .focused($focused, equals: .settingsRow(0))
                }
                HLine(color: K.line2)
                row("模型") {
                    TextField("", text: bind(\.model.model), prompt: prompt("anthropic/claude-sonnet-4.5"))
                        .focused($focused, equals: .settingsRow(1))
                }
                HLine(color: K.line2)
                row("Key") {
                    SecureField("", text: bind(\.model.key), prompt: prompt(keyHint(s.model)))
                        .focused($focused, equals: .settingsRow(2))
                }

                HStack {
                    Text("高级 · 留空用默认").font(KFont.sans(12)).foregroundStyle(K.text4)
                    Spacer()
                }
                .padding(.horizontal, 24)
                .padding(.top, 14)
                .padding(.bottom, 4)

                ForEach(Array(AdvancedItem.keys.enumerated()), id: \.offset) { i, k in
                    if i > 0 { HLine(color: K.line2) }
                    row(AdvancedItem.labels[k]!, width: 120) {
                        HStack(spacing: 8) {
                            TextField("", text: advanced(k), prompt: prompt(s.advancedDefaults[k] ?? ""))
                                .focused($focused, equals: .settingsRow(3 + i))
                            Text(AdvancedItem.units[k]!).font(KFont.sans(12)).foregroundStyle(K.text4)
                        }
                    }
                }
            }
            .padding(.vertical, 6)
            .syncFocus(store, $focused)
            .onChange(of: focused) { _, f in
                if case .settingsRow(let r) = f { store.settings?.row = r }
            }
        }
    }

    private func keyHint(_ m: ModelSettingsState) -> String {
        if m.canReuseKey, let k = m.savedKey { return "已保存 ••••\(k.suffix(4))" }
        return m.isLocal ? "本机服务可以不填" : "sk-…"
    }

    private func prompt(_ text: String) -> Text { Text(text).foregroundColor(K.text4) }

    private func row<Field: View>(_ label: String, width: CGFloat = 48, @ViewBuilder _ field: () -> Field) -> some View {
        HStack(spacing: 0) {
            Text(label)
                .font(KFont.sans(13))
                .foregroundStyle(K.text3)
                .frame(width: width, alignment: .leading)
            field()
                .textFieldStyle(.plain)
                .font(KFont.mono(14))
                .foregroundStyle(K.ink)
        }
        .padding(.horizontal, 24)
        .frame(height: 44)
    }

    private func bind(_ kp: WritableKeyPath<SettingsState, String>) -> Binding<String> {
        Binding(
            get: { store.settings?[keyPath: kp] ?? "" },
            set: {
                guard store.settings != nil else { return }
                store.settings![keyPath: kp] = $0
                store.settings!.error = nil
            }
        )
    }

    private func advanced(_ k: String) -> Binding<String> {
        Binding(
            get: { store.settings?.advanced[k] ?? "" },
            set: {
                guard store.settings != nil else { return }
                store.settings!.advanced[k] = $0
                store.settings!.error = nil
            }
        )
    }
}
