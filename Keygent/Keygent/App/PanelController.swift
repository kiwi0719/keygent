import AppKit
import SwiftUI

/// 无边框、可成为 key 的悬浮面板（类似 Spotlight）。顶边固定，内容变高时向下长。
final class KeyPanel: NSPanel {
    var anchorTop: CGFloat?

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    /// 没人接的按键不要「咚」一声：快捷键都由 AppStore 处理，剩下的静默丢弃。
    override func noResponder(for eventSelector: Selector) {
        if eventSelector == #selector(NSResponder.keyDown(with:)) { return }
        super.noResponder(for: eventSelector)
    }

    /// 输入框里没人认的按键命令（比如 ⌘K → noop:）最后会到窗口这里，默认实现是「咚」。面板里静默忽略。
    override func doCommand(by selector: Selector) {}

    override func setFrame(_ frameRect: NSRect, display flag: Bool) {
        super.setFrame(anchored(frameRect), display: flag)
        invalidateShadow()
    }

    override func setFrame(_ frameRect: NSRect, display displayFlag: Bool, animate animateFlag: Bool) {
        super.setFrame(anchored(frameRect), display: displayFlag, animate: animateFlag)
        invalidateShadow()
    }

    private func anchored(_ r: NSRect) -> NSRect {
        guard let top = anchorTop, let vis = (screen ?? NSScreen.main)?.visibleFrame else { return r }
        var f = r
        f.origin.x = (vis.midX - f.width / 2).rounded()
        // 放不下时往上挪，但不超出可视区域
        let t = max(top, min(vis.maxY - 12, vis.minY + 12 + f.height))
        f.origin.y = (t - f.height).rounded()
        return f
    }
}

final class PanelController: NSObject, NSWindowDelegate {
    let panel: KeyPanel
    private let store: AppStore
    private var keyMonitor: Any?
    private var scrollMonitor: Any?
    private var scrollAccum: CGFloat = 0
    var suppressHide = false

    init(store: AppStore) {
        self.store = store
        panel = KeyPanel(
            contentRect: NSRect(x: 0, y: 0, width: 780, height: 420),
            styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        super.init()

        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.appearance = NSAppearance(named: .aqua)
        panel.delegate = self

        let host = NSHostingController(rootView: RootView().environment(store))
        host.sizingOptions = [.preferredContentSize]
        host.view.wantsLayer = true
        host.view.layer?.backgroundColor = .clear
        panel.contentViewController = host

        store.hidePanel = { [weak self] in self?.hide() }
        store.popPanel = { [weak self] in self?.show() }
        store.resignInput = { [weak self] in
            guard let self else { return }
            self.panel.makeFirstResponder(self.panel.contentView)
        }
        store.openFinder = { [weak self] in self?.runOpenPanel() }
        store.openFolder = { [weak self] in self?.runFolderPanel() }

        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] ev in
            // 注意不能写成 `self?.handleKey(ev) ?? ev`：handleKey 返回 nil 表示「吞掉」，?? 会把事件又放回去
            guard let self else { return ev }
            return self.handleKey(ev)
        }
        scrollMonitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] ev in
            guard let self else { return ev }
            return self.handleScroll(ev)
        }
    }

    var isVisible: Bool { panel.isVisible }

    func show() {
        if NSApp.isHidden { NSApp.unhideWithoutActivation() }
        let screen = NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) } ?? NSScreen.main
        if let vis = screen?.visibleFrame {
            panel.anchorTop = vis.maxY - (vis.height * 0.1).rounded()
            store.panelMaxHeight = panel.anchorTop! - vis.minY - 12
            panel.setFrame(panel.frame, display: false)
        }
        panel.makeKeyAndOrderFront(nil)
        // 等窗口真正成为 key 之后再聚焦输入框，否则 SwiftUI 的焦点请求会落空
        DispatchQueue.main.async { [weak self] in self?.store.didShow() }
    }

    func hide() {
        panel.orderOut(nil)
        store.didHide()
        // 收起后把前台还给之前的 App；否则 Keygent 仍在前台却没有窗口，之后每次按键都会「咚」
        if NSApp.isActive { NSApp.hide(nil) }
    }

    func toggle() {
        if panel.isVisible && panel.isKeyWindow { hide() } else { show() }
    }

    func windowDidResignKey(_ notification: Notification) {
        guard !suppressHide else { return }
        hide()
    }

    // MARK: 键盘 / 滚轮

    private func handleKey(_ ev: NSEvent) -> NSEvent? {
        guard ev.window === panel, panel.isKeyWindow else { return ev }
        // 问题卡刚自动弹出：这段时间的按键全部吞掉（连输入法也不给），免得别处正在打的字落进回答框
        if let until = store.questionLockUntil, Date() < until { return nil }
        let editor = panel.firstResponder as? NSTextView
        // 输入法组字中（拼音候选），按键全部交给输入法
        if let editor, editor.hasMarkedText() { return ev }
        let e = KeyEvent(ev, inInput: editor != nil)
        return store.handleKey(e) ? nil : ev
    }

    private func handleScroll(_ ev: NSEvent) -> NSEvent? {
        guard ev.window === panel else { return ev }
        scrollAccum += ev.scrollingDeltaY * (ev.hasPreciseScrollingDeltas ? 1 : 12)
        let step: CGFloat = 24
        while abs(scrollAccum) >= step {
            store.handleScroll(rows: scrollAccum > 0 ? -1 : 1)
            scrollAccum -= scrollAccum > 0 ? step : -step
        }
        if ev.phase == .ended || ev.momentumPhase == .ended { scrollAccum = 0 }
        return ev
    }

    // MARK: 系统访达

    private func runFolderPanel() {
        let op = NSOpenPanel()
        op.canChooseFiles = false
        op.canChooseDirectories = true
        op.canCreateDirectories = true
        op.prompt = "在这里工作"
        suppressHide = true
        op.level = .modalPanel
        let res = op.runModal()
        suppressHide = false
        if res == .OK, let u = op.url { store.setWorkspace(u.path) }
        panel.makeKeyAndOrderFront(nil)
    }

    private func runOpenPanel() {
        let op = NSOpenPanel()
        op.allowsMultipleSelection = true
        op.canChooseDirectories = false
        op.prompt = "添加"
        suppressHide = true
        op.level = .modalPanel
        let res = op.runModal()
        suppressHide = false
        if res == .OK { store.addFiles(op.urls) }
        panel.makeKeyAndOrderFront(nil)
    }

}
