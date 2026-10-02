import AppKit

// MCP 服务器在调用中途问你（design/mcp2.md 第二节）：一张琥珀卡，一行一个字段。
// Tab / ↑↓ 在字段间移动，空格切换是否，⌘数字 选单选项，⌘↵ 交上去，⌫ 不给，esc 先放着。

extension AppStore {
    /// 这件提问的字段（网址那种没有字段）
    func elicitFields(_ w: WaitItem) -> [ElicitField] { w.mode == "url" ? [] : (w.fields ?? []) }

    /// 换了一件提问：按服务器给的默认值重新填
    func elicitPrepare(_ w: WaitItem) {
        guard task.elicitFor != w.id else { return }
        task.elicitFor = w.id
        task.elicitCur = 0
        var v: [String: JSONValue] = [:]
        for f in elicitFields(w) {
            if let d = f.defaultValue, d != .null { v[f.name] = d }
            else if f.type == "boolean" { v[f.name] = .bool(false) }
        }
        task.elicitValues = v
    }

    func elicitText(_ name: String) -> String {
        switch task.elicitValues[name] {
        case .string(let s)?: return s
        case .number(let n)?: return n.rounded() == n ? String(Int(n)) : String(n)
        case .bool(let b)?: return b ? "true" : "false"
        default: return ""
        }
    }

    func elicitSet(_ name: String, _ v: JSONValue?) {
        task.elicitValues[name] = v
    }

    func elicitFocus(_ w: WaitItem) {
        let fields = elicitFields(w)
        guard fields.indices.contains(task.elicitCur) else { requestFocus(nil); return }
        let f = fields[task.elicitCur]
        requestFocus(["string", "number", "integer"].contains(f.type) ? .elicit(task.elicitCur) : nil)
    }

    func elicitSubmit(_ w: WaitItem) {
        if w.mode != "url" {
            for f in elicitFields(w) where f.required {
                let v = task.elicitValues[f.name]
                if v == nil || v == .null || v == .string("") {
                    flash("「\(f.title)」必须填")
                    task.elicitCur = elicitFields(w).firstIndex { $0.name == f.name } ?? 0
                    elicitFocus(w)
                    return
                }
            }
        }
        let values = task.elicitValues.filter { $0.value != .string("") }
        answer(w, "accept", values: w.mode == "url" ? nil : values) { [weak self] ok in
            if ok { self?.flash("已交给 \(w.server ?? "服务器")") }
        }
        requestFocus(nil)
    }

    func elicitKey(_ e: KeyEvent, _ w: WaitItem) -> Bool? {
        elicitPrepare(w)
        let fields = elicitFields(w)
        if e.cmd, e.key == .enter { elicitSubmit(w); return true }
        if w.mode == "url", e.cmd, e.isChar("o"), let u = w.url.flatMap(URL.init(string:)) {
            NSWorkspace.shared.open(u)
            return true
        }
        if !e.inInput, e.plain, e.key == .delete { answer(w, "decline"); return true }
        if e.key == .tab, !fields.isEmpty {
            task.elicitCur = (task.elicitCur + (e.shift ? -1 : 1) + fields.count) % fields.count
            elicitFocus(w)
            return true
        }
        guard fields.indices.contains(task.elicitCur) else { return nil }
        let f = fields[task.elicitCur]
        if e.inInput {
            if e.key == .enter, e.plain {                       // 文字字段里 ↵ = 下一个（最后一个就交上去）
                if task.elicitCur == fields.count - 1 { elicitSubmit(w) } else {
                    task.elicitCur += 1
                    elicitFocus(w)
                }
                return true
            }
            return nil
        }
        if e.plain, e.key == .up || e.key == .down {
            task.elicitCur = max(0, min(fields.count - 1, task.elicitCur + (e.key == .down ? 1 : -1)))
            return true
        }
        if f.type == "boolean", e.plain, e.key == .space {
            elicitSet(f.name, .bool(!(task.elicitValues[f.name] == .bool(true))))
            return true
        }
        if f.type == "enum", e.cmd, let d = e.digit, let opts = f.options, d <= opts.count {
            elicitSet(f.name, .string(opts[d - 1]))
            return true
        }
        if e.plain, e.key == .enter, ["string", "number", "integer"].contains(f.type) {
            elicitFocus(w)
            return true
        }
        return nil
    }
}
