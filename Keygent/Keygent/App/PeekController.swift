import AppKit
import SwiftUI

/// 胶囊下面那张小卡（宣传视频 0:12）：任务交出去、面板收起后浮出来 4 秒，鼠标停在胶囊上时也出来。
/// 只是看：不抢键盘、不挡点击。内容见 PeekView。
@MainActor
final class PeekController {
    private let store: AppStore
    private let panel: NSPanel
    private let host: NSHostingView<AnyView>
    private var hideWork: DispatchWorkItem?
    /// 卡片挂在谁下面（胶囊的按钮）
    var anchor: () -> NSRect? = { nil }

    init(store: AppStore) {
        self.store = store
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 300, height: 120),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .transient, .ignoresCycle]
        host = NSHostingView(rootView: AnyView(PeekView().environment(store)))
        panel.contentView = host
    }

    /// 出来：seconds 秒后自己收起（nil = 一直在，直到 hide）
    func show(for seconds: Double? = 4) {
        guard store.panelVisible == false, store.peekTask != nil else { return }
        store.loadPeek()
        place()
        panel.orderFrontRegardless()
        hideWork?.cancel()
        if let seconds {
            let w = DispatchWorkItem { [weak self] in self?.hide() }
            hideWork = w
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: w)
        }
    }

    func hide() {
        hideWork?.cancel()
        panel.orderOut(nil)
    }

    private func place() {
        let size = host.fittingSize
        guard let a = anchor(), size.width > 0 else { return }
        let screen = NSScreen.screens.first { $0.frame.intersects(a) } ?? NSScreen.main
        var x = a.maxX - size.width
        if let s = screen?.visibleFrame { x = min(max(x, s.minX + 8), s.maxX - size.width - 8) }
        panel.setFrame(NSRect(x: x, y: a.minY - size.height - 6, width: size.width, height: size.height), display: true)
    }
}

/// 卡片内容：任务名 + 状态；下面有清单列清单（最多 5 项），没有就列最近 3 步
struct PeekView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        let p = store.peek
        let t = store.peekTask
        let kind = t?.kind ?? .run
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Dot(color: kind.color, size: 7)
                Text(t?.title ?? "")
                    .font(KFont.sans(13, .bold))
                    .lineLimit(1)
                Spacer(minLength: 12)
                Text(kind == .wait ? "等你" : kind == .error ? "出错了" : kind.isActive ? "在后台跑" : kind.label)
                    .font(KFont.sans(11))
                    .foregroundStyle(kind == .wait ? K.amber : kind == .error ? K.red : K.text3)
            }
            if !p.todos.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(p.todos.prefix(5).enumerated()), id: \.offset) { _, td in
                        HStack(spacing: 8) {
                            TodoMark(status: td.status).frame(width: 12).scaleEffect(0.85)
                            Text(td.content)
                                .font(KFont.sans(11.5, td.status == "in_progress" ? .medium : .regular))
                                .foregroundStyle(td.status == "pending" ? K.text3 : K.text2)
                                .strikethrough(td.status == "cancelled")
                                .lineLimit(1)
                        }
                    }
                }
            } else if !p.steps.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(p.steps.suffix(3).enumerated()), id: \.offset) { _, s in
                        HStack(spacing: 8) {
                            TodoMark(status: s.status == "running" ? "in_progress" : s.status == "ok" ? "completed" : "pending")
                                .frame(width: 12).scaleEffect(0.85)
                            Text(s.title)
                                .font(s.monoTitle ? KFont.mono(11) : KFont.sans(11.5))
                                .foregroundStyle(K.text2)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                    }
                }
            }
            if kind == .wait || kind == .error {
                HStack(spacing: 5) {
                    Kbd("⌘⇧Space", size: 10)
                    Text("去处理")
                }
                .font(KFont.sans(11))
                .foregroundStyle(K.text3)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(width: 300, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.white))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.black.opacity(0.08), lineWidth: 0.5))
        .foregroundStyle(K.ink)
        .environment(\.colorScheme, .light)
    }
}
