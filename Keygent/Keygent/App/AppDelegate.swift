import AppKit
import Carbon.HIToolbox
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let store = AppStore()
    private var panel: PanelController!
    private var capsule: CapsuleController!
    private var hotKeys: [HotKey] = []
    private var askedNotify = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 只留一个 Keygent：新起的接管，旧的退出（不然两个面板、两个胶囊、⌥空格 呼出的可能是另一个）
        let me = NSRunningApplication.current
        for old in NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
        where old.processIdentifier != me.processIdentifier {
            old.terminate()
        }

        NSApp.setActivationPolicy(.accessory)
        NSApp.mainMenu = Self.makeMainMenu()

        panel = PanelController(store: store)
        capsule = CapsuleController(store: store)
        capsule.onOpen = { [weak self] in self?.openCapsuleTarget() }
        capsule.onLauncher = { [weak self] in self?.panel.show() }
        capsule.onSettings = { [weak self] in
            self?.store.openSettings()
            self?.panel.show()
        }

        // 面板没开着时出现新的等待 → 系统通知（api.md 3：App 在后台时弹系统通知）
        UNUserNotificationCenter.current().delegate = self
        store.notify = { [weak self] title, body in self?.postNotification(title, body) }

        // ⌥空格：呼出启动器（⌘空格 是 Spotlight，留给系统）。隔离的调试实例（KEYGENT_DEBUG_NAME）不抢全局快捷键
        if ProcessInfo.processInfo.environment["KEYGENT_DEBUG_NAME"] == nil {
        hotKeys.append(HotKey(keyCode: kVK_Space, modifiers: optionKey) { [weak self] in
            guard let self else { return }
            if self.panel.isVisible && self.store.route == .launcher {
                self.panel.hide()
            } else {
                self.store.go(.launcher)
                self.store.pinCapsuleTaskFirst()
                self.panel.show()
            }
        })
        // ⌘⇧空格：直接打开胶囊里的那件事
        hotKeys.append(HotKey(keyCode: kVK_Space, modifiers: cmdKey | shiftKey) { [weak self] in
            self?.openCapsuleTarget()
        })
        }

        #if DEBUG
        DebugHooks.install(store: store, panel: panel)
        #endif
        store.daemon = DaemonService.ensureRegistered()
        DaemonService.restartIfStale()
        store.start()
        // 第一次用：还没配模型，直接把设置摆出来
        if !FileManager.default.fileExists(atPath: DaemonService.envFile.path) { store.openSettings() }
        panel.show()
    }

    private func openCapsuleTarget() {
        store.openCapsuleTarget()
        panel.show()
    }

    // MARK: 系统通知

    private func postNotification(_ title: String, _ body: String) {
        let center = UNUserNotificationCenter.current()
        let send = {
            let c = UNMutableNotificationContent()
            c.title = title
            c.body = body
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
        }
        if askedNotify { send(); return }
        askedNotify = true
        center.requestAuthorization(options: [.alert, .sound]) { ok, _ in if ok { send() } }
    }

    /// 点通知 → 打开胶囊里的那件事
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async { self.openCapsuleTarget() }
        completionHandler()
    }

    /// 无 Dock 图标的 App 也需要 Edit 菜单，输入框里的 ⌘C/⌘V/⌘A 才能用。
    private static func makeMainMenu() -> NSMenu {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 Keygent", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        return main
    }
}
