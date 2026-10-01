#if DEBUG
import AppKit

/// 仅调试：通过分布式通知驱动面板，配合按键模拟做实测。只有带 KEYGENT_DEBUG_HOOKS=1 启动时才生效。
/// 通知名 com.keygent.debug（设了 KEYGENT_DEBUG_NAME 就是 com.keygent.debug.<名字>），object 是命令：
/// show | launcher | query:文字 | task[:id] | task-proc | queue | review | detail | key:space/down/up/esc | hide | state
/// 设置页：settings[:mcp|:skills] | keys:cmd+n（带修饰键）| settext:文字（写进当前编辑器 / 输入框）| settings-state | winid
enum DebugHooks {
    static func install(store: AppStore, panel: PanelController) {
        guard ProcessInfo.processInfo.environment["KEYGENT_DEBUG_HOOKS"] == "1" else { return }
        // KEYGENT_DEBUG_NAME：同时开着几个调试实例时，各听各的（com.keygent.debug.<名字>）
        let suffix = ProcessInfo.processInfo.environment["KEYGENT_DEBUG_NAME"].map { ".\($0)" } ?? ""
        DistributedNotificationCenter.default().addObserver(
            forName: Notification.Name("com.keygent.debug" + suffix), object: nil, queue: .main
        ) { note in
            let cmd = note.object as? String ?? ""
            let firstTask = store.tasks.first?.id
            switch cmd {
            case "show": break
            case "launcher": store.go(.launcher)
            case _ where cmd.hasPrefix("task:"): store.openTask(id: String(cmd.dropFirst(5)))
            case "task": if let id = firstTask { store.openTask(id: id) }
            case "task-proc": if let id = firstTask { store.openTask(id: id); store.task.proc = true }
            case _ where cmd.hasPrefix("query:"):      // 往启动器输入框里填字（测搜索用）
                store.go(.launcher)
                store.launcher.query = String(cmd.dropFirst(6))
                store.launcherQueryChanged()
            case "queue": store.openQueue()
            case "review": store.openQueue(); store.queueExpand()
            case "detail": if let id = firstTask { store.openTask(id: id); store.openDetail(from: .task) }
            case "edit": if let w = store.taskGate ?? store.waits.first { store.openEditor(w) }
            case _ where cmd.hasPrefix("key:"):        // 模拟面板里按键（不在输入框里）：key:space | key:down | key:up
                let keys: [String: KeyEvent.Key] = ["space": .space, "down": .down, "up": .up, "esc": .escape]
                if let k = keys[String(cmd.dropFirst(4))] { _ = store.handleKey(KeyEvent(key: k)) }
                return
            case _ where cmd.hasPrefix("keys:"):       // 带修饰键：keys:cmd+n | keys:cmd+enter | keys:ctrl+shift+tab | keys:cmd+3 | keys:enter
                let parts = cmd.dropFirst(5).split(separator: "+").map(String.init)
                let named: [String: KeyEvent.Key] = ["space": .space, "down": .down, "up": .up, "esc": .escape,
                                                     "enter": .enter, "tab": .tab, "delete": .delete,
                                                     "left": .left, "right": .right]
                guard let last = parts.last else { return }
                let key: KeyEvent.Key = named[last] ?? (Int(last).map { .digit($0) } ?? .char(Character(last)))
                let ev = KeyEvent(key: key, cmd: parts.contains("cmd"), shift: parts.contains("shift"),
                                  opt: parts.contains("opt"), ctrl: parts.contains("ctrl"),
                                  inInput: panel.panel.firstResponder is NSTextView)
                NSLog("KGDEBUG key %@ handled=%d", cmd, store.handleKey(ev) ? 1 : 0)
                return
            case _ where cmd == "settings" || cmd.hasPrefix("settings:"):    // settings | settings:mcp | settings:skills
                let tabs: [String: SettingsTab] = ["model": .model, "mcp": .mcp, "skills": .skills]
                store.openSettings(tab: tabs[String(cmd.dropFirst(9))])
            case _ where cmd.hasPrefix("settext:"):    // 往设置页当前的编辑器 / 输入框里写字（模拟打字、粘贴）
                let text = String(cmd.dropFirst(8))
                guard let st = store.settings else { return }
                switch st.tab {
                case .model:
                    let r = st.row
                    if r == 0 { store.settings?.model.url = text } else if r == 1 { store.settings?.model.model = text }
                    else if r == 2 { store.settings?.model.key = text }
                    else { store.settings?.advanced[AdvancedItem.keys[r - 3]] = text }
                case .mcp:
                    switch st.mcp.mode {
                    case .paste: store.settings?.mcp.pasteText = text; store.mcpPasteChanged()
                    case .edit: if store.focusedField == .settingsName { store.settings?.mcp.editName = text }
                                else { store.settings?.mcp.editText = text }
                    case .presets: store.settings?.mcp.filter = text; store.mcpFilterChanged()
                    case .presetFill(let id):
                        if let p = st.mcp.presets.first(where: { $0.id == id }) {
                            store.settings?.mcp.needs[p.needs[st.mcp.needPick].id] = text
                        }
                    default: break
                    }
                case .skills: store.settings?.skills.text = text
                }
                return
            case "settings-state":
                let s = store.settings
                NSLog("KGDEBUG settings tab=%@ mode=%@ row=%d err=%@ armed=%@ focus=%@ mcp=%@ skills=%@",
                      String(describing: s?.tab), String(describing: s?.mcp.mode), s?.row ?? -1, s?.error ?? "-",
                      s?.armed ?? "-", String(describing: store.focusedField),
                      (s?.mcp.servers.map { "\($0.name):\($0.status):\($0.tools.count)" } ?? []).joined(separator: ","),
                      (s?.skills.skills.map { "\($0.name)@\($0.source)" } ?? []).joined(separator: ","))
                return
            case "winid": NSLog("KGDEBUG winid=%d", panel.panel.windowNumber); return
            case "hide": panel.hide(); return
            case "state":
                NSLog("KGDEBUG state route=%@ conn=%@ tasks=%d waits=%d visible=%d key=%d resp=%@ query=%@",
                      String(describing: store.route), String(describing: store.connection),
                      store.tasks.count, store.waits.count,
                      panel.panel.isVisible ? 1 : 0, panel.panel.isKeyWindow ? 1 : 0, String(describing: panel.panel.firstResponder.map { type(of: $0) }), store.launcher.query)
                return
            default: return
            }
            panel.show()
        }
    }
}
#endif
