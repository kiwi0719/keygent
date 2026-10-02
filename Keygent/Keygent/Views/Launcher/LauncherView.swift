import SwiftUI
import UniformTypeIdentifiers

/// ① 呼出：新任务 / 接着做最近任务
struct LauncherView: View {
    @Environment(AppStore.self) private var store
    @FocusState private var focused: InputField?
    @State private var dropTargeted = false

    var body: some View {
        @Bindable var store = store
        let picked = store.pickedTask

        VStack(spacing: 0) {
            chips

            HStack(spacing: 14) {
                PromptGlyph(size: 34)
                TextField(
                    "",
                    text: $store.launcher.query,
                    prompt: Text(picked != nil ? "接着说，或者直接按 ↵ 打开" : "交代一件新事，或从下面选一个接着做；? 开头 = 搜以前的任务")
                        .foregroundColor(K.text4)
                )
                .textFieldStyle(.plain)
                .font(KFont.sans(20, .medium))
                .foregroundStyle(K.ink)
                .focused($focused, equals: .launcher)
                .frame(height: 40)
                .onChange(of: store.launcher.query) { store.launcherQueryChanged() }
                if store.launcher.submitting { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal, 24)
            .padding(.top, 16)
            .padding(.bottom, 18)

            HLine()

            if store.connection == .offline {
                OfflineNotice()
            } else if store.launcher.archivedMode {
                ArchivedList()
            } else if store.launcherSearchMode {
                SearchResults()
            } else {
                recentList
            }
        }
        .frame(width: 780)
        .frame(minHeight: store.launcher.picker || store.launcher.wsPicker ? 560 : nil, alignment: .top)   // 弹层是 overlay，不撑高度：按 5 行窗口 + 翻页条留够
        .overlay {
            if dropTargeted {
                RoundedRectangle(cornerRadius: 16)
                    .strokeBorder(K.green, style: StrokeStyle(lineWidth: 2, dash: [6, 4]))
                    .padding(4)
                    .allowsHitTesting(false)
            }
        }
        .overlay(alignment: .top) {
            if store.launcher.picker {
                ZStack(alignment: .top) {
                    Color.black.opacity(0.12)
                        .onTapGesture { store.launcher.picker = false }
                    FilePickerView()
                        .padding(.top, 70)
                }
            } else if store.launcher.wsPicker {
                ZStack(alignment: .top) {
                    Color.black.opacity(0.12)
                        .onTapGesture { store.launcher.wsPicker = false }
                    WorkspacePickerView()
                        .padding(.top, 70)
                }
            }
        }
        .onDrop(of: [.fileURL], isTargeted: $dropTargeted) { providers in
            for p in providers {
                _ = p.loadObject(ofClass: URL.self) { url, _ in
                    if let url { DispatchQueue.main.async { store.addFiles([url]) } }
                }
            }
            return true
        }
        .syncFocus(store, $focused)
    }

    // MARK: 附件 / 继续 chips

    private var chips: some View {
        FlowLayout(spacing: 8, lineSpacing: 8) {
            if let t = store.pickedTask {
                HStack(spacing: 8) {
                    Dot(color: K.greenSoft, size: 7)
                    Text("继续 · \(t.title)").lineLimit(1)
                    ChipClose(color: K.paper, label: "取消，改为新任务") { store.launcher.pick = nil }
                }
                .font(KFont.sans(12))
                .foregroundStyle(K.paper)
                .padding(.leading, 10)
                .padding(.trailing, 6)
                .frame(height: 30)
                .background(RoundedRectangle(cornerRadius: 6).fill(K.ink))
            }
            if store.pickedTask == nil {
                workspaceChip
            }
            ForEach(store.launcher.attachments) { a in
                Chip(text: a.label, fill: a.url == nil ? K.chip : K.amberBg) {
                    store.removeAttachment(a)
                }
            }
            Button { store.openFilePicker() } label: {
                HStack(spacing: 8) {
                    Text("+ 文件")
                    Kbd("⌘O")
                }
                .font(KFont.sans(12))
                .foregroundStyle(K.text2)
                .padding(.leading, 10)
                .padding(.trailing, 8)
                .frame(height: 30)
                .background(
                    RoundedRectangle(cornerRadius: 6)
                        .strokeBorder(K.dash, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                )
                .contentShape(Rectangle())
            }
            .buttonStyle(PressableStyle())
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// 新任务在哪个文件夹里干活；⌘E 换
    private var workspaceChip: some View {
        let wd = store.launcher.workdir
        return Button { store.openWorkspacePicker() } label: {
            HStack(spacing: 8) {
                Text(wd.map { "在 \(Workspaces.name($0))" } ?? "临时目录")
                    .lineLimit(1)
                Kbd("⌘E")
            }
            .font(KFont.sans(12))
            .foregroundStyle(wd == nil ? K.text2 : K.ink)
            .padding(.leading, 10)
            .padding(.trailing, 8)
            .frame(height: 30)
            .background(RoundedRectangle(cornerRadius: 6).fill(wd == nil ? Color.clear : K.chip))
            .overlay {
                if wd == nil {
                    RoundedRectangle(cornerRadius: 6)
                        .strokeBorder(K.dash, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .help(wd.map(Workspaces.short) ?? "每个任务一个空目录")
    }

    // MARK: 最近任务

    private var recentList: some View {
        let L = store.launcher
        let full = store.launcherFull
        let w = store.launcherWindow

        return VStack(spacing: 2) {
            HStack {
                SectionLabel("最近任务 · 选一个接着做")
                Spacer()
                Text("⌘ + 数字 = 眼前第几条")
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text4)
                Button { store.openArchived() } label: {
                    HStack(spacing: 5) {
                        Kbd("⌘⇧A")
                        Text("已归档")
                    }
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text3)
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableStyle())
                .padding(.leading, 10)
            }
            .padding(.horizontal, 8)
            .padding(.top, 4)
            .padding(.bottom, 8)

            if !store.loadedOnce {
                VStack(spacing: 8) {
                    SkeletonBar()
                    SkeletonBar()
                    SkeletonBar()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            } else if store.tasks.isEmpty {
                Text("还没有任务 · 在上面写一句话交给 Agent")
                    .font(KFont.sans(13))
                    .foregroundStyle(K.text4)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 22)
            }

            ForEach(store.launcherRows, id: \.task.id) { row in
                LauncherRow(task: row.task, slot: row.slot, selected: L.pick == row.index) {
                    store.pickLauncher(row.index)
                }
            }

            if store.loadedOnce, L.shown < store.tasks.count {
                Button { store.launcherMore() } label: {
                    HStack(spacing: 4) {
                        Text("展开更多")
                            .font(KFont.sans(13))
                            .foregroundStyle(K.text2)
                        Text("· 还有 \(store.tasks.count - L.shown) 条 · 或在最后一条按 ↓")
                            .font(KFont.mono(11))
                            .foregroundStyle(K.text4)
                    }
                    .frame(maxWidth: .infinity)
                    .frame(height: 40)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .strokeBorder(K.dash, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableStyle())
                .padding(.horizontal, 8)
                .padding(.top, 6)
                .padding(.bottom, 4)
            }

            if full, store.tasks.count > LauncherState.viewCount {
                WindowBar(window: w.top..<(w.top + w.count), total: store.tasks.count) { store.launcherScroll($0) }
                .padding(.horizontal, 8)
                .padding(.top, 8)
                .padding(.bottom, 2)
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 8)
    }
}

// MARK: - 子视图

/// 搜索结果：命中的任务、哪一类记录、前后一段摘录
private struct SearchResults: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        let L = store.launcher
        let win = store.resultWindow
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                SectionLabel(L.searchedFor.isEmpty ? "搜索以前的任务" : "“\(L.searchedFor)” · \(L.results.count) 条")
                Spacer()
                if L.searching { ProgressView().controlSize(.mini) }
                if !L.results.isEmpty {
                    Text("⌘ + 数字 = 眼前第几条")
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text4)
                }
            }
            .padding(.horizontal, 8)
            .padding(.top, 4)
            .padding(.bottom, 8)

            if store.launcherSearchTerms.isEmpty {
                hint("在 ? 后面写关键字，比如：? 华北 阈值")
            } else if !L.searching && L.results.isEmpty && !L.searchedFor.isEmpty {
                hint("没有同时包含这些关键字的记录")
            }

            VStack(spacing: 2) {
                ForEach(win, id: \.self) { i in
                    let h = L.results[i]
                    Button { store.openSearchHit(h) } label: {
                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 6) {
                                    Text(h.taskTitle)
                                        .font(KFont.sans(14, .bold))
                                        .foregroundStyle(K.ink)
                                        .lineLimit(1)
                                    Text((h.archived ? "已归档 · " : "") + (h.sub.isEmpty ? h.kind : "子 Agent · \(h.kind)"))
                                        .font(KFont.sans(11, .medium))
                                        .foregroundStyle(K.text3)
                                        .padding(.horizontal, 6)
                                        .padding(.vertical, 1)
                                        .background(Capsule().fill(K.line3))
                                }
                                Text(h.excerpt)
                                    .font(KFont.sans(12))
                                    .foregroundStyle(K.text2)
                                    .lineLimit(2)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            Text(TimeText.relative(h.ts))
                                .font(KFont.mono(12))
                                .foregroundStyle(K.text4)
                            Kbd("⌘\(i - win.lowerBound + 1)", active: L.resultPick == i, size: 12, weight: .medium, minWidth: 34)
                        }
                        .padding(.vertical, 9)
                        .padding(.horizontal, 12)
                    }
                    .buttonStyle(RowStyle(selected: L.resultPick == i, selectedFill: .white, radius: 8,
                                          selectedStroke: K.ink))
                }
            }

            if L.results.count > ListWindow.size {
                WindowBar(window: win, total: L.results.count, up: "↑ 往上", down: "↓ 往下") { store.resultScroll($0) }
                    .padding(.horizontal, 8)
                    .padding(.top, 8)
                    .padding(.bottom, 2)
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 8)
    }

    private func hint(_ s: String) -> some View {
        Text(s)
            .font(KFont.sans(13))
            .foregroundStyle(K.text4)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 22)
    }
}

/// 读不到 daemon.json 或连不上。weaverd 是 App 自带的登录项，这里说清楚卡在哪、给一个能点的下一步。
struct OfflineNotice: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Dot(color: K.dash, size: 8)
                Text(title).font(KFont.sans(14, .bold))
            }
            Text(message)
                .font(KFont.sans(13))
                .foregroundStyle(K.text3)
                .fixedSize(horizontal: false, vertical: true)
            if let code {
                Text(code)
                    .font(KFont.mono(12))
                    .textSelection(.enabled)
                    .lineLimit(3)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(RoundedRectangle(cornerRadius: 6).fill(K.line3))
            }
            HStack(spacing: 8) {
                if store.daemon == .needsApproval {
                    Button("打开登录项设置") { DaemonService.openLoginItemsSettings() }
                }
                if store.daemon == .missingConfig || isFailed {
                    Button("配置模型…  ⌘,") { store.openSettings() }
                }
                if store.daemon != .needsApproval, store.daemon != .missingConfig {
                    Button("重启 Weaver") { store.restartDaemon() }
                }
            }
            .controlSize(.small)
            .padding(.top, 2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 28)
        .padding(.vertical, 20)
    }

    private var isFailed: Bool {
        if case .failed = store.daemon { return true }
        return false
    }

    private var title: String {
        switch store.daemon {
        case .needsApproval: return "Weaver 的后台服务被关掉了"
        case .missingConfig: return "还没配置模型"
        case .failed: return "Weaver 没起来"
        case .starting, nil: return "Weaver 正在启动"
        }
    }

    private var message: String {
        switch store.daemon {
        case .needsApproval: return "在「系统设置 › 通用 › 登录项」里打开 Keygent，起来后会自动连上。"
        case .missingConfig: return "选好服务商、填上模型和 API Key 就能用了。"
        case .failed: return "最近一次启动的报错："
        case .starting, nil: return "起来后会自动连上（每 3 秒试一次）。"
        }
    }

    private var code: String? {
        switch store.daemon {
        case .failed(let e): return e
        default: return nil
        }
    }
}

private struct LauncherRow: View {
    let task: TaskSummary
    let slot: Int
    let selected: Bool
    let action: () -> Void

    var body: some View {
        let k = task.kind
        Button(action: action) {
            HStack(spacing: 14) {
                Dot(color: k.color, size: 8)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(task.title)
                            .font(KFont.sans(14, .bold))
                            .foregroundStyle(K.ink)
                            .lineLimit(1)
                        if task.waiting > 0 {
                            Text("\(task.waiting) 件等你")
                                .font(KFont.sans(11, .medium))
                                .foregroundStyle(K.ink)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(K.amberBg))
                        }
                    }
                    HStack(spacing: 0) {
                        if !Workspaces.isScratch(task.workdir) {
                            Text("\(Workspaces.name(task.workdir)) · ")
                                .font(KFont.mono(11))
                                .foregroundStyle(K.text4)
                        }
                        Text(task.line)
                            .font(KFont.sans(12))
                            .foregroundStyle(k == .error ? K.red : K.text3)
                    }
                    .lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                TimelineView(.periodic(from: .now, by: 30)) { _ in
                    Text(TimeText.relative(task.updated))
                        .font(KFont.mono(12))
                        .foregroundStyle(K.text4)
                }
                Kbd("⌘\(slot + 1)", active: selected, size: 12, weight: .medium, minWidth: 34)
            }
            .padding(.vertical, 10)
            .padding(.horizontal, 12)
        }
        .buttonStyle(RowStyle(selected: selected, selectedFill: .white, radius: 8, selectedStroke: K.ink))
    }
}

struct Chip: View {
    let text: String
    let fill: Color
    let onRemove: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Text(text).lineLimit(1)
            ChipClose(color: K.ink, label: "移除", action: onRemove)
        }
        .font(KFont.sans(12))
        .foregroundStyle(K.ink)
        .padding(.leading, 10)
        .padding(.trailing, 6)
        .frame(height: 30)
        .background(RoundedRectangle(cornerRadius: 6).fill(fill))
    }
}

private struct ChipClose: View {
    let color: Color
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text("×")
                .font(KFont.sans(14))
                .foregroundStyle(color)
                .frame(width: 22, height: 22)
                .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel(label)
    }
}

struct SkeletonBar: View {
    @State private var on = false
    var height: CGFloat = 40

    var body: some View {
        RoundedRectangle(cornerRadius: 8)
            .fill(K.skeleton)
            .frame(height: height)
            .opacity(on ? 0.55 : 1)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true)) { on = true }
            }
    }
}
