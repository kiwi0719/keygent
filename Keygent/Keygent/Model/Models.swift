import SwiftUI

// MARK: - 任务状态（api.md 1.1）

enum TaskKind {
    case run, queued, wait, done, cancelled, error

    init(status: String) {
        switch status {
        case "running": self = .run
        case "waiting": self = .wait
        case "queued": self = .queued
        case "done", "idle": self = .done
        case "cancelled": self = .cancelled
        case "error": self = .error
        default: self = .run   // 未知的值按「在跑」处理，以后加新状态不会崩
        }
    }

    var color: Color {
        switch self {
        case .run: return K.green
        case .queued: return K.text4
        case .wait: return K.amber
        case .done: return K.dash
        case .cancelled: return K.text4
        case .error: return K.red
        }
    }

    var label: String {
        switch self {
        case .run: return "运行中"
        case .queued: return "排队中"
        case .wait: return "等你"
        case .done: return "已完成"
        case .cancelled: return "已取消"
        case .error: return "出错了"
        }
    }

    /// 这一轮还没结束（可以「停下」）
    var isActive: Bool { self == .run || self == .queued || self == .wait }
}

extension TaskSummary {
    var kind: TaskKind { TaskKind(status: status) }

    /// 列表第二行：出错显示 note，取消显示「已取消」，否则 now
    var line: String {
        switch kind {
        case .error: return note.isEmpty ? (now.isEmpty ? "出错了" : now) : note
        case .cancelled: return now.isEmpty ? "已取消" : "已取消 · \(now)"
        case .queued: return now.isEmpty ? "排队中" : now
        default: return now
        }
    }
}

extension Step {
    /// 用作 SwiftUI id：同一条回复里的文字和工具调用 seq 相同，所以带上下标
    func uid(_ index: Int) -> String { "\(seq)-\(index)" }

    /// 标题拆成「动作」和「对象」：「跑命令 ls -la」→（跑命令, ls -la），「派子 Agent：查文档」→（派子 Agent, 查文档）
    var splitTitle: (String, String) {
        let t = title
        let cut = [t.firstIndex(of: "："), t.firstIndex(of: " ")].compactMap { $0 }.min()
        guard kind == .step, status != "note", let i = cut else { return (t, "") }
        let verb = String(t[..<i])
        let obj = String(t[t.index(after: i)...]).trimmingCharacters(in: .whitespaces)
        return verb.count > 8 ? (t, "") : (verb, obj)
    }

    /// 「记住了 N 条」「记住：…」这类步骤：可以跳到设置 › 记忆
    var isMemoryNote: Bool {
        ["remember", "forget"].contains(tool) || (status == "note" && title.hasPrefix("记住了"))
    }

    /// 对象是命令或路径的，用等宽字
    var monoTitle: Bool { ["bash", "read_file", "write_file", "edit_file", "find_files", "grep"].contains(tool) }

    var toolIcon: String {
        if status == "note" { return "info.circle" }
        switch tool {
        case "bash": return "terminal"
        case "read_file": return "doc.text"
        case "write_file": return "square.and.pencil"
        case "edit_file": return "pencil"
        case "grep": return "magnifyingglass"
        case "find_files": return "folder"
        case "task": return "person.2"
        case "todo_write": return "checklist"
        case "remember", "forget", "recall": return "brain"
        case "skill": return "puzzlepiece"
        case "jobs", "job_output", "job_kill", "job_wait": return "gearshape.2"
        default: return tool.hasPrefix("mcp") ? "puzzlepiece.extension" : "circle.dashed"
        }
    }

    var bubbleKind: Bubble.Kind {
        switch kind {
        case .you: return .you
        case .agent: return .agent
        case .step: return .step
        }
    }

    var statusColor: Color {
        switch status {
        case "running": return K.green
        case "waiting": return K.amber
        case "error": return K.red
        default: return K.dash
        }
    }

    var statusLabel: String? {
        switch status {
        case "running": return "正在执行"
        case "waiting": return tool == "ask_user" ? "等你回答" : "等你放行"
        case "denied": return "被拒绝了"
        case "cancelled": return "取消了"
        case "interrupted": return "中断（服务重启）"
        case "error": return "出错"
        default: return nil
        }
    }

    /// step 的 text 是调用参数 JSON，格式化一下方便看
    var prettyArgs: String {
        guard kind == .step, !text.isEmpty,
              let data = text.data(using: .utf8),
              let v = try? JSONDecoder().decode(JSONValue.self, from: data),
              let out = try? JSONEncoder.pretty.encode(v),
              let s = String(data: out, encoding: .utf8) else { return text }
        return s
    }
}

// MARK: - 等待（api.md 1.4）

extension WaitItem {
    /// 前端按 kind 写死的小字
    var sub: String {
        let base: String
        switch kind {
        case "approval": base = "停下来等你"
        case "stuck": base = "它在原地打转"
        case "input": base = "不是你发的输入，要不要接"
        case "trust": base = "不信任也不影响任务，只是这些先不加载"
        case "question": base = "它在等你回答"
        default: base = ""
        }
        guard let a = fromAgent, !a.isEmpty else { return base }
        return "来自子 Agent「\(a)」· " + base
    }

    /// 按钮文字：信任确认用“信任 / 不信任”，其余同 Choice.label
    func label(_ c: String) -> String {
        if kind == "trust" {
            if c == "allow" { return "信任" }
            if c == "deny" { return "不信任" }
        }
        return Choice.label(c)
    }

    /// ask_user 问你（api.md v1.6）：问题卡，按键和别的等待不一样（design/ask-user.md 第四节）
    var isQuestion: Bool { kind == "question" }
    var questionOptions: [String] { options ?? [] }

    /// 「⌘↵」对应的那个选择（问题卡另有按键，见 AppStore+Question）
    var primaryChoice: String? { ["allow", "continue"].first { choices.contains($0) } }
    /// 「⌫」对应的那个选择
    var secondaryChoice: String? { ["deny", "stop"].first { choices.contains($0) } }
    /// 文字输入框发出去的选择：卡住 = 给个提示；其它 = 拒绝并说明原因
    var noteChoice: String? { kind == "trust" ? nil : ["hint", "deny"].first { choices.contains($0) } }

    /// 能一键放行的：信任确认不算（信任一个项目要你单独看过）
    var isBulkApprovable: Bool { choices.contains("allow") && kind != "trust" }

    /// 改文件审批的 diff 卡标题：路径
    var diffTitle: String {
        if case .string(let p)? = call?.args["path"] { return Workspaces.short(p) }
        return call?.name ?? ""
    }

    /// bash 的命令 / 其它工具的参数预览
    var callPreview: String? {
        guard let call else { return nil }
        if call.args.count == 1, let v = call.args.values.first, case .string(let s) = v { return s }
        guard let data = try? JSONEncoder.pretty.encode(call.args) else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

enum Choice {
    static func label(_ c: String) -> String {
        switch c {
        case "allow": return "放行"
        case "deny": return "先不"
        case "always": return "总是允许"
        case "edit": return "改一下"
        case "continue": return "继续"
        case "stop": return "停下"
        case "hint": return "给个提示"
        case "answer": return "发给它"
        case "skip": return "你自己定"
        default: return c
        }
    }
}

// MARK: - 胶囊（api.md 2.1）

enum CapsuleState: Equatable {
    case offline
    case idle
    case running(Int)
    case waiting(task: String, title: String, more: Int, question: Bool = false)
    case error(task: String, title: String)

    init(_ s: StatusSummary?) {
        guard let s else { self = .offline; return }
        if s.waiting > 0, let w = s.firstWait {
            self = .waiting(task: w.task, title: w.taskTitle, more: s.waiting - 1, question: w.kind == "question")
        } else if let e = s.error {
            self = .error(task: e.task, title: e.taskTitle)
        } else if s.running > 0 {
            self = .running(s.running)
        } else {
            self = .idle
        }
    }

    var text: String {
        switch self {
        case .offline: return "Weaver 没在运行"
        case .idle: return ""
        case .running(let n): return "\(n) 个在跑"
        case .waiting(_, let t, _, let q): return q ? "\(t) · 问你" : "\(t) · 等你放行"
        case .error(_, let t): return "\(t) · 出错了"
        }
    }

    var color: Color {
        switch self {
        case .offline, .idle: return K.dash
        case .running: return K.green
        case .waiting: return K.amber
        case .error: return K.red
        }
    }

    var background: Color {
        switch self {
        case .waiting: return K.amberBg
        case .error: return K.redBg
        default: return .clear
        }
    }

    var more: Int {
        if case .waiting(_, _, let m, _) = self { return m }
        return 0
    }

    var needsYou: Bool {
        switch self {
        case .waiting, .error: return true
        default: return false
        }
    }
}

// MARK: - 附件

struct Attachment: Identifiable, Equatable {
    enum Source: Equatable {
        case file(URL)
        /// 粘贴的长文本：作为文件交给 Agent，太长的它用 grep 按需读（weaver/daemon/uploads.py）
        case text(String)
    }

    let id = UUID()
    var source: Source

    /// 粘贴超过这么多就不放进输入框，变成「长文本」附件
    static func isLongText(_ s: String) -> Bool {
        s.count >= 2000 || s.reduce(0) { $1 == "\n" ? $0 + 1 : $0 } >= 20
    }

    var url: URL? {
        if case .file(let u) = source { return u }
        return nil
    }

    var label: String {
        switch source {
        case .file(let u): return "附件 · \(u.lastPathComponent)"
        case .text(let t):
            let lines = t.split(separator: "\n", omittingEmptySubsequences: false).count
            return lines > 1 ? "长文本 · \(lines) 行" : "长文本 · \(t.count) 字"
        }
    }
}

/// 「最近用过的文件」：在 Keygent 里附加过的文件（存在 UserDefaults，只记还存在的）。
enum RecentFiles {
    private static let key = "recentFiles"
    /// 列表是窗口式的（⌘数字 = 眼前第几个），不用再卡在 9 个
    static let limit = 50

    static func load() -> [URL] {
        let paths = UserDefaults.standard.stringArray(forKey: key) ?? []
        return paths.map { URL(fileURLWithPath: $0) }.filter { FileManager.default.fileExists(atPath: $0.path) }
    }

    static func remember(_ urls: [URL]) {
        var paths = UserDefaults.standard.stringArray(forKey: key) ?? []
        for u in urls {
            paths.removeAll { $0 == u.path }
            paths.insert(u.path, at: 0)
        }
        UserDefaults.standard.set(Array(paths.prefix(limit)), forKey: key)
    }

    static func ext(_ u: URL) -> String {
        let e = u.pathExtension.uppercased()
        return e.isEmpty ? "FILE" : String(e.prefix(4))
    }

    static func whereLabel(_ u: URL) -> String {
        let dir = u.deletingLastPathComponent().path
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let short = dir.hasPrefix(home) ? "~" + dir.dropFirst(home.count) : dir
        let mod = (try? u.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? nil
        return mod.map { "\(short) · \(TimeText.relative($0.timeIntervalSince1970))" } ?? short
    }
}

// MARK: - 时间（api.md 0：显示由前端算）

enum TimeText {
    /// 列表的 when：现在 − updated
    static func relative(_ ts: Double, now: Date = Date()) -> String {
        let d = now.timeIntervalSince1970 - ts
        if d < 60 { return "刚刚" }
        if d < 3600 { return "\(Int(d / 60)) 分钟前" }
        let date = Date(timeIntervalSince1970: ts)
        let cal = Calendar.current
        if cal.isDateInToday(date) { return "\(Int(d / 3600)) 小时前" }
        if cal.isDateInYesterday(date) { return "昨天" }
        let days = cal.dateComponents([.day], from: cal.startOfDay(for: date), to: cal.startOfDay(for: now)).day ?? 99
        if days < 7 {
            let w = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
            return w[cal.component(.weekday, from: date) - 1]
        }
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh_CN")
        f.dateFormat = cal.isDate(date, equalTo: now, toGranularity: .year) ? "M 月 d 日" : "yyyy 年 M 月 d 日"
        return f.string(from: date)
    }

    /// 步骤的 time：ts − 任务 created，形如 0:05 / 12:30 / 1:02:03
    static func offset(_ ts: Double, from created: Double) -> String {
        let s = max(0, Int(ts - created))
        if s >= 3600 { return String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60) }
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    /// 「用时 3 分 50 秒」
    static func duration(_ seconds: Double) -> String {
        let s = max(0, Int(seconds))
        if s < 60 { return "\(s) 秒" }
        if s < 3600 { return "\(s / 60) 分 \(s % 60) 秒" }
        return "\(s / 3600) 小时 \((s % 3600) / 60) 分"
    }
}

// MARK: - 工作区（任务在哪个文件夹里干活；不选 = weaverd 给的临时空目录）

enum Workspaces {
    private static let currentKey = "workspace"
    private static let recentKey = "recentWorkspaces"
    static let limit = 30

    /// weaverd 默认的临时目录（~/Weaver/scratch/<id>），不算「项目」
    static func isScratch(_ path: String) -> Bool {
        path.isEmpty || path.contains("/Weaver/scratch/")
    }

    /// 上次选的工作区；文件夹没了就当没选
    static var current: String? {
        get {
            guard let p = UserDefaults.standard.string(forKey: currentKey), exists(p) else { return nil }
            return p
        }
        set {
            UserDefaults.standard.set(newValue, forKey: currentKey)
            if let newValue { remember(newValue) }
        }
    }

    /// 最近用过的：自己记的 + 已有任务的 workdir，按最近排，去掉临时目录和已删除的
    static func recent(from tasks: [TaskSummary]) -> [String] {
        var out: [String] = []
        let mine = UserDefaults.standard.stringArray(forKey: recentKey) ?? []
        let fromTasks = tasks.sorted { $0.updated > $1.updated }.map(\.workdir)
        for p in mine + fromTasks where !isScratch(p) && !out.contains(p) && exists(p) {
            out.append(p)
        }
        return Array(out.prefix(limit))
    }

    static func remember(_ path: String) {
        var paths = UserDefaults.standard.stringArray(forKey: recentKey) ?? []
        paths.removeAll { $0 == path }
        paths.insert(path, at: 0)
        UserDefaults.standard.set(Array(paths.prefix(limit)), forKey: recentKey)
    }

    static func name(_ path: String) -> String {
        URL(fileURLWithPath: path).lastPathComponent
    }

    static func short(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }

    static func isDir(_ u: URL) -> Bool {
        (try? u.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
    }

    private static func exists(_ p: String) -> Bool {
        var d: ObjCBool = false
        return FileManager.default.fileExists(atPath: p, isDirectory: &d) && d.boolValue
    }
}
