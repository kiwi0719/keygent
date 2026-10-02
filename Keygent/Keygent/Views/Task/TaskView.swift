import SwiftUI

/// ③ 任务：启动器原地展开成这个任务。结果在上，要你决定的事紧跟着，过程默认折叠。
struct TaskView: View {
    @Environment(AppStore.self) private var store
    @FocusState private var focused: InputField?
    /// 顶栏、底部（输入框 + 底栏）、中间内容的实际高度：中间那块最多占到面板放得下的高度，超出就滚动
    @State private var headerH: CGFloat = 62
    @State private var bottomH: CGFloat = 110
    @State private var bodyH: CGFloat = 0

    var body: some View {
        @Bindable var store = store
        let T = store.task
        let kind = store.taskKind
        let inInput = store.focusedField == .task

        VStack(spacing: 0) {
            // 顶栏
            HStack(spacing: 12) {
                // 输入中 Esc = 离开输入框，过程展开时 Esc = 收起过程，这两种情况返回按钮不标 esc
                BackButton(label: "回到启动器", showEsc: !inInput && !T.proc) {
                    store.backToLauncherFromTask()
                }
                Text(T.summary?.title ?? "")
                    .font(KFont.sans(18, .bold))
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button { store.openDetail(from: .task) } label: {
                    HStack(spacing: 6) {
                        Kbd("⌘⇧↵")
                        Text("全屏详情")
                    }
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text4)
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableStyle())
                if kind.isActive {
                    Button { store.taskCancel() } label: {
                        HStack(spacing: 6) {
                            Kbd("⌘⌫")
                            Text("停下")
                        }
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text4)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableStyle())
                }
                HStack(spacing: 8) {
                    Dot(color: kind.color, size: 8)
                    Text(kind.label)
                        .font(KFont.sans(13))
                        .foregroundStyle(K.text2)
                }
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 16)
            .overlay(alignment: .bottom) { HLine() }
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { headerH = $0 }

            ScrollView {
                VStack(spacing: 0) {
                    VStack(spacing: 14) {
                        if T.archived {
                            ArchivedBanner()
                        }
                        // 清单：它列了步骤就一直看得见（视频里胶囊下那张小卡的同一份）
                        if !store.taskTodos.isEmpty {
                            TodoCard(todos: store.taskTodos, settled: !kind.isActive)
                        }
                        // 这一轮还在跑 / 停在等你：看它正在做的事；跑完了：看结论
                        if kind == .run || kind == .wait {
                            LiveRound()
                            if !store.taskChanges.isEmpty {
                                ChangesList(changes: store.taskChanges, inCard: false)
                            }
                        } else {
                            ResultCard()
                        }

                        if let jobs = T.detail?.jobs, !jobs.isEmpty {
                            JobsStrip(jobs: jobs)
                        }

                        if let w = store.taskQuestion {
                            QuestionCard(wait: w, pick: T.optionPick, busy: T.answering == w.id,
                                         locked: store.questionLockUntil != nil,
                                         more: store.taskWaits.count - (store.taskGate == nil ? 1 : 2),
                                         onOption: { store.questionOption(w, $0) },
                                         onSkip: { store.questionSkip(w) },
                                         onLater: { store.questionLater(w) },
                                         onMore: { store.openQueue() })
                        }
                        if let w = store.taskElicit {
                            ElicitCard(wait: w, busy: T.answering == w.id, focused: $focused)
                        }
                        if let w = store.taskGate {
                            GateCard(wait: w, more: store.taskWaits.count - (store.taskQuestion == nil ? 1 : 2),
                                     busy: T.answering == w.id,
                                     onChoose: { store.taskChoose(w, $0) },
                                     onMore: { store.openQueue() })
                        } else if store.taskQuestion == nil, kind == .error, let s = T.summary {
                            ErrorCard(note: s.note.isEmpty ? s.now : s.note) { store.requestFocus(.task) }
                        }

                        Button { store.task.proc.toggle() } label: {
                            HStack {
                                // 正在干什么已经在上面那张卡里一步步列着，这里只给步数和用时，别说两遍
                                Text(store.taskProcSummary)
                                    .lineLimit(1)
                                Spacer()
                                HStack(spacing: 6) {
                                    Text("⌘.")
                                    if T.proc && !inInput { Kbd("esc") }
                                    Text(T.proc ? "收起" : "看过程")
                                }
                                .font(KFont.mono(13))
                            }
                            .font(KFont.sans(13))
                            .foregroundStyle(K.text3)
                            .padding(.vertical, 8)
                            .padding(.horizontal, 2)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(PressableStyle())

                        if T.proc {
                            ProcessPanel()
                        }
                    }
                    .padding(.horizontal, 24)
                    .padding(.vertical, 18)

                    // 最近两句对话
                    if !store.taskTalk.isEmpty {
                        VStack(spacing: 8) {
                            ForEach(store.taskTalk, id: \.index) { t in
                                let m = t.step
                                HStack {
                                    if m.kind == .you { Spacer(minLength: 120) }
                                    Button {
                                        store.task.step = t.index
                                        store.task.proc = true
                                    } label: {
                                        Bubble(text: m.text, kind: m.bubbleKind, lineLimit: 3)
                                            .contentShape(Rectangle())
                                    }
                                    .buttonStyle(PressableStyle())
                                    .help("看全文")
                                    if m.kind != .you { Spacer(minLength: 120) }
                                }
                            }
                        }
                        .padding(.horizontal, 24)
                        .padding(.top, 12)
                        .padding(.bottom, 4)
                        .overlay(alignment: .top) { HLine() }
                    }
                }
                .background(ScrollNudger { store.taskScrollBy = $0 })
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { bodyH = $0 }
            }
            .frame(maxHeight: bodyMax)
            .fixedSize(horizontal: false, vertical: true)

            VStack(spacing: 0) {
                if T.archived {
                    FooterBar {
                        Text("已归档 · 只能看，恢复后才能接着说").foregroundStyle(K.text3)
                    } trailing: {
                        HStack(spacing: 14) {
                            if !store.taskChanges.isEmpty { HStack(spacing: 5) { Kbd("⌘D"); Text("看改动") } }
                            HStack(spacing: 5) { Kbd("⌘R"); Text("恢复") }
                        }
                    }
                } else {
                HStack(spacing: 10) {
                    if store.answerMode {
                        Text("回答")
                            .font(KFont.sans(11, .medium))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(K.amberBg))
                    } else if let w = T.hintFor {
                        Text(w.kind == "stuck" ? "提示" : "说明")
                            .font(KFont.sans(11, .medium))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(K.amberBg))
                    }
                    TextField("", text: $store.task.draft,
                              prompt: Text(placeholder).foregroundColor(K.text4))
                        .textFieldStyle(.plain)
                        .font(KFont.sans(15))
                        .focused($focused, equals: .task)
                        .frame(height: 36)
                    if T.sending { ProgressView().controlSize(.small) }
                }
                .padding(.horizontal, 24)
                .padding(.top, 10)
                .padding(.bottom, 14)
                .overlay(alignment: .top) { if store.taskTalk.isEmpty || overflows { HLine() } }

                FooterBar {
                    Text((T.proc ? "↑↓ 选步骤 · " : overflows ? "↑↓ 滚动 · " : "") + (store.taskChanges.isEmpty ? "⌘C 复制结论 · 直接拖到任意应用"
                                                          : "⌘C 复制结论 · ⌘D 看改动 · ⌘Z 撤销"))
                } trailing: {
                    EnterHint(label: T.hintFor != nil || store.answerMode ? "发给它" : "接着做")
                }
                }
            }
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { bottomH = $0 }
        }
        .frame(width: 780)
        .syncFocus(store, $focused)
    }

    private var bodyMax: CGFloat { max(160, store.panelMaxHeight - headerH - bottomH) }
    private var overflows: Bool { bodyH > bodyMax + 1 }

    private var placeholder: String {
        if let w = store.taskQuestion {
            return w.questionOptions.isEmpty ? "直接打字回答，↵ 发送" : "直接打字回答，或者 ↑↓ 选一个、⌘数字 直接选"
        }
        if let w = store.task.hintFor {
            return w.kind == "stuck" ? "给它一个提示，比如：试试换个网址" : "告诉它为什么不行"
        }
        return store.taskKind.isActive ? "tab 进入 · 插一句话，它在下一步之前会看到" : "tab 进入 · 接着说，开启新的一轮"
    }
}

/// 归档的任务：顶上一条灰色横条
private struct ArchivedBanner: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "archivebox").font(.system(size: 12)).foregroundStyle(K.text3)
            Text("已归档" + (store.task.summary?.archivedAt.map { " · " + TimeText.relative($0) } ?? ""))
                .font(KFont.sans(13, .medium))
            Text("只能看").font(KFont.sans(12)).foregroundStyle(K.text3)
            Spacer()
            Button { if let id = store.task.id { store.restoreTask(id) } } label: {
                HStack(spacing: 6) { Text("恢复"); Kbd("⌘R") }
                    .font(KFont.sans(12))
                    .foregroundStyle(K.ink)
                    .contentShape(Rectangle())
            }
            .buttonStyle(PressableStyle())
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 10).fill(K.line3))
    }
}

// MARK: - 结果

private struct ResultCard: View {
    @Environment(AppStore.self) private var store
    @State private var fullH: CGFloat = 0

    var body: some View {
        let T = store.task
        let final = T.detail?.final ?? ""
        let kind = store.taskKind

        VStack(spacing: 0) {
            // 还没结论时不挂「结果 · 0 步 · 0 tokens」这个空表头，整张卡就是一行「在干什么」
            if !final.isEmpty || T.loading {
            HStack {
                Text("结果")
                Spacer()
                if let u = T.detail?.usage, u.steps > 0 || u.tokens > 0 {
                    Text("\(u.steps) 步 · \(formatTokens(u.tokens)) tokens"
                         + ((u.extractTokens ?? 0) > 0 ? " · 记忆 \(formatTokens(u.extractTokens!))" : ""))
                        .font(KFont.mono(11))
                        .help((u.extractTokens ?? 0) > 0 ? "任务结束后自动提取记忆用掉的 token（不算在这一轮的预算里）" : "")
                }
            }
            .font(KFont.sans(12))
            .foregroundStyle(K.text3)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .overlay(alignment: .bottom) { HLine(color: K.line2) }
            }

            Group {
                if !final.isEmpty {
                    // 太长就折叠到 cap，space / 点按钮展开；展开后整页一起滚，不在卡片里套一层滚动
                    let cap: CGFloat = T.proc ? 120 : 280
                    let folds = fullH > cap + 40
                    let folded = folds && !T.resultOpen
                    MarkdownView(text: final, baseDir: T.summary?.workdir)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 12)
                        .fixedSize(horizontal: false, vertical: true)
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { fullH = $0 }
                        .frame(maxHeight: folded ? cap : nil, alignment: .top)
                        .clipped()
                        .overlay(alignment: .bottom) {
                            if folded {
                                LinearGradient(colors: [.white.opacity(0), .white], startPoint: .top, endPoint: .bottom)
                                    .frame(height: 36)
                                    .allowsHitTesting(false)
                            }
                        }
                    if folds {
                        Button { store.taskToggleResult() } label: {
                            HStack(spacing: 6) {
                                Text(T.resultOpen ? "收起" : "展开全文")
                                Kbd("space")
                            }
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text3)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 8)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(PressableStyle())
                        .overlay(alignment: .top) { HLine(color: K.line2) }
                    }
                } else if T.loading {
                    VStack(spacing: 8) {
                        SkeletonBar(height: 14)
                        SkeletonBar(height: 14)
                    }
                    .padding(14)
                } else {
                    PendingRow(text: pendingText, kind: kind, writing: !T.delta.isEmpty, since: store.taskRoundStart)
                }
            }
            if !store.taskChanges.isEmpty {
                ChangesList(changes: store.taskChanges)
            }
        }
        .background(RoundedRectangle(cornerRadius: 10).fill(.white))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(K.line))
        .onDrag { NSItemProvider(object: final as NSString) }
        .help("拖到任意应用即可粘贴结论")
    }

    private var pendingText: String {
        let T = store.task
        if !T.delta.isEmpty { return "…" + T.delta.suffix(120).trimmingCharacters(in: .whitespacesAndNewlines) }
        switch store.taskKind {
        case .wait: return "等你决定下面这件事"
        case .queued: return "排队中 · 同时在跑的已满"
        case .run: return T.summary?.now.isEmpty == false ? T.summary!.now : "正在开始"
        case .cancelled: return "这一轮已取消 · 可以接着说"
        case .error: return "没有结论"
        case .done: return T.summary?.now.isEmpty == false ? T.summary!.now : "没有结论"
        }
    }

    private func formatTokens(_ n: Int) -> String {
        n >= 10_000 ? String(format: "%.1f 万", Double(n) / 10_000) : "\(n)"
    }
}

/// 还没结论时结果卡里那一行：左边一个状态记号，中间它此刻在干什么，右边这一轮已经跑了多久。
private struct PendingRow: View {
    let text: String
    let kind: TaskKind
    let writing: Bool
    let since: Double

    var body: some View {
        HStack(spacing: 12) {
            Group {
                if kind == .run { WorkingDots(color: K.green) }
                else if kind == .queued { WorkingDots(color: K.dash) }
                else { Dot(color: kind.color, size: 7) }
            }
            .frame(width: 22)

            Text(text)
                .font(writing ? KFont.sans(13) : KFont.sans(14))
                .foregroundStyle(writing ? K.text3 : K.text2)
                .lineLimit(writing ? 2 : 1)
                .truncationMode(writing ? .head : .tail)
                .frame(maxWidth: .infinity, alignment: .leading)

            if kind == .run || kind == .queued, since > 0 {
                TimelineView(.periodic(from: .now, by: 1)) { ctx in
                    Text(TimeText.duration(ctx.date.timeIntervalSince1970 - since))
                        .monospacedDigit()
                }
                .font(KFont.mono(12))
                .foregroundStyle(K.text4)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 三个依次起伏的小点，代替系统转圈
private struct WorkingDots: View {
    var color: Color
    @State private var on = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 4) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(color)
                    .frame(width: 5, height: 5)
                    .opacity(reduceMotion ? 0.8 : (on ? 1 : 0.25))
                    .animation(reduceMotion ? nil : .easeInOut(duration: 0.55).repeatForever().delay(Double(i) * 0.18), value: on)
            }
        }
        .onAppear { on = true }
    }
}

// MARK: - 这一轮正在做的事（照 Claude App：一步一行往下长，不来回换字）

struct LiveRound: View {
    @Environment(AppStore.self) private var store
    /// 卡里最多列几行；再早的折成一行「前面还有 N 步」
    var limit = 6
    /// 点某一行（参数是在 taskSteps 里的下标）；不给就在 ③ 里展开过程、选中那一步
    var onPick: ((Int) -> Void)? = nil
    /// 点「前面还有 N 步」
    var onMore: (() -> Void)? = nil

    var body: some View {
        let T = store.task
        let kind = store.taskKind
        let items = store.taskRoundItems
        let shown = Array(items.suffix(limit))
        let hidden = items.count - shown.count
        let tools = items.filter { $0.step.kind == .step && $0.step.status != "note" }.count
        let busy = items.contains { $0.step.status == "running" || $0.step.status == "waiting" }
        let since = store.taskRoundStart

        VStack(spacing: 0) {
            HStack {
                Text(kind == .wait ? "等你决定" : "进行中")
                Spacer()
                if since > 0 {
                    TimelineView(.periodic(from: .now, by: 1)) { ctx in
                        Text((tools > 0 ? "\(tools) 步 · " : "") + TimeText.duration(ctx.date.timeIntervalSince1970 - since))
                            .monospacedDigit()
                    }
                    .font(KFont.mono(11))
                }
            }
            .font(KFont.sans(12))
            .foregroundStyle(K.text3)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .overlay(alignment: .bottom) { HLine(color: K.line2) }

            VStack(alignment: .leading, spacing: 2) {
                if hidden > 0 {
                    Button { if let onMore { onMore() } else { store.task.proc = true } } label: {
                        Text(onMore == nil ? "前面还有 \(hidden) 步 · ⌘. 看全部" : "前面还有 \(hidden) 步 · 在时间线上往回走")
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text4)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableStyle())
                }
                ForEach(shown, id: \.index) { it in
                    Button {
                        if let onPick { onPick(it.index) } else {
                            store.task.step = it.index
                            store.task.proc = true
                        }
                    } label: {
                        LiveItem(step: it.step)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(RowStyle(selected: false, selectedFill: K.line3, radius: 6))
                    .transition(.opacity)
                }
                // 没有在跑的工具、也没停下等你：就是模型在想 / 在写
                if kind == .run, !busy {
                    ThinkingLine(delta: T.delta)
                        .transition(.opacity)
                }
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .animation(.easeOut(duration: 0.18), value: items.count)
            .animation(.easeOut(duration: 0.18), value: busy)
        }
        .background(RoundedRectangle(cornerRadius: 10).fill(.white))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(K.line))
    }
}

/// 一行：模型说的话照原样；工具调用 = 图标 + 动作 + 对象，右边只在不顺利时才说话
private struct LiveItem: View {
    let step: Step

    var body: some View {
        switch step.kind {
        case .agent:
            Text(step.text.trimmingCharacters(in: .whitespacesAndNewlines))
                .font(KFont.sans(13.5))
                .foregroundStyle(K.text2)
                .lineLimit(3)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
        case .you, .step:
            let (verb, obj) = step.splitTitle
            HStack(spacing: 10) {
                Image(systemName: step.toolIcon)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(step.status == "note" ? K.dash : K.text4)
                    .frame(width: 16)
                HStack(spacing: 6) {
                    Text(verb)
                        .font(KFont.sans(13, step.status == "note" ? .regular : .medium))
                        .foregroundStyle(step.status == "note" ? K.text4 : K.text2)
                        .fixedSize()
                    if !obj.isEmpty {
                        Text(obj)
                            .font(step.monoTitle ? KFont.mono(12) : KFont.sans(13))
                            .foregroundStyle(K.text3)
                            .strikethrough(step.status == "denied", color: K.text4)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                trailing
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
        }
    }

    @ViewBuilder private var trailing: some View {
        switch step.status {
        case "running": WorkingDots(color: K.green)
        case "waiting": Text(step.tool == "ask_user" ? "等你回答" : "等你放行").font(KFont.sans(12)).foregroundStyle(K.amber)
        case "error": Text("出错").font(KFont.sans(12)).foregroundStyle(K.red)
        case "interrupted": Text("中断").font(KFont.sans(12)).foregroundStyle(K.red)
        case "denied": Text("被拒绝").font(KFont.sans(12)).foregroundStyle(K.text4)
        case "cancelled": Text("取消").font(KFont.sans(12)).foregroundStyle(K.text4)
        default: EmptyView()
        }
    }
}

/// 模型在想 / 在写的那一行：在写就滚动显示正在写的最后一点，不然就是「思考中」
private struct ThinkingLine: View {
    let delta: String

    var body: some View {
        HStack(spacing: 10) {
            WorkingDots(color: K.green)
                .frame(width: 16)
            Text(delta.isEmpty ? "思考中" : String(delta.suffix(160)).trimmingCharacters(in: .whitespacesAndNewlines))
                .font(KFont.sans(13))
                .foregroundStyle(delta.isEmpty ? K.text4 : K.text3)
                .lineLimit(2)
                .truncationMode(.head)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
    }
}

// MARK: - 后台（后台命令 / 后台子 Agent）

private struct JobsStrip: View {
    @Environment(AppStore.self) private var store
    let jobs: [JobInfo]

    var body: some View {
        let running = jobs.filter { $0.status == "running" }
        let shown = running.isEmpty ? Array(jobs.suffix(2)) : running
        VStack(alignment: .leading, spacing: 4) {
            Text(running.isEmpty ? "后台 · 都结束了" : "后台 · \(running.count) 个在跑")
                .font(KFont.sans(12))
                .foregroundStyle(K.text3)
            ForEach(shown) { j in
                Button { if let s = j.sub, !s.isEmpty { store.openAgent(s) } } label: {
                HStack(spacing: 8) {
                    if j.status == "running" { ProgressView().controlSize(.mini) } else {
                        Dot(color: j.status == "done" ? K.green : j.status == "failed" ? K.red : K.dash, size: 7)
                    }
                    Text(j.kind == "agent" ? "子 Agent" : "命令")
                        .font(KFont.sans(11, .medium))
                        .foregroundStyle(K.text3)
                    Text(j.title)
                        .font(j.kind == "agent" ? KFont.sans(12) : KFont.mono(12))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 8)
                    Text(Self.statusText(j.status))
                        .font(KFont.sans(11))
                        .foregroundStyle(K.text4)
                    if let s = j.sub, !s.isEmpty {
                        Text("看过程 →").font(KFont.sans(11)).foregroundStyle(K.text3)
                    }
                }
                .contentShape(Rectangle())
                }
                .buttonStyle(PressableStyle())
                .disabled((j.sub ?? "").isEmpty)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).strokeBorder(K.line))
    }

    static func statusText(_ s: String) -> String {
        ["running": "运行中", "done": "完成", "failed": "失败", "killed": "已结束"][s] ?? s
    }
}

// MARK: - 问你（ask_user）：问题 + 带 ⌘数字 的选项；回答在底部输入框里打

struct QuestionCard: View {
    let wait: WaitItem
    var pick: Int? = nil
    var busy = false
    /// 刚自动弹出、键盘还锁着：顶上一条进度走完才能输入
    var locked = false
    var more: Int = 0
    let onOption: (Int) -> Void
    let onSkip: () -> Void
    var onLater: () -> Void = {}
    var onMore: () -> Void = {}
    @State private var fill: CGFloat = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(wait.title)
                        .font(KFont.sans(15, .bold))
                        .lineSpacing(3)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(wait.sub)
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text3)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if busy { ProgressView().controlSize(.small) }
            }

            if !wait.questionOptions.isEmpty {
                VStack(spacing: 4) {
                    ForEach(Array(wait.questionOptions.enumerated()), id: \.offset) { i, o in
                        Button { onOption(i + 1) } label: {
                            HStack(spacing: 10) {
                                Kbd("⌘\(i + 1)", active: pick == i)
                                Text(o)
                                    .font(KFont.sans(13))
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(RoundedRectangle(cornerRadius: 6).fill(pick == i ? Color.white : Color.white.opacity(0.45)))
                            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(pick == i ? K.ink : .clear))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(PressableStyle())
                    }
                }
            }

            HStack(spacing: 8) {
                if more > 0 {
                    Button(action: onMore) {
                        Text("还有 \(more) 件 →")
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text2)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableStyle())
                }
                Text(wait.questionOptions.isEmpty ? "在下面打字回答 · ↵ 发送" : "↵ 发送 · ↑↓ 选 · ⌘1–\(wait.questionOptions.count) 直接选")
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text3)
                Spacer()
                OutlineButton(title: "稍后", kbd: "esc", action: onLater)
                OutlineButton(title: Choice.label("skip"), kbd: "⌘↵", action: onSkip)
            }
            .disabled(busy)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(RoundedRectangle(cornerRadius: 10).fill(K.amberBg))
        .overlay(alignment: .top) {
            if locked {
                GeometryReader { g in
                    Capsule().fill(K.amber.opacity(0.5)).frame(width: g.size.width * fill, height: 2)
                }
                .frame(height: 2)
                .padding(.horizontal, 10)
                .onAppear {
                    fill = 0
                    withAnimation(.linear(duration: AppStore.questionLock)) { fill = 1 }
                }
            }
        }
    }
}

// MARK: - 等你（审批 / 卡住了 / 外部输入 / 信任项目）

struct GateCard: View {
    let wait: WaitItem
    var more: Int = 0
    var busy = false
    let onChoose: (String) -> Void
    var onMore: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(wait.title)
                        .font(KFont.sans(14, .bold))
                        .lineLimit(2)
                    if !wait.body.isEmpty {
                        Text(wait.body)
                            .font(KFont.sans(13))
                            .lineSpacing(3)
                    }
                    Text(wait.sub)
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text3)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if busy { ProgressView().controlSize(.small) }
            }

            if let diff = wait.diff, !diff.isEmpty {
                DiffCard(title: wait.diffTitle, diff: diff, maxHeight: 200)
            } else if let cmd = wait.callPreview, !wait.title.contains(cmd) {
                Text(cmd)
                    .font(KFont.mono(12))
                    .lineLimit(4)
                    .textSelection(.enabled)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.white.opacity(0.7)))
            }

            HStack(spacing: 8) {
                if more > 0 {
                    Button(action: onMore) {
                        Text("还有 \(more) 件 →")
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text2)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableStyle())
                }
                Spacer()
                ForEach(orderedChoices, id: \.self) { c in
                    if c == wait.primaryChoice {
                        InkButton(title: "\(wait.label(c)) ⌘↵") { onChoose(c) }
                    } else if c == wait.secondaryChoice {
                        OutlineButton(title: "\(wait.label(c)) ⌫") { onChoose(c) }
                    } else {
                        OutlineButton(title: wait.label(c)) { onChoose(c) }
                    }
                }
            }
            .disabled(busy)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(RoundedRectangle(cornerRadius: 10).fill(wait.kind == "stuck" ? K.redBg : wait.kind == "trust" ? K.line3 : K.amberBg))
    }

    /// 次要的在左，主按钮放最右
    private var orderedChoices: [String] {
        let rank: [String: Int] = ["always": 0, "edit": 1, "hint": 1, "deny": 2, "stop": 2, "allow": 3, "continue": 3]
        return wait.choices.sorted { (rank[$0] ?? 1) < (rank[$1] ?? 1) }
    }
}

private struct ErrorCard: View {
    let note: String
    let onReply: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                (Text("出错了").fontWeight(.bold) + Text("：" + note))
                    .font(KFont.sans(14))
                    .lineSpacing(4)
                Text("可以在下面接着说，让它换个办法。")
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text3)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            OutlineButton(title: "接着说 tab", action: onReply)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(RoundedRectangle(cornerRadius: 10).fill(K.redBg))
    }
}

// MARK: - 过程

private struct ProcessPanel: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        let steps = store.taskSteps
        let cs = store.taskCurrentStep
        let win = store.taskStepWindow
        let created = store.taskCreated

        HStack(spacing: 0) {
            // 窗口式：一屏 5 步，⌘数字 = 眼前第几步，⌘↑↓ / 滚轮挪，编号跟着重排
            VStack(alignment: .leading, spacing: 2) {
                if steps.isEmpty {
                    Text("还没有步骤")
                        .font(KFont.sans(13))
                        .foregroundStyle(K.text4)
                        .padding(16)
                }
                if steps.count > ListWindow.size {
                    Text("第 \(win.lowerBound + 1)–\(win.upperBound) 步 / 共 \(steps.count) 步 · ↑↓ 选步骤")
                        .font(KFont.sans(11))
                        .foregroundStyle(K.text4)
                        .padding(.horizontal, 8)
                        .padding(.top, 2)
                        .padding(.bottom, 4)
                }
                ForEach(win, id: \.self) { i in
                    let p = steps[i]
                    Button { store.task.step = i } label: {
                        HStack(spacing: 10) {
                            StepDot(step: p)
                            Text(p.title)
                                .font(KFont.sans(p.status == "note" ? 12 : 13))
                                .foregroundStyle(p.kind == .step && p.status != "note" ? K.ink : K.text2)
                                .strikethrough(p.status == "denied", color: K.text4)
                                .lineLimit(1)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Text(TimeText.offset(p.ts, from: created))
                                .font(KFont.mono(11))
                                .foregroundStyle(K.text4)
                            Kbd("⌘\(i - win.lowerBound + 1)", active: i == cs)
                        }
                        .padding(8)
                    }
                    .buttonStyle(RowStyle(selected: i == cs, selectedFill: K.line3, radius: 6))
                }
            }
            .padding(8)
            .frame(width: 320)
            .frame(maxHeight: .infinity, alignment: .top)
            .contentShape(Rectangle())
            .onHover { store.task.stepsHovered = $0 }

            VLine()

            Group {
                if steps.indices.contains(cs) {
                    StepDetail(step: steps[cs])
                } else {
                    Color.clear
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(height: 230)
        .background(RoundedRectangle(cornerRadius: 10).fill(.white))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(K.line))
    }
}

struct StepDot: View {
    let step: Step
    var body: some View {
        switch step.kind {
        case .you: Dot(color: K.ink, size: 7, square: true)
        case .agent: Dot(color: .white, size: 7, square: true, hollow: true)
        case .step:
            if step.status == "running" {
                ProgressView().controlSize(.mini).frame(width: 7, height: 7).scaleEffect(0.6)
            } else {
                Dot(color: step.statusColor, size: 7)
            }
        }
    }
}

/// 一步的详情：你说 / Agent 说 / 工具调用（用了 · 参数 · 得到 · 为什么）
struct StepDetail: View {
    @Environment(AppStore.self) private var store
    let step: Step
    var large = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                if step.kind != .step {
                    SmallCaps(step.kind == .you ? "你说" : "Agent 说")
                    if large {
                        MarkdownView(text: step.text, size: 15)
                    } else {
                        Bubble(text: step.text, kind: step.bubbleKind)
                    }
                } else {
                    HStack(spacing: 8) {
                        Text(step.title).font(KFont.sans(large ? 22 : 13, .bold))
                        if let s = step.statusLabel {
                            Text(s)
                                .font(KFont.sans(11, .medium))
                                .foregroundStyle(step.status == "error" ? K.red : K.ink)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(step.status == "waiting" ? K.amberBg : step.status == "error" ? K.redBg : K.line3))
                        }
                    }
                    if !step.tool.isEmpty {
                        SmallCaps("用了")
                        Text(step.tool)
                            .font(KFont.mono(12))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 6)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 6).fill(K.line3))
                    }
                    if let sub = step.sub, !sub.isEmpty {
                        Button { store.openAgent(sub) } label: {
                            HStack(spacing: 8) {
                                Image(systemName: "person.2")
                                Text("看它的过程")
                                Spacer()
                                Kbd("↵")
                            }
                            .font(KFont.sans(13, .medium))
                            .foregroundStyle(K.ink)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 8)
                            .background(RoundedRectangle(cornerRadius: 6).fill(K.line3))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(PressableStyle())
                    }
                    if let diff = step.diff, !diff.isEmpty {
                        SmallCaps("改动")
                        DiffView(diff, maxLines: large ? nil : 60, size: large ? 13 : 11.5)
                            .background(RoundedRectangle(cornerRadius: 6).strokeBorder(K.line2))
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    } else if !step.text.isEmpty {
                        SmallCaps(step.tool.isEmpty ? "内容" : "参数")
                        Text(step.prettyArgs)
                            .font(KFont.mono(12))
                            .foregroundStyle(K.text2)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if !step.out.isEmpty {
                        SmallCaps("得到")
                        Text(step.out)
                            .font(step.tool.isEmpty ? KFont.sans(13) : KFont.mono(12))
                            .foregroundStyle(K.text2)
                            .textSelection(.enabled)
                    }
                    if let imgs = step.images, !imgs.isEmpty {
                        HStack(alignment: .top, spacing: 8) {
                            ForEach(imgs, id: \.self) { BlobThumb(image: $0) }
                        }
                    }
                    if step.isMemoryNote {
                        Button { store.openSettings(tab: .memory) } label: {
                            HStack(spacing: 8) {
                                Kbd("⌘M")
                                Text("去记忆页看、改、删")
                            }
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text2)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(PressableStyle())
                    }
                    if !step.why.isEmpty {
                        Text("为什么这么做：\(step.why)")
                            .foregroundStyle(K.text2)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 6).fill(K.paper))
                    }
                }
            }
            .font(KFont.sans(large ? 15 : 13))
            .lineSpacing(3)
            .padding(.horizontal, large ? 0 : 16)
            .padding(.vertical, large ? 0 : 14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct SmallCaps: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(KFont.mono(11))
            .tracking(0.9)
            .foregroundStyle(K.text3)
    }
}

extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}

// MARK: - 按键滚动

/// 放在 ScrollView 内容的背景里，找到外面的 NSScrollView，把「滚一段」注册给 store（↑↓ 用）
private struct ScrollNudger: NSViewRepresentable {
    let register: (@escaping (CGFloat) -> Void) -> Void

    func makeNSView(context: Context) -> NSView {
        let v = NSView()
        register { [weak v] dy in
            guard let sv = v?.enclosingScrollView, let doc = sv.documentView else { return }
            let clip = sv.contentView
            let maxY = max(0, doc.frame.height - clip.bounds.height)
            var o = clip.bounds.origin
            o.y = min(max(0, o.y + (doc.isFlipped ? dy : -dy)), maxY)
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.12
                clip.animator().setBoundsOrigin(o)
            }
            sv.reflectScrolledClipView(clip)
        }
        return v
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}
