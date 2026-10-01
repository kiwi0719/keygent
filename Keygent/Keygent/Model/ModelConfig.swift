import Foundation

/// ~/.weaver/.env：按行读写，只改我们管的那几项，注释和其他配置原样保留。
/// 解析规则和 weaver 的 load_env 一致：`KEY=VALUE`，去掉引号，同一个 key 以第一次出现的为准。
struct EnvFile {
    var lines: [String] = []

    static func load(_ url: URL) -> EnvFile {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return EnvFile() }
        var lines = text.components(separatedBy: "\n")
        if lines.last == "" { lines.removeLast() }
        return EnvFile(lines: lines)
    }

    private static func parse(_ line: String) -> (key: String, value: String)? {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, !t.hasPrefix("#"), let eq = t.firstIndex(of: "=") else { return nil }
        let key = t[..<eq].trimmingCharacters(in: .whitespaces)
        let value = t[t.index(after: eq)...].trimmingCharacters(in: .whitespaces)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\"")).trimmingCharacters(in: CharacterSet(charactersIn: "'"))
        return (key, value)
    }

    subscript(key: String) -> String? {
        for l in lines { if let kv = Self.parse(l), kv.key == key { return kv.value.isEmpty ? nil : kv.value } }
        return nil
    }

    /// nil = 删掉这一项
    mutating func set(_ key: String, _ value: String?) {
        let v = value?.replacingOccurrences(of: "\n", with: "").trimmingCharacters(in: .whitespaces)
        var out: [String] = []
        var done = false
        for l in lines {
            guard Self.parse(l)?.key == key else { out.append(l); continue }
            if !done, let v, !v.isEmpty { out.append("\(key)=\(v)") }
            done = true
        }
        if !done, let v, !v.isEmpty { out.append("\(key)=\(v)") }
        lines = out
    }

    /// 先建成 0600 再写，任何时候别人都读不到（和 daemon.json 一样）
    func write(to url: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let tmp = url.deletingLastPathComponent().appendingPathComponent(".env.tmp-\(getpid())")
        fm.createFile(atPath: tmp.path, contents: nil, attributes: [.posixPermissions: 0o600])
        try Data((lines.joined(separator: "\n") + "\n").utf8).write(to: tmp)
        _ = try fm.replaceItemAt(url, withItemAt: tmp)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
}

/// 「模型」设置：只要地址、模型、Key。是哪家、什么协议由 weaver 按地址认（providers.guess_provider）。
struct ModelSettingsState {
    var url = ""
    var model = ""
    /// 新填的 key；留空 = 沿用已保存的
    var key = ""
    var savedKey: String? = nil
    /// 打开时的地址；没改地址就保留原来写死的 WEAVER_PROVIDER（命令行用户可能配过）
    var savedURL = ""
    /// 还没配过模型：没有「取消」可言
    var firstRun = true
    var error: String? = nil

    /// 只配了 WEAVER_PROVIDER、没写地址时，显示这家的默认地址（和 weaver PRESETS 对应）
    private static let presetURLs = [
        "openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1", "kimi": "https://api.moonshot.cn/v1",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1", "glm": "https://open.bigmodel.cn/api/paas/v4",
        "siliconflow": "https://api.siliconflow.cn/v1", "groq": "https://api.groq.com/openai/v1",
        "together": "https://api.together.xyz/v1", "xai": "https://api.x.ai/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "ollama": "http://localhost:11434/v1", "lmstudio": "http://localhost:1234/v1", "vllm": "http://localhost:8000/v1",
        "anthropic": "https://api.anthropic.com", "openrouter-anthropic": "https://openrouter.ai/api",
        "deepseek-anthropic": "https://api.deepseek.com/anthropic", "kimi-anthropic": "https://api.moonshot.cn/anthropic",
    ]

    init() {}

    init(env: EnvFile) {
        let legacyKey = env["OPENROUTER_API_KEY"]
        let provider = env["WEAVER_PROVIDER"] ?? (legacyKey != nil ? "openrouter" : nil)
        let legacy = provider?.hasPrefix("openrouter") == true
        url = env["WEAVER_BASE_URL"] ?? provider.flatMap { Self.presetURLs[$0] } ?? ""
        savedURL = url
        model = env["WEAVER_MODEL"] ?? (legacy ? env["OPENROUTER_MODEL"] : nil) ?? ""
        savedKey = env["WEAVER_API_KEY"] ?? (legacy ? legacyKey : nil)
        firstRun = url.isEmpty || model.isEmpty
    }

    private static func host(_ s: String) -> String? {
        URL(string: s.trimmingCharacters(in: .whitespaces))?.host?.lowercased()
    }

    /// 已保存的 key 还能不能接着用：地址还是同一家
    var canReuseKey: Bool {
        savedKey != nil && Self.host(url) != nil && Self.host(url) == Self.host(savedURL)
    }

    /// 本机的服务一般不要 key
    var isLocal: Bool {
        guard let h = Self.host(url) else { return false }
        return h == "localhost" || h == "127.0.0.1" || h == "::1" || h.hasSuffix(".local")
    }

    /// 检查并写进 env；返回给人看的错误
    func apply(to env: inout EnvFile) -> String? {
        let url = url.trimmingCharacters(in: .whitespaces)
        let model = model.trimmingCharacters(in: .whitespaces)
        let typed = key.trimmingCharacters(in: .whitespaces)
        guard let u = URL(string: url), ["http", "https"].contains(u.scheme ?? ""), u.host != nil else {
            return url.isEmpty ? "填一下接口地址" : "地址要以 http:// 或 https:// 开头"
        }
        if model.isEmpty { return "填一下模型名" }
        let key: String? = typed.isEmpty ? (canReuseKey ? savedKey : nil) : typed
        if key == nil, !isLocal { return "填一下 API Key" }

        if url != savedURL {
            // 换了地址：让 weaver 按地址重新认是哪家
            env.set("WEAVER_PROVIDER", nil)
            env.set("WEAVER_PROTOCOL", nil)
        }
        env.set("WEAVER_BASE_URL", url)
        env.set("WEAVER_MODEL", model)
        env.set("WEAVER_API_KEY", key)
        // 旧写法已经并进 WEAVER_*，留着会让人以为改它有用
        env.set("OPENROUTER_API_KEY", nil)
        env.set("OPENROUTER_MODEL", nil)
        return nil
    }
}
