import AppKit

/// 面板里的按键，统一从 NSEvent 本地监听进来，交给 AppStore 路由。
struct KeyEvent {
    enum Key: Equatable {
        case char(Character)
        case digit(Int)
        case enter, escape, tab, up, down, left, right, delete, space
        case other
    }

    let key: Key
    let cmd: Bool
    let shift: Bool
    let opt: Bool
    let ctrl: Bool
    /// 焦点是否在输入框里
    let inInput: Bool

    /// 没有 ⌘ ⌥ ⌃（shift 不算）
    var plain: Bool { !cmd && !opt && !ctrl }

    var digit: Int? {
        if case .digit(let d) = key { return d }
        return nil
    }

    func isChar(_ c: Character) -> Bool { key == .char(c) }

    init(key: Key, cmd: Bool = false, shift: Bool = false, opt: Bool = false, ctrl: Bool = false, inInput: Bool = false) {
        self.key = key
        self.cmd = cmd
        self.shift = shift
        self.opt = opt
        self.ctrl = ctrl
        self.inInput = inInput
    }

    init(_ ev: NSEvent, inInput: Bool) {
        let f = ev.modifierFlags
        cmd = f.contains(.command)
        shift = f.contains(.shift)
        opt = f.contains(.option)
        ctrl = f.contains(.control)
        self.inInput = inInput

        let digits: [UInt16: Int] = [18: 1, 19: 2, 20: 3, 21: 4, 23: 5, 22: 6, 26: 7, 28: 8, 25: 9]
        switch ev.keyCode {
        case 36, 76: key = .enter
        case 53: key = .escape
        case 48: key = .tab
        case 126: key = .up
        case 125: key = .down
        case 123: key = .left
        case 124: key = .right
        case 51: key = .delete
        case 49: key = .space
        default:
            if let d = digits[ev.keyCode] {
                key = .digit(d)
            } else if let c = ev.charactersIgnoringModifiers?.lowercased().first {
                key = .char(c)
            } else {
                key = .other
            }
        }
    }
}
