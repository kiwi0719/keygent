import SwiftUI

/// ⑤ 任务详情：结果 + 时间线 + 一条命令栏。
/// 在「现在」看结论；沿时间线往回走看当时的那一步（你说的、Agent 说的、工具调用）。
struct DetailView: View {
    @Environment(AppStore.self) private var store
    @FocusState private var focused: InputField?

    var body: some View {
        @Bindable var store = store
        let nodes = store.detailNodes
        let N = nodes.count
        let at = store.detailAt
        let live = store.detailLive
        let inInput = store.focusedField == .detail
        let frac = N > 1 ? CGFloat(at) / CGFloat(N - 1) : 1
        let created = store.taskCreated

        VStack(spacing: 0) {
            // 顶栏：只留标题。返回（esc）、复制（⌘C）都在底部快捷键栏里
            Text(store.task.summary?.title ?? "")
                .font(KFont.sans(17, .bold))
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 28)
                .padding(.top, 18)
                .padding(.bottom, 14)

            // 正文
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 10) {
                    Text(live ? "现在" : "第 \(at + 1) / \(N) 步")
                        .font(KFont.mono(11))
                        .foregroundStyle(live ? K.green : K.ink)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 2)
                        .background(RoundedRectangle(cornerRadius: 4).fill(live ? K.greenBg : K.amberBg))
                    if !live, nodes.indices.contains(at) {
                        Text("\(TimeText.offset(nodes[at].ts, from: created)) · 回看中 · 这是当时的这一步")
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text4)
                    } else if !(store.taskKind == .run || store.taskKind == .wait), let u = store.task.detail?.usage {
                        Text("\(u.steps) / \(u.maxSteps) 步 · \(u.tokens) tokens")
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text4)
                    }
                }

                if live, store.taskKind == .run || store.taskKind == .wait {
                    // 这一轮还没跑完：和 ③ 一样看它正在做的事；停下等你时，放行卡就在这里，不用退回去
                    ScrollView {
                        VStack(spacing: 14) {
                            LiveRound(limit: 12, onPick: { store.detailJump($0) }, onMore: { store.detailMove(-1) })
                            if let w = store.taskQuestion {
                                QuestionCard(wait: w, pick: store.task.optionPick, busy: store.task.answering == w.id,
                                             locked: store.questionLockUntil != nil,
                                         more: store.taskWaits.count - (store.taskGate == nil ? 1 : 2),
                                             onOption: { store.questionOption(w, $0) },
                                             onSkip: { store.questionSkip(w) },
                                             onLater: { store.questionLater(w) },
                                             onMore: { store.openQueue() })
                            }
                            if let w = store.taskGate {
                                GateCard(wait: w, more: store.taskWaits.count - (store.taskQuestion == nil ? 1 : 2),
                                         busy: store.task.answering == w.id,
                                         onChoose: { store.taskChoose(w, $0) },
                                         onMore: { store.openQueue() })
                            }
                        }
                    }
                } else if live {
                    let final = store.task.detail?.final ?? ""
                    ScrollView {
                        if final.isEmpty {
                            VStack(alignment: .leading, spacing: 10) {
                                Text("还没有结论")
                                    .font(KFont.sans(30, .black))
                                Text(store.task.summary.map { $0.kind == .error ? ($0.note.isEmpty ? $0.now : $0.note) : $0.now } ?? "")
                                    .font(KFont.sans(15))
                                    .foregroundStyle(K.text3)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        } else {
                            MarkdownView(text: final, size: 15, spacing: 12, baseDir: store.task.summary?.workdir)
                        }
                    }
                } else if nodes.indices.contains(at) {
                    StepDetail(step: nodes[at], large: true)
                }
            }
            .padding(.horizontal, 64)
            .padding(.vertical, 32)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 12).fill(.white))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(K.line))
            .padding(.horizontal, 24)

            // 时间线
            VStack(spacing: 10) {
                GeometryReader { geo in
                    HStack(spacing: 0) {
                        if nodes.indices.contains(at) {
                            caption(nodes[at])
                                .padding(.leading, geo.size.width * frac * 0.3)
                        }
                        Spacer(minLength: 0)
                    }
                    .frame(maxHeight: .infinity, alignment: .bottom)
                }
                .frame(height: 44)

                if N > 0 {
                    Timeline(nodes: nodes, at: at, frac: frac) { store.detailJump($0) }
                        .frame(height: 26)
                } else {
                    HLine().frame(height: 26)
                }

                HStack {
                    Text("0:00 开始")
                    Spacer()
                    Text("■ 你说 · □ Agent 说 · ● 步骤 · 琥珀 = 等你 · 红 = 出错")
                    Spacer()
                    if live {
                        Text("现在")
                    } else {
                        Button { store.detail.at = -1 } label: {
                            HStack(spacing: 6) {
                                Text("回到现在")
                                if !inInput { Kbd("esc") }
                            }
                            .foregroundStyle(K.ink)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(PressableStyle())
                    }
                }
                .font(KFont.mono(11))
                .foregroundStyle(K.text4)
            }
            .padding(.horizontal, 40)
            .padding(.top, 18)
            .padding(.bottom, 6)

            // 命令栏
            HStack(spacing: 12) {
                PromptGlyph(size: 26)
                TextField("", text: $store.detail.draft,
                          prompt: Text(store.answerMode ? "回答它的问题，↵ 发送" :
                                        live ? "和 Agent 说，接着这个任务做" : "回看中 · 说的话会接在最新一步之后")
                            .foregroundColor(K.text4))
                    .textFieldStyle(.plain)
                    .font(KFont.sans(15))
                    .focused($focused, equals: .detail)
                    .frame(height: 40)
                Kbd(inInput ? "↵" : "tab")
            }
            .padding(.horizontal, 14)
            .frame(height: 52)
            .background(RoundedRectangle(cornerRadius: 12).fill(.white))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(inInput ? K.ink : K.line))
            .padding(.horizontal, 24)
            .padding(.top, 8)
            .padding(.bottom, 16)

            KeyHintBar(
                mode: inInput ? "输入中" : (live ? "现在" : "回看中"),
                keys: store.answerMode
                    ? [KeyHint("↵", "发给它"), KeyHint("⌘↵", "你自己定"), KeyHint("esc", "稍后")]
                    : inInput
                    ? [KeyHint("↵", "发送"), KeyHint("esc", "离开输入框")]
                    : (store.taskGate?.primaryChoice.map { [KeyHint("⌘↵", Choice.label($0))] } ?? [])
                      + [KeyHint("←→", "沿时间线走"), KeyHint("tab", "和 Agent 说"), KeyHint("⌘C", live ? "复制结论" : "复制这一步"),
                         KeyHint("esc", live ? "返回" : "回到现在")],
                horizontal: 24
            )
        }
        .frame(width: 1100, height: 820)
        .syncFocus(store, $focused)
    }

    @ViewBuilder
    private func caption(_ n: Step) -> some View {
        HStack(spacing: 8) {
            Text(n.kind == .you ? "你说" : n.kind == .agent ? "Agent 说" : "步骤")
                .font(KFont.sans(11))
                .foregroundStyle(n.kind == .you ? K.canvas : K.text4)
            Text(n.kind == .step ? n.title : n.text)
                .font(KFont.sans(14))
                .foregroundStyle(n.kind == .you ? K.paper : K.ink)
        }
        .lineLimit(1)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(n.kind == .you ? K.ink : n.kind == .agent ? Color.white : K.bar)
        )
        .overlay {
            if n.kind == .agent { RoundedRectangle(cornerRadius: 10).strokeBorder(K.ink) }
        }
        .frame(maxWidth: 700, alignment: .leading)
    }
}

// MARK: - 时间线

private struct Timeline: View {
    let nodes: [Step]
    let at: Int
    let frac: CGFloat
    let onTap: (Int) -> Void

    var body: some View {
        GeometryReader { geo in
            // 步骤很多时把点缩小，免得挤在一起
            let room = geo.size.width / CGFloat(max(1, nodes.count))
            let base = min(9, max(3, room * 0.55))
            ZStack(alignment: .leading) {
                Rectangle().fill(K.line).frame(height: 2)
                Rectangle().fill(K.ink).frame(width: geo.size.width * frac, height: 2)
                HStack(spacing: 0) {
                    ForEach(Array(nodes.enumerated()), id: \.offset) { i, n in
                        if i > 0 { Spacer(minLength: 0) }
                        node(n, i: i, base: base)
                    }
                }
            }
            .frame(maxHeight: .infinity)
        }
    }

    @ViewBuilder
    private func node(_ n: Step, i: Int, base: CGFloat) -> some View {
        let on = i == at
        let big = n.status == "waiting" || n.status == "error"
        let sz: CGFloat = on ? max(12, base + 7) : big ? base + 3 : base
        let radius: CGFloat = n.kind == .step ? sz / 2 : min(3, sz / 3)
        let fill: Color = {
            switch n.kind {
            case .you: return K.ink
            case .agent: return .white
            case .step:
                if n.status == "waiting" { return K.amber }
                if n.status == "error" { return K.red }
                return i <= at ? K.ink : K.dash
            }
        }()

        Button { onTap(i) } label: {
            RoundedRectangle(cornerRadius: radius)
                .fill(fill)
                .overlay {
                    if n.kind == .agent { RoundedRectangle(cornerRadius: radius).strokeBorder(K.ink, lineWidth: 1.5) }
                }
                .frame(width: sz, height: sz)
                .background {
                    if on {
                        ZStack {
                            RoundedRectangle(cornerRadius: radius + 6).fill(K.ink).padding(-6)
                            RoundedRectangle(cornerRadius: radius + 4).fill(K.paper).padding(-4)
                        }
                    }
                }
                .frame(width: max(sz, 10), height: 22)
                .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .help(n.title)
        .accessibilityLabel(n.title)
        .frame(width: sz, height: 22)
    }
}
