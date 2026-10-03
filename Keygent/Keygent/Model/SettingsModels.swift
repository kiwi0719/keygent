import Foundation

// 设置页接口的类型（design/api.md 2.12）。配置本身用 JSONValue 原样传，不在 App 里解释。

struct ModelSettings: Decodable {
    var url: String
    var model: String
    /// 已保存的 key 只给末 4 位（"••••abcd"），没保存过是 nil
    var key: String?
    var advanced: [String: String]
    var defaults: [String: String]
}

struct McpServerItem: Decodable, Identifiable, Equatable {
    var id: String { name }
    var name: String
    var config: JSONValue
    var transport: String
    /// connected / connecting / failed / idle / invalid / needs_login / logging_in
    var status: String
    var error: String
    var tools: [String]
    /// 登录进行中（status == logging_in）：浏览器授权的地址，或者设备码
    var login: LoginInfo?
}

/// POST /v1/settings/mcp/{name}/login 的回应，也是列表里进行中的登录（api.md 2.12 登录）
struct LoginInfo: Decodable, Equatable {
    /// done / browser / device
    var kind: String
    var via: String?
    var url: String?
    var userCode: String?
    var verificationURI: String?
    var expiresIn: Double?
    var error: String?

    enum CodingKeys: String, CodingKey {
        case kind, via, url, error
        case userCode = "user_code", verificationURI = "verification_uri", expiresIn = "expires_in"
    }
}

struct McpList: Decodable {
    var servers: [McpServerItem]
    var problem: String?
}

struct McpParsed: Decodable {
    var servers: [String: JSONValue]
    var conflicts: [String]
}

struct PresetNeed: Decodable, Equatable {
    var id: String
    /// env：写进 .env 的令牌；arg：追加到 args（比如目录）
    var kind: String
    var label: String
    var url: String?
}

struct McpPreset: Decodable, Identifiable, Equatable {
    var id: String
    var name: String
    var title: String
    var description: String
    var config: JSONValue
    var needs: [PresetNeed]
    /// "github"：加入后要登录（有 gh 就自动用 gh）
    var login: String?
}

struct ImportSource: Decodable, Equatable {
    var from: String
    var path: String
    var servers: [String: JSONValue]
}

struct SkillItem: Decodable, Identifiable, Equatable {
    var id: String { "\(source)/\(name)" }
    var name: String
    var description: String
    /// weaver / claude / builtin
    var source: String
    var editable: Bool
    var path: String
    var shadowed: Bool?
}

struct SkillProblem: Decodable, Equatable {
    var path: String
    var message: String
}

struct SkillList: Decodable {
    var skills: [SkillItem]
    var problems: [SkillProblem]
}

struct SkillDetail: Decodable {
    var name: String
    var text: String
    var editable: Bool
    var files: [String]
}

/// 设置 › 权限（GET /v1/settings/permissions，api.md v1.9）
struct PermissionList: Decodable, Equatable {
    struct Rule: Decodable, Equatable, Identifiable {
        var task: String
        var taskTitle: String
        var key: String
        var kind: String        // bash / mcp
        var ts: Double
        var id: String { task + "\u{1}" + key }

        enum CodingKeys: String, CodingKey { case task, taskTitle = "task_title", key, kind, ts }
    }

    struct Project: Decodable, Equatable, Identifiable {
        var root: String
        var what: [String]
        var state: String       // trusted / denied
        var id: String { root }
    }

    var rules: [Rule]
    var projects: [Project]
    var builtin: [String]
}

/// 设置 › 记忆（GET /v1/settings/memory）
struct MemoryList: Decodable, Equatable {
    struct Item: Decodable, Equatable {
        var name: String
        var type: String
        var description: String
        var path: String
    }

    struct Scope: Decodable, Equatable {
        var scope: String       // user / project
        var root: String
        var label: String
        var items: [Item]
    }

    var scopes: [Scope]
    var template: String
}

struct MemoryDetail: Decodable {
    var name: String
    var text: String
}

extension JSONValue {
    /// 转成 JSONSerialization 能编码的对象（原样发回去）
    var foundation: Any {
        switch self {
        case .string(let s): return s
        case .number(let n): return n.rounded() == n && abs(n) < 1e15 ? Int(n) as Any : n
        case .bool(let b): return b
        case .array(let a): return a.map(\.foundation)
        case .object(let o): return o.mapValues(\.foundation)
        case .null: return NSNull()
        }
    }

    /// 编辑器里显示的漂亮 JSON
    var prettyJSON: String {
        guard let data = try? JSONEncoder.pretty.encode(self), let s = String(data: data, encoding: .utf8) else { return "{}" }
        return s
    }
}
