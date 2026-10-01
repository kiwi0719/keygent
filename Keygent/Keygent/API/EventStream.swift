import Foundation

/// SSE 事件（design/api.md 第 3 节）
enum WeaverEvent {
    case task(TaskSummary)
    case wait(WaitItem)
    case waitClosed(task: String, waitID: String)
    case step(task: String, step: Step)
    case delta(task: String, text: String)
    case archived(task: String)
    case reset
}

/// 订阅 `GET /v1/events`。断线 1 秒后重连，失败逐步拉长到 30 秒；带上最后收到的 id 让服务端补发。
final class EventStream {
    private let client: WeaverClient
    private var loop: Task<Void, Never>?
    private var cursor: String?

    /// 每条事件（主线程回调）
    var onEvent: (WeaverEvent) -> Void = { _ in }
    /// 连上 / 断开（主线程回调）
    var onConnected: (Bool) -> Void = { _ in }

    init(client: WeaverClient) {
        self.client = client
    }

    func start() {
        guard loop == nil else { return }
        loop = Task.detached(priority: .utility) { [weak self] in
            var backoff: Double = 1
            while !Task.isCancelled {
                guard let self else { return }
                let gotData = await self.runOnce()
                await MainActor.run { self.onConnected(false) }
                if gotData { backoff = 1 }
                try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
                backoff = min(30, backoff * 2)
            }
        }
    }

    func stop() {
        loop?.cancel()
        loop = nil
    }

    /// 连一次，直到断开。返回这次是否成功收到过数据。
    private func runOnce() async -> Bool {
        guard let req = try? client.eventsRequest(after: cursor) else { return false }
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 60   // 服务端每 15 秒 ping 一次
        cfg.timeoutIntervalForResource = 60 * 60 * 24 * 7
        cfg.connectionProxyDictionary = [:]
        let session = URLSession(configuration: cfg)
        defer { session.invalidateAndCancel() }

        var got = false
        do {
            let (bytes, resp) = try await session.bytes(for: req)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return false }
            await MainActor.run { self.onConnected(true) }
            got = true

            var id: String?
            var event = "message"
            var data = ""
            func handle(_ line: String) {
                if line.isEmpty {
                    if !data.isEmpty || event != "message" { dispatch(id: id, event: event, data: data) }
                    id = nil; event = "message"; data = ""
                    return
                }
                if line.hasPrefix(":") { return }
                let (field, value) = Self.split(line)
                switch field {
                case "id": id = value
                case "event": event = value
                case "data": data += data.isEmpty ? value : "\n" + value
                default: break
                }
            }
            // 不用 bytes.lines：它会丢掉空行，而 SSE 靠空行分隔事件
            var buf: [UInt8] = []
            for try await b in bytes {
                if Task.isCancelled { break }
                if b == 0x0A {
                    var line = String(decoding: buf, as: UTF8.self)
                    if line.hasSuffix("\r") { line.removeLast() }
                    handle(line)
                    buf.removeAll(keepingCapacity: true)
                } else {
                    buf.append(b)
                }
            }
        } catch {
            // 断线，交给外层重连
        }
        return got
    }

    private static func split(_ line: String) -> (String, String) {
        guard let i = line.firstIndex(of: ":") else { return (line, "") }
        var v = line[line.index(after: i)...]
        if v.hasPrefix(" ") { v = v.dropFirst() }
        return (String(line[..<i]), String(v))
    }

    private func dispatch(id: String?, event: String, data: String) {
        if let id { cursor = id }
        guard let ev = Self.parse(event: event, data: data) else { return }
        DispatchQueue.main.async { self.onEvent(ev) }
    }

    static func parse(event: String, data: String) -> WeaverEvent? {
        let dec = JSONDecoder()
        let bytes = Data(data.utf8)
        switch event {
        case "task":
            struct P: Decodable { var summary: TaskSummary }
            return (try? dec.decode(P.self, from: bytes)).map { .task($0.summary) }
        case "wait":
            struct P: Decodable { var wait: WaitItem }
            return (try? dec.decode(P.self, from: bytes)).map { .wait($0.wait) }
        case "wait_closed":
            struct P: Decodable { var task: String; var wait_id: String }
            return (try? dec.decode(P.self, from: bytes)).map { .waitClosed(task: $0.task, waitID: $0.wait_id) }
        case "step":
            struct P: Decodable { var task: String; var step: Step }
            return (try? dec.decode(P.self, from: bytes)).map { .step(task: $0.task, step: $0.step) }
        case "delta":
            struct P: Decodable { var task: String; var text: String }
            return (try? dec.decode(P.self, from: bytes)).map { .delta(task: $0.task, text: $0.text) }
        case "archived":
            struct P: Decodable { var task: String }
            return (try? dec.decode(P.self, from: bytes)).map { .archived(task: $0.task) }
        case "reset":
            return .reset
        default:
            return nil   // ping 等
        }
    }
}
