import SwiftUI

/// 面板的根：同一个窗口，按 route 原地切换形态。
struct RootView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        Group {
            if store.settings != nil {
                SettingsView()
            } else {
                switch store.route {
                case .launcher: LauncherView()
                case .task: TaskView()
                case .queue: QueueView()
                case .detail: DetailView()
                }
            }
        }
        .frame(minHeight: store.editor != nil ? 480 : nil, alignment: .top)
        .overlay {
            if store.editor != nil {
                ZStack {
                    Color.black.opacity(0.12).onTapGesture { store.editor = nil }
                    ArgsEditorView()
                }
            }
        }
        .overlay(alignment: .bottom) {
            if let t = store.toast {
                Text(t)
                    .font(KFont.sans(12, .medium))
                    .foregroundStyle(K.paper)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(Capsule().fill(K.ink.opacity(0.92)))
                    .padding(.bottom, 56)
                    .transition(.opacity)
                    .allowsHitTesting(false)
            }
        }
        .overlay(alignment: .top) {
            if let b = store.banner {
                Button { if let w = store.bannerFor { store.openQuestion(w) } else { store.openCapsuleTarget() } } label: {
                    Text(b)
                        .font(KFont.sans(12, .medium))
                        .foregroundStyle(K.ink)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                        .background(Capsule().fill(K.amberBg))
                        .overlay(Capsule().strokeBorder(K.amber.opacity(0.4)))
                }
                .buttonStyle(PressableStyle())
                .padding(.top, 10)
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: store.toast)
        .animation(.easeOut(duration: 0.15), value: store.banner)
        .foregroundStyle(K.ink)
        .background(K.paper)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(Color.black.opacity(0.08), lineWidth: 0.5)
        )
        .environment(\.colorScheme, .light)
    }
}
