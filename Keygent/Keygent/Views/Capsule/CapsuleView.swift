import SwiftUI

/// ② 胶囊：菜单栏里的状态点，只在需要你时出声（规则见 api.md 2.1）。
/// 空闲 = 一个小点；在跑 = 只显示数量；等你 / 出错 = 变色 + 任务名，其余用 +N 表示。
struct CapsuleView: View {
    let state: CapsuleState

    var body: some View {
        HStack(spacing: 8) {
            if state == .offline {
                Circle().strokeBorder(K.dash, lineWidth: 1.5).frame(width: 8, height: 8)
            } else {
                Dot(color: state.color, size: 8)
            }
            if !state.text.isEmpty {
                Text(state.text)
                    .font(KFont.sans(12, state == .offline ? .regular : .medium))
                    .foregroundStyle(state.needsYou ? K.ink : (state == .offline ? Color.secondary : Color.primary))
                    .lineLimit(1)
                    .fixedSize()
            }
            if state.more > 0 {
                Text("+\(state.more)")
                    .font(KFont.mono(12))
                    .foregroundStyle(K.text4)
            }
        }
        .padding(.horizontal, state.text.isEmpty ? 6 : 10)
        .frame(height: 22)
        .background(Capsule().fill(state.background))
        .fixedSize()
    }
}
