import AppKit
import ServiceManagement

/// App 自带的 weaverd：注册成登录项（Contents/Library/LaunchAgents/com.keygent.weaverd.plist），
/// 登录后自动起、崩了自动拉起，退出 Keygent 也继续跑。
enum DaemonService {
    static let label = "com.keygent.weaverd"
    private static let service = SMAppService.agent(plistName: "\(label).plist")
    /// 重启之前的报错不算数
    private static var restartedAt = Date.distantPast

    /// 连不上 weaverd 时，给人看的原因
    enum Health: Equatable {
        /// 已注册，在起或者刚崩（看 `lastError`）
        case starting
        /// 用户在「系统设置 › 登录项」里关掉了
        case needsApproval
        /// 没有模型配置，起了也会立刻退出
        case missingConfig
        case failed(String)
    }

    /// 联调：用 WEAVER_DAEMON_JSON 接到别的 weaverd（比如一个临时 home 起的）。这时 home 跟着那个文件走，
    /// 也不碰 launchd 里的那个 weaverd（不注册、不重启），免得测试改到正在用的环境。
    static var external: Bool {
        !(ProcessInfo.processInfo.environment["WEAVER_DAEMON_JSON"] ?? "").isEmpty
    }

    static var weaverHome: URL {
        if external { return DaemonConfig.path.deletingLastPathComponent() }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".weaver")
    }

    static var envFile: URL { weaverHome.appendingPathComponent(".env") }

    /// App 启动时调一次：没注册就注册。已经注册过（包括用户关掉了）不动它。
    @discardableResult
    static func ensureRegistered() -> Health? {
        if external { return nil }
        switch service.status {
        case .enabled:
            return nil
        case .requiresApproval:
            return .needsApproval
        case .notRegistered, .notFound:
            do {
                try service.register()
                return service.status == .requiresApproval ? .needsApproval : nil
            } catch {
                return .failed("注册登录项失败：\(error.localizedDescription)")
            }
        @unknown default:
            return nil
        }
    }

    static func health() -> Health {
        if external { return FileManager.default.fileExists(atPath: envFile.path) ? .starting : .missingConfig }
        switch service.status {
        case .requiresApproval: return .needsApproval
        case .notRegistered, .notFound: return ensureRegistered() ?? .starting
        default: break
        }
        if !FileManager.default.fileExists(atPath: envFile.path) { return .missingConfig }
        if let e = lastError() { return .failed(e) }
        return .starting
    }

    /// 杀掉重起（launchd 会用 App 包里当前的代码）。改了 weaver 代码、补了 .env 之后用。
    static func restart() {
        restartedAt = Date()
        if external {          // 联调时 weaverd 是别人起的：让它自己退出，由起它的人负责再起
            if let pid = DaemonConfig.load()?.pid { kill(pid_t(pid), SIGTERM) }
            return
        }
        if service.status != .enabled { _ = ensureRegistered() }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "-k", "gui/\(getuid())/\(label)"]
        try? p.run()
    }

    /// App 包里的 weaver 代码和正在跑的 weaverd 不是同一份（⌘R 重新构建、App 更新之后）就重启它。
    /// launchd 管的 weaverd 不会跟着 App 重启，不然会一直跑旧代码。重启后任务会自动恢复。
    static func restartIfStale() {
        guard !external, service.status == .enabled,
              let bundled = Bundle.main.url(forResource: "weaverd", withExtension: "stamp"),
              let want = try? String(contentsOf: bundled, encoding: .utf8) else { return }
        let have = try? String(contentsOf: weaverHome.appendingPathComponent("daemon.stamp"), encoding: .utf8)
        if have != want { restart() }
    }

    static func openLoginItemsSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }

    /// daemon.out 里最近一次启动失败的最后一行（2 分钟内写的才算）
    private static func lastError() -> String? {
        let url = weaverHome.appendingPathComponent("daemon.out")
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              let modified = attrs[.modificationDate] as? Date,
              Date().timeIntervalSince(modified) < 120, modified > restartedAt,
              let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        try? handle.seek(toOffset: size > 4096 ? size - 4096 : 0)
        guard let data = try? handle.readToEnd(), let text = String(data: data, encoding: .utf8) else { return nil }
        let line = text.split(whereSeparator: \.isNewline).last.map(String.init)?.trimmingCharacters(in: .whitespaces)
        guard let line, !line.isEmpty, !line.contains("weaverd 已经在运行"), !line.contains("缺少模型配置") else { return nil }
        return line
    }
}
