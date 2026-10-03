import SwiftUI

/// 一张白卡里的表单（启动器里 / 提示词的参数、MCP 服务器问你的字段）：照宣传视频的卡片——
/// 表头一行小字，一行一个字段、细线隔开；左边等宽小字的名字（必填带小标签），右边不带框的输入；
/// 当前这一行左边一道墨色竖条、底色略深。
struct FormCard<Rows: View>: View {
    let title: String
    var subtitle: String = ""
    var trailing: String = ""
    var keys: [KeyHint] = []
    @ViewBuilder var rows: Rows

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Text(title).font(KFont.mono(13, .medium)).foregroundStyle(K.ink).lineLimit(1)
                if !subtitle.isEmpty {
                    Text(subtitle).font(KFont.sans(12)).foregroundStyle(K.text3).lineLimit(1)
                }
                Spacer(minLength: 8)
                if !trailing.isEmpty { Text(trailing).font(KFont.mono(11)).foregroundStyle(K.text4) }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .overlay(alignment: .bottom) { HLine(color: K.line2) }

            VStack(spacing: 0) { rows }

            if !keys.isEmpty {
                HStack(spacing: 14) {
                    ForEach(keys) { k in
                        HStack(spacing: 5) { Kbd(k.k, size: 10.5); Text(k.t) }
                    }
                    Spacer(minLength: 0)
                }
                .font(KFont.sans(11.5))
                .foregroundStyle(K.text3)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(K.paper.opacity(0.7))
                .overlay(alignment: .top) { HLine(color: K.line2) }
            }
        }
        .background(RoundedRectangle(cornerRadius: 10).fill(.white))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(K.line))
    }
}

/// 表单的一行
struct FormRow<Content: View>: View {
    let name: String
    var required = false
    var selected = false
    var last = false
    var onTap: () -> Void = {}
    @ViewBuilder var content: Content

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            HStack(spacing: 6) {
                Text(name)
                    .font(KFont.mono(12, selected ? .medium : .regular))
                    .foregroundStyle(selected ? K.ink : K.text2)
                    .lineLimit(1)
                if required {
                    Text("必填")
                        .font(KFont.sans(10, .medium))
                        .foregroundStyle(K.text3)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(RoundedRectangle(cornerRadius: 3).fill(K.line3))
                }
            }
            .frame(width: 128, alignment: .leading)
            content
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.leading, 14)
        .padding(.trailing, 14)
        .frame(minHeight: 40)
        .background(selected ? K.paper : Color.clear)
        .overlay(alignment: .leading) {
            if selected { Rectangle().fill(K.ink).frame(width: 2) }
        }
        .overlay(alignment: .bottom) { if !last { HLine(color: K.line3) } }
        .contentShape(Rectangle())
        .onTapGesture(perform: onTap)
    }
}

/// 不带框的输入（占位是说明）
struct FormField: View {
    let text: Binding<String>
    var placeholder = ""
    var mono = false
    var focused: FocusState<InputField?>.Binding
    let field: InputField

    var body: some View {
        TextField("", text: text, prompt: Text(placeholder).foregroundColor(K.dash))
            .textFieldStyle(.plain)
            .font(mono ? KFont.mono(13) : KFont.sans(13.5))
            .foregroundStyle(K.ink)
            .focused(focused, equals: field)
    }
}

/// 单选：一排小片，选中的墨底；当前行上标 ⌘数字
struct FormChoices: View {
    let options: [String]
    let picked: String?
    var showKeys = false
    let pick: (String) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(Array(options.enumerated()), id: \.offset) { j, o in
                let on = picked == o
                Button { pick(o) } label: {
                    HStack(spacing: 5) {
                        if showKeys { Text("⌘\(j + 1)").font(KFont.mono(10)).foregroundStyle(on ? K.paper.opacity(0.7) : K.text4) }
                        Text(o).font(KFont.sans(12.5, on ? .medium : .regular))
                    }
                    .foregroundStyle(on ? K.paper : K.text2)
                    .padding(.horizontal, 9)
                    .frame(height: 24)
                    .background(RoundedRectangle(cornerRadius: 6).fill(on ? K.ink : K.line3))
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableStyle())
            }
        }
    }
}

/// 是 / 否
struct FormToggle: View {
    let on: Bool
    var showKey = false
    let toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 4)
                    .fill(on ? K.ink : Color.white)
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(on ? K.ink : K.border))
                    .overlay { if on { Image(systemName: "checkmark").font(.system(size: 9, weight: .bold)).foregroundStyle(K.paper) } }
                    .frame(width: 15, height: 15)
                Text(on ? "是" : "否").font(KFont.sans(13)).foregroundStyle(K.text2)
                if showKey { Kbd("空格", size: 10) }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
    }
}
