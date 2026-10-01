import Foundation
import UniformTypeIdentifiers

enum APIError: LocalizedError, Equatable {
    /// 读不到 daemon.json 或连不上
    case notRunning
    /// 服务端返回的错误（message 是给人看的中文）
    case server(status: Int, code: String, message: String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .notRunning: return "Weaver 没在运行"
        case .server(_, _, let m): return m
        case .decoding(let m): return "看不懂服务返回的内容：\(m)"
        }
    }

    var code: String? {
        if case .server(_, let c, _) = self { return c }
        return nil
    }
}

/// `~/.weaver/daemon.json`
struct DaemonConfig: Codable, Equatable {
    var port: Int
    var token: String
    var pid: Int?
    var version: String?

    /// 可以用环境变量 WEAVER_DAEMON_JSON 指到别的文件（联调用）
    static var path: URL {
        if let p = ProcessInfo.processInfo.environment["WEAVER_DAEMON_JSON"], !p.isEmpty {
            return URL(fileURLWithPath: (p as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".weaver/daemon.json")
    }

    static func load() -> DaemonConfig? {
        guard let data = try? Data(contentsOf: path) else { return nil }
        return try? JSONDecoder().decode(DaemonConfig.self, from: data)
    }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }
}

/// weaverd 的 HTTP 客户端（design/api.md 第 2 节）。每次请求都重新读 daemon.json，weaverd 重启换了端口也能接上。
final class WeaverClient {
    private let session: URLSession

    init() {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 15
        cfg.connectionProxyDictionary = [:]  // 只连本机，不走代理
        session = URLSession(configuration: cfg)
    }

    // MARK: 接口

    func status() async throws -> StatusSummary { try await get("/v1/status") }

    func tasks() async throws -> [TaskSummary] { (try await get("/v1/tasks") as TaskList).tasks }

    func createTask(text: String, workdir: String? = nil, attachments: [String] = []) async throws -> TaskSummary {
        var body: [String: Any] = ["text": text]
        if let workdir { body["workdir"] = workdir }
        if !attachments.isEmpty { body["attachments"] = attachments }
        return try await send("POST", "/v1/tasks", json: body)
    }

    func task(_ id: String) async throws -> TaskDetail { try await get("/v1/tasks/\(id.pathEscaped)") }

    func rename(_ id: String, title: String) async throws -> TaskSummary {
        try await send("PATCH", "/v1/tasks/\(id.pathEscaped)", json: ["title": title])
    }

    func input(_ id: String, text: String, attachments: [String] = []) async throws -> TaskSummary {
        try await send("POST", "/v1/tasks/\(id.pathEscaped)/input", json: ["text": text, "attachments": attachments])
    }

    func cancel(_ id: String) async throws -> TaskSummary {
        try await send("POST", "/v1/tasks/\(id.pathEscaped)/cancel", json: nil)
    }

    func archive(_ id: String) async throws {
        _ = try await raw("DELETE", "/v1/tasks/\(id.pathEscaped)", body: nil, headers: [:])
    }

    func waits() async throws -> [WaitItem] { (try await get("/v1/waits") as WaitList).waits }

    /// decision: allow / deny / always / continue / stop / hint；「改一下」= allow + args
    func answer(_ waitID: String, decision: String, note: String? = nil, args: [String: JSONValue]? = nil) async throws -> TaskSummary {
        struct Body: Encodable { var decision: String; var note: String?; var args: [String: JSONValue]? }
        let data = try JSONEncoder().encode(Body(decision: decision, note: note, args: args))
        let (d, _) = try await raw("POST", "/v1/waits/\(waitID.pathEscaped)", body: data, headers: ["Content-Type": "application/json"])
        return try decode(d)
    }

    /// 跨任务搜索：关键字空格分开，都要出现
    func search(_ q: String, archived: Bool = false, limit: Int = 20) async throws -> [SearchHit] {
        var comps = URLComponents()
        comps.queryItems = [URLQueryItem(name: "q", value: q), URLQueryItem(name: "archived", value: archived ? "1" : "0"),
                            URLQueryItem(name: "limit", value: String(limit))]
        let query = comps.percentEncodedQuery ?? ""
        return (try await get("/v1/search?" + query) as SearchList).results
    }

    func uploadFile(_ url: URL) async throws -> BlobInfo {
        let data = try Data(contentsOf: url)
        if data.count > 50 * 1024 * 1024 {
            throw APIError.server(status: 413, code: "too_large", message: "附件太大（上限 50 MB）：\(url.lastPathComponent)")
        }
        let mime = UTType(filenameExtension: url.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
        return try await upload(data, name: url.lastPathComponent, mime: mime)
    }

    func uploadText(_ text: String, name: String = "粘贴的长文本.txt") async throws -> BlobInfo {
        try await upload(Data(text.utf8), name: name, mime: "text/plain")
    }

    private func upload(_ data: Data, name: String, mime: String) async throws -> BlobInfo {
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        let (d, _) = try await raw("POST", "/v1/blobs", body: data, headers: ["Content-Type": mime, "X-Filename": encoded])
        return try decode(d)
    }

    // MARK: 设置页（api.md 2.12）

    func settingsModel() async throws -> ModelSettings { try await get("/v1/settings/model") }

    /// key 传 nil = 沿用已保存的；advanced 里空字符串 = 用默认
    func saveModel(url: String, model: String, key: String?, advanced: [String: String]) async throws {
        var body: [String: Any] = ["url": url, "model": model, "advanced": advanced]
        if let key, !key.isEmpty { body["key"] = key }
        try await sendVoid("PUT", "/v1/settings/model", json: body)
    }

    func mcpList() async throws -> McpList { try await get("/v1/settings/mcp") }

    func mcpParse(_ text: String) async throws -> McpParsed {
        try await send("POST", "/v1/settings/mcp/parse", json: ["text": text])
    }

    @discardableResult
    func mcpAdd(_ servers: [String: JSONValue], overwrite: Bool) async throws -> [String] {
        struct Added: Decodable { var added: [String] }
        let added: Added = try await send("POST", "/v1/settings/mcp",
                                          json: ["servers": servers.mapValues(\.foundation), "overwrite": overwrite])
        return added.added
    }

    /// config 是编辑器里那段 JSON 解析出来的对象；返回最终名字
    func mcpReplace(_ name: String, config: Any, rename: String?) async throws -> String {
        struct Named: Decodable { var name: String }
        var body: [String: Any] = ["config": config]
        if let rename, rename != name { body["rename"] = rename }
        let r: Named = try await send("PUT", "/v1/settings/mcp/\(name.pathEscaped)", json: body)
        return r.name
    }

    func mcpRemove(_ name: String) async throws { try await sendVoid("DELETE", "/v1/settings/mcp/\(name.pathEscaped)", json: nil) }

    func mcpReconnect(_ name: String) async throws {
        try await sendVoid("POST", "/v1/settings/mcp/\(name.pathEscaped)/reconnect", json: nil)
    }

    func mcpLogin(_ name: String) async throws -> LoginInfo {
        try await send("POST", "/v1/settings/mcp/\(name.pathEscaped)/login", json: nil)
    }

    func mcpPresets() async throws -> [McpPreset] {
        struct L: Decodable { var presets: [McpPreset] }
        return (try await get("/v1/settings/mcp/presets") as L).presets
    }

    @discardableResult
    func mcpAddPreset(_ id: String, values: [String: String]) async throws -> String {
        struct Added: Decodable { var added: [String] }
        let r: Added = try await send("POST", "/v1/settings/mcp/presets/\(id.pathEscaped)", json: ["values": values])
        return r.added.first ?? id
    }

    func mcpImport() async throws -> [ImportSource] {
        struct L: Decodable { var sources: [ImportSource] }
        return (try await get("/v1/settings/mcp/import") as L).sources
    }

    func skills() async throws -> SkillList { try await get("/v1/settings/skills") }

    func skill(_ name: String) async throws -> SkillDetail { try await get("/v1/settings/skills/\(name.pathEscaped)") }

    func skillCreate(_ text: String) async throws -> String {
        struct Named: Decodable { var name: String }
        return (try await send("POST", "/v1/settings/skills", json: ["text": text]) as Named).name
    }

    func skillSave(_ name: String, text: String) async throws -> String {
        struct Named: Decodable { var name: String }
        return (try await send("PUT", "/v1/settings/skills/\(name.pathEscaped)", json: ["text": text]) as Named).name
    }

    func skillRemove(_ name: String) async throws {
        try await sendVoid("DELETE", "/v1/settings/skills/\(name.pathEscaped)", json: nil)
    }

    // MARK: 事件流的请求

    func eventsRequest(after cursor: String?) throws -> URLRequest {
        guard let cfg = DaemonConfig.load() else { throw APIError.notRunning }
        var comps = URLComponents(url: cfg.baseURL.appendingPathComponent("/v1/events"), resolvingAgainstBaseURL: false)!
        if let cursor { comps.queryItems = [URLQueryItem(name: "after", value: cursor)] }
        var req = URLRequest(url: comps.url!)
        req.setValue("Bearer \(cfg.token)", forHTTPHeaderField: "Authorization")
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        if let cursor { req.setValue(cursor, forHTTPHeaderField: "Last-Event-ID") }
        req.timeoutInterval = 60 * 60 * 24
        return req
    }

    // MARK: 底层

    private func get<T: Decodable>(_ path: String) async throws -> T {
        let (d, _) = try await raw("GET", path, body: nil, headers: [:])
        return try decode(d)
    }

    private func send<T: Decodable>(_ method: String, _ path: String, json: [String: Any]?) async throws -> T {
        var data: Data? = nil
        if let json { data = try JSONSerialization.data(withJSONObject: json) }
        let (d, _) = try await raw(method, path, body: data, headers: json == nil ? [:] : ["Content-Type": "application/json"])
        return try decode(d)
    }

    /// 不关心返回内容（204 或者只要成功）
    private func sendVoid(_ method: String, _ path: String, json: [String: Any]?) async throws {
        var data: Data? = nil
        if let json { data = try JSONSerialization.data(withJSONObject: json) }
        _ = try await raw(method, path, body: data, headers: json == nil ? [:] : ["Content-Type": "application/json"])
    }

    private func raw(_ method: String, _ path: String, body: Data?, headers: [String: String]) async throws -> (Data, HTTPURLResponse) {
        guard let cfg = DaemonConfig.load() else { throw APIError.notRunning }
        // 不用 appendingPathComponent：它会把查询参数里的 ? 也转义掉（path 里的 id 已经自己转义过）
        guard let url = URL(string: path, relativeTo: cfg.baseURL)?.absoluteURL else { throw APIError.notRunning }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.httpBody = body
        req.setValue("Bearer \(cfg.token)", forHTTPHeaderField: "Authorization")
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }

        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch let e as URLError where [.cannotConnectToHost, .networkConnectionLost, .timedOut, .cannotFindHost, .notConnectedToInternet].contains(e.code) {
            throw APIError.notRunning
        }
        guard let http = resp as? HTTPURLResponse else { throw APIError.notRunning }
        guard (200..<300).contains(http.statusCode) else {
            if let e = try? JSONDecoder().decode(APIErrorBody.self, from: data) {
                throw APIError.server(status: http.statusCode, code: e.error.code, message: e.error.message)
            }
            throw APIError.server(status: http.statusCode, code: "internal", message: "服务返回了 \(http.statusCode)")
        }
        return (data, http)
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(String(describing: error))
        }
    }
}

private extension String {
    var pathEscaped: String { addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? self }
}
