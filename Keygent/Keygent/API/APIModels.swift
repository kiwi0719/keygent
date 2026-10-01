import Foundation

// 字段与 design/api.md 第 4 节一致。
// 不用 convertFromSnakeCase：它会连工具参数（call.args）字典的键一起改，发回去就错了。蛇形字段各自写 CodingKeys。

struct TaskSummary: Codable, Identifiable, Equatable {
    let id: String
    var title: String
    var status: String          // running / waiting / queued / done / cancelled / error / idle
    var note: String
    var now: String
    var created: Double
    var updated: Double
    var waiting: Int
    var workdir: String
}

struct Step: Codable, Equatable {
    enum Kind: String, Codable { case you, agent, step }
    var kind: Kind
    var seq: Int
    var ts: Double
    var title: String
    var text: String
    var tool: String
    var out: String
    var why: String
    var status: String          // ok / running / waiting / denied / cancelled / interrupted / error / note
}

struct WaitCall: Codable, Equatable {
    var id: String
    var name: String
    var args: [String: JSONValue]
}

struct WaitItem: Codable, Identifiable, Equatable {
    let id: String
    var task: String
    var taskTitle: String
    var seq: Int
    var ts: Double
    var kind: String            // approval / stuck / input / trust / question
    var title: String
    var body: String
    var choices: [String]
    var call: WaitCall?
    /// 子 Agent 升上来的等待：子 Agent 的任务名（api.md v1.4）
    var fromAgent: String?
    /// question（ask_user 问你）的可选答案：0 个或 2~4 个（api.md v1.6）
    var options: [String]?

    enum CodingKeys: String, CodingKey {
        case id, task, taskTitle = "task_title", seq, ts, kind, title, body, choices, call, fromAgent = "from_agent", options
    }
}

/// 后台命令 / 后台子 Agent（任务详情的 jobs，api.md v1.4）
struct JobInfo: Codable, Equatable, Identifiable {
    let id: String
    var kind: String            // shell / agent
    var title: String
    var status: String          // running / done / failed / killed
    var started: Double
    var ended: Double?
}

/// 搜索结果（GET /v1/search，api.md v1.4）
struct SearchHit: Codable, Equatable, Identifiable {
    var task: String
    var taskTitle: String
    var workdir: String
    var archived: Bool
    /// 不为空：命中在子 Agent 的账本里（seq 是子账本的序号，不能跳到主任务的步骤）
    var sub: String
    var seq: Int
    var ts: Double
    var kind: String            // 用户 / 模型 / 工具结果 / 系统通知 / 压缩摘要
    var excerpt: String

    var id: String { "\(task)-\(sub)-\(seq)" }

    enum CodingKeys: String, CodingKey {
        case task, taskTitle = "task_title", workdir, archived, sub, seq, ts, kind, excerpt
    }
}

struct SearchList: Codable { var results: [SearchHit] }

struct TaskDetail: Codable, Equatable {
    var task: TaskSummary
    var steps: [Step]
    var waiting: [WaitItem]
    var final: String
    var usage: Usage
    /// 后台命令和后台子 Agent；旧版 weaverd 没有这个字段
    var jobs: [JobInfo]?

    struct Usage: Codable, Equatable {
        var tokens: Int
        var budget: Int
        var steps: Int
        var maxSteps: Int
        /// 记忆提取用掉的 token（折算后）；旧版 weaverd 没有这个字段
        var extractTokens: Int?

        enum CodingKeys: String, CodingKey {
            case tokens, budget, steps, maxSteps = "max_steps", extractTokens = "extract_tokens"
        }
    }
}

struct StatusSummary: Codable, Equatable {
    struct Ref: Codable, Equatable {
        var task: String
        var taskTitle: String
        var title: String?
        var note: String?
        /// first_wait 的等待类型（api.md v1.6）；旧版 weaverd 没有
        var kind: String?

        enum CodingKeys: String, CodingKey { case task, taskTitle = "task_title", title, note, kind }
    }

    var running: Int
    var queued: Int
    var waiting: Int
    var firstWait: Ref?
    var error: Ref?

    enum CodingKeys: String, CodingKey { case running, queued, waiting, firstWait = "first_wait", error }
}

struct BlobInfo: Codable, Equatable {
    var id: String
    var name: String
    var mime: String
    var size: Int
}

struct APIErrorBody: Codable {
    struct Inner: Codable { var code: String; var message: String }
    var error: Inner
}

struct TaskList: Codable { var tasks: [TaskSummary] }
struct WaitList: Codable { var waits: [WaitItem] }

/// 工具参数是任意 JSON
enum JSONValue: Codable, Equatable {
    case string(String), number(Double), bool(Bool), array([JSONValue]), object([String: JSONValue]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        case .null: try c.encodeNil()
        }
    }

    /// 编辑表单里显示的文字：字符串原样，其它是 JSON
    var editableText: String {
        if case .string(let s) = self { return s }
        guard let data = try? JSONEncoder.pretty.encode(self), let s = String(data: data, encoding: .utf8) else { return "" }
        return s
    }

    var isString: Bool {
        if case .string = self { return true }
        return false
    }

    /// 从表单文字还原：原来是字符串就仍是字符串，否则按 JSON 解析（解析失败退回字符串）
    static func from(text: String, like original: JSONValue) -> JSONValue {
        if original.isString { return .string(text) }
        if let data = text.data(using: .utf8), let v = try? JSONDecoder().decode(JSONValue.self, from: data) { return v }
        return .string(text)
    }
}

extension JSONEncoder {
    static let pretty: JSONEncoder = {
        let e = JSONEncoder()
        e.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return e
    }()
}
