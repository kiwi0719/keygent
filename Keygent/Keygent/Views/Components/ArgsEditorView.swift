import SwiftUI

/// 「改一下再放行」：把 call.args 显示成可编辑的表单，改完把整个 args 发回去（api.md 1.4）。
struct ArgsEditorView: View {
    @Environment(AppStore.self) private var store
    @FocusState private var focused: InputField?

    var body: some View {
        if let ed = store.editor {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("改一下再放行").font(KFont.sans(15, .bold))
                        Text(ed.wait.title).font(KFont.sans(12)).foregroundStyle(K.text3).lineLimit(1)
                    }
                    Spacer()
                    if let name = ed.wait.call?.name {
                        Text(name).font(KFont.mono(11)).foregroundStyle(K.text3)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 14)

                HLine(color: K.line2)

                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(Array(ed.fields.enumerated()), id: \.element.id) { i, f in
                            VStack(alignment: .leading, spacing: 4) {
                                SmallCaps(f.key + (f.original.isString ? "" : " · JSON"))
                                field(i)
                            }
                        }
                    }
                    .padding(18)
                }
                .frame(maxHeight: 320)
                .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 8) {
                    Text("Weaver 会用改过的参数执行，并告诉它你改了什么")
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text3)
                    Spacer()
                    OutlineButton(title: "取消", kbd: "esc") { store.editor = nil }
                    InkButton(title: "放行", kbd: "⌘↵") { store.submitEditor() }
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 12)
                .background(K.paper)
            }
            .frame(width: 600)
            .background(Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .shadow(color: .black.opacity(0.28), radius: 24, y: 12)
            .syncFocus(store, $focused)
        }
    }

    @ViewBuilder
    private func field(_ i: Int) -> some View {
        let tf = TextField("", text: binding(i), axis: .vertical)
            .textFieldStyle(.plain)
            .font(KFont.mono(13))
            .lineLimit(1...8)
        Group {
            // 只有第一个字段接收「打开时自动聚焦」
            if i == 0 { tf.focused($focused, equals: .editor) } else { tf }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 8).fill(.white))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(K.border))
    }

    private func binding(_ i: Int) -> Binding<String> {
        Binding(
            get: { store.editor?.fields[safe: i]?.text ?? "" },
            set: { if store.editor?.fields.indices.contains(i) == true { store.editor!.fields[i].text = $0 } }
        )
    }
}
