import Carbon.HIToolbox

/// 全局快捷键（Carbon RegisterEventHotKey，不需要辅助功能权限）。
final class HotKey {
    private static var handlers: [UInt32: () -> Void] = [:]
    private static var installed = false
    private static var nextID: UInt32 = 1

    private var ref: EventHotKeyRef?
    private let id: UInt32

    init(keyCode: Int, modifiers: Int, handler: @escaping () -> Void) {
        HotKey.installHandlerIfNeeded()
        id = HotKey.nextID
        HotKey.nextID += 1
        HotKey.handlers[id] = handler
        let hkID = EventHotKeyID(signature: OSType(0x4B47_4E54), id: id) // 'KGNT'
        RegisterEventHotKey(UInt32(keyCode), UInt32(modifiers), hkID, GetApplicationEventTarget(), 0, &ref)
    }

    deinit {
        if let ref { UnregisterEventHotKey(ref) }
        HotKey.handlers[id] = nil
    }

    private static func installHandlerIfNeeded() {
        guard !installed else { return }
        installed = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var hk = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
            DispatchQueue.main.async { HotKey.handlers[hk.id]?() }
            return noErr
        }, 1, &spec, nil, nil)
    }
}
