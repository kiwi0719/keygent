import AppKit
import SwiftUI
import Observation

/// 菜单栏胶囊。左键 = 打开胶囊里的那件事；右键 = 菜单。
@MainActor
final class CapsuleController: NSObject {
    private let store: AppStore
    private let item: NSStatusItem
    private let host: PassthroughHostingView<CapsuleView>
    var onOpen: () -> Void = {}
    var onLauncher: () -> Void = {}
    var onSettings: () -> Void = {}
    /// 鼠标进出胶囊（胶囊下的小卡）
    var onHover: (Bool) -> Void = { _ in }
    private static let autosaveName = "keygent.capsule"

    init(store: AppStore) {
        self.store = store
        // 新图标默认排在最左：有刘海的屏会被挡住，Hidden Bar 收起时也会被藏掉。
        // 第一次出现时放到最右边（数值 = 离屏幕右边缘的距离）；之后用户 ⌘ 拖到哪，系统就记在哪。
        let posKey = "NSStatusItem Preferred Position \(Self.autosaveName)"
        if UserDefaults.standard.object(forKey: posKey) == nil {
            UserDefaults.standard.set(0, forKey: posKey)
        }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.autosaveName = Self.autosaveName
        host = PassthroughHostingView(rootView: CapsuleView(state: store.capsule))
        super.init()

        if let button = item.button {
            button.title = ""
            button.addSubview(host)
            button.target = self
            button.action = #selector(clicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.setAccessibilityLabel("Keygent")
            button.addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                                  owner: self, userInfo: nil))
        }
        refresh()
    }

    /// 跟踪胶囊状态的变化，更新菜单栏宽度。
    private func refresh() {
        withObservationTracking {
            host.rootView = CapsuleView(state: store.capsule)
        } onChange: { [weak self] in
            Task { @MainActor in self?.refresh() }
        }
        let size = host.fittingSize
        item.length = size.width + 8
        if let button = item.button {
            let h = button.bounds.height > 0 ? button.bounds.height : NSStatusBar.system.thickness
            host.frame = NSRect(x: 4, y: ((h - size.height) / 2).rounded(), width: size.width, height: size.height)
        }
    }

    @objc func mouseEntered(with event: NSEvent) { onHover(true) }
    @objc func mouseExited(with event: NSEvent) { onHover(false) }

    /// 胶囊在屏幕上的位置（小卡挂在它下面）
    var screenFrame: NSRect? {
        guard let b = item.button, let w = b.window else { return nil }
        return w.convertToScreen(b.convert(b.bounds, to: nil))
    }

    @objc private func clicked(_ sender: NSStatusBarButton) {
        guard let ev = NSApp.currentEvent else { return }
        if ev.type == .rightMouseUp || ev.modifierFlags.contains(.control) {
            showMenu()
        } else {
            onOpen()
        }
    }

    private func showMenu() {
        let menu = NSMenu()
        menu.addItem(withTitle: "打开启动器    ⌘⇧空格", action: #selector(openLauncher), keyEquivalent: "").target = self
        let q = menu.addItem(withTitle: "等你的事（\(store.waits.count)）", action: #selector(openQueue), keyEquivalent: "")
        q.target = self
        menu.addItem(.separator())
        let conn = NSMenuItem(title: store.connection == .online ? "已连接 Weaver" : "Weaver 没在运行", action: nil, keyEquivalent: "")
        conn.isEnabled = false
        menu.addItem(conn)
        menu.addItem(withTitle: "重新连接", action: #selector(reconnect), keyEquivalent: "").target = self
        menu.addItem(withTitle: "设置…    ⌘,", action: #selector(openSettings), keyEquivalent: "").target = self
        menu.addItem(withTitle: "重启 Weaver", action: #selector(restartDaemon), keyEquivalent: "").target = self
        if store.daemon == .needsApproval {
            menu.addItem(withTitle: "在登录项里允许 Weaver…", action: #selector(openLoginItems), keyEquivalent: "").target = self
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 Keygent", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        item.menu = menu
        item.button?.performClick(nil)
        item.menu = nil
    }

    @objc private func openLauncher() {
        store.go(.launcher)
        onLauncher()
    }

    @objc private func openQueue() {
        store.openQueue()
        onLauncher()
    }

    @objc private func reconnect() { store.refreshAll() }

    @objc private func restartDaemon() { store.restartDaemon() }

    @objc private func openSettings() { onSettings() }

    @objc private func openLoginItems() { DaemonService.openLoginItemsSettings() }
}

/// 不吃鼠标事件的 hosting view，让点击落到 status bar button 上。
final class PassthroughHostingView<Content: View>: NSHostingView<Content> {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}
