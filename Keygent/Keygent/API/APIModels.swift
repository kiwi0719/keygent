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
    /// 归档的任务（GET /v1/tasks?archived=1，api.md v1.9）
    var archived: Bool?
    var archivedAt: Double?

    enum CodingKeys: String, CodingKey {
        case id, title, status, note, now, created, updated, waiting, workdir, archived, archivedAt = "archived_at"
    }
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
    /// 改文件那一步：从参数算的统一 diff（api.md v1.9）
    var diff: String?
    /// 派子 Agent 那一步：子账本（GET /v1/tasks/{id}/agents/{sub}）
    var sub: String?
    /// 工具返回的图片（MCP）：GET /v1/blobs/{id} 取
    var images: [StepImage]?
}

struct StepImage: Codable, Equatable, Hashable {
    var id: String
    var mime: String
}

/// MCP 服务器提供的 prompt（GET /v1/prompts，启动器里 / 选用）
struct PromptItem: Codable, Equatable, Identifiable {
    struct Argument: Codable, Equatable {
        var name: String
        var description: String
        var required: Bool
    }
    var server: String
    var name: String
    var title: String
    var description: String
    var arguments: [Argument]

    var id: String { server + ":" + name }
    var command: String { "/\(server):\(name)" }
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
    /// 改文件的审批：按磁盘现状和参数算的统一 diff（api.md v1.9）
    var diff: String?
    /// elicit（MCP 服务器向你提问，api.md v1.9）：哪个服务器、表单 / 网址
    var server: String?
    var mode: String?
    var url: String?
    var fields: [ElicitField]?

    enum CodingKeys: String, CodingKey {
        case id, task, taskTitle = "task_title", seq, ts, kind, title, body, choices, call, fromAgent = "from_agent", options,
             diff, server, mode, url, fields
    }
}

/// MCP 服务器提问的一个字段（字符串 / 数字 / 布尔 / 单选）
struct ElicitField: Codable, Equatable, Identifiable {
    var name: String
    var title: String
    var description: String
    var type: String            // string / number / integer / boolean / enum
    var required: Bool
    var options: [String]?
    var defaultValue: JSONValue?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, title, description, type, required, options, defaultValue = "default"
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
    /// 后台子 Agent 的子账本
    var sub: String?
}

/// 这个任务改过的一个文件（任务详情的 changes，api.md v1.9）
struct FileChange: Codable, Equatable, Identifiable {
    var path: String
    var rel: String
    var added: Int
    var removed: Int
    var created: Bool
    var undone: Bool
    var canUndo: Bool
    var why: String

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case path, rel, added, removed, created, undone, canUndo = "can_undo", why
    }
}

struct FileDiff: Codable, Equatable {
    var path: String
    var rel: String
    var diff: String
    var why: String?
}

struct TodoItem: Codable, Equatable {
    var content: String
    var status: String          // pending / in_progress / completed / cancelled
}

/// 一个子 Agent 的过程（GET /v1/tasks/{id}/agents/{sub}）
struct AgentDetail: Codable, Equatable {
    struct Usage: Codable, Equatable { var tokens: Int; var steps: Int }
    var sub: String
    var title: String
    var status: String
    var steps: [Step]
    var final: String
    var usage: Usage
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
    /// 当前 todo 清单、改过的文件（api.md v1.9）；旧版 weaverd 没有
    var todos: [TodoItem]?
    var changes: [FileChange]?

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
