import SwiftUI

/// ⑤ 任务详情：过程面板的放大版。左边一步一行，最下面一行「现在」；右边看选中的那个。
/// ↑↓ 选，tab 和 Agent 说，esc 返回。
struct DetailView: View {
    @Environment(AppStore.self) private var store
    @FocusState private var focused: InputField?

    var body: some View {
        @Bindable var store = store
        let nodes = store.detailNodes
        let live = store.detailLive
        let at = store.detailAt
        let inInput = store.focusedField == .detail
        let kind = store.taskKind

        VStack(spacing: 0) {
            HStack(spacing: 12) {
                BackButton(label: "回到任务", showEsc: !inInput) { store.detailBack() }
                Text(store.task.summary?.title ?? "")
                    .font(KFont.sans(17, .bold))
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                HStack(spacing: 8) {
                    Dot(color: kind.color, size: 8)
                    Text(kind.label).font(KFont.sans(13)).foregroundStyle(K.text2)
                }
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 14)

            HStack(spacing: 0) {
                stepList(nodes, live: live, at: at)
                    .frame(width: 360)
                VLine()
                ScrollView {
                    Group {
                        if live { now } else if nodes.indices.contains(at) { StepDetail(step: nodes[at], large: true) }
                    }
                    .padding(.horizontal, 40)
                    .padding(.vertical, 28)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
            }
            .frame(maxHeight: .infinity)
            .background(RoundedRectangle(cornerRadius: 12).fill(.white))
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(K.line))
            .padding(.horizontal, 24)

            // 命令栏
            HStack(spacing: 12) {
                PromptGlyph(size: 26)
                TextField("", text: $store.detail.draft,
                          prompt: Text(store.answerMode ? "回答它的问题" : "接着说").foregroundColor(K.text4))
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
            .padding(.top, 12)
            .padding(.bottom, 16)

            FooterBar {
                HStack(spacing: 14) {
                    ForEach(hints) { h in HStack(spacing: 5) { Kbd(h.k); Text(h.t) } }
                }
            } trailing: { EmptyView() }
        }
        .frame(width: 1100, height: 820)
        .syncFocus(store, $focused)
    }

    private var hints: [KeyHint] {
        if store.answerMode { return [KeyHint("↵", "发给它"), KeyHint("⌘↵", "你自己定"), KeyHint("esc", "稍后")] }
        if store.focusedField == .detail { return [KeyHint("↵", "发送"), KeyHint("esc", "离开输入框")] }
        return (store.taskGate?.primaryChoice.map { [KeyHint("⌘↵", Choice.label($0))] } ?? [])
            + [KeyHint("↑↓", "选"), KeyHint("⌘C", "复制"), KeyHint("tab", "接着说")]
    }

    /// 左边：一步一行，最下面「现在」
    private func stepList(_ nodes: [Step], live: Bool, at: Int) -> some View {
        let created = store.taskCreated
        return ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(nodes.enumerated()), id: \.offset) { i, p in
                        Button { store.detailJump(i) } label: {
                            HStack(spacing: 10) {
                                StepDot(step: p)
                                Text(p.listTitle)
                                    .font(KFont.sans(p.status == "note" ? 12 : 13))
                                    .foregroundStyle(p.kind == .step && p.status != "note" ? K.ink : K.text2)
                                    .strikethrough(p.status == "denied", color: K.text4)
                                    .lineLimit(1)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                Text(TimeText.offset(p.ts, from: created))
                                    .font(KFont.mono(11))
                                    .foregroundStyle(K.text4)
                            }
                            .padding(8)
                        }
                        .buttonStyle(RowStyle(selected: !live && i == at, selectedFill: K.line3, radius: 6))
                        .id(i)
                    }
                    Button { store.detailJump(nodes.count) } label: {
                        HStack(spacing: 10) {
                            Dot(color: store.taskKind.color, size: 7)
                            Text("现在").font(KFont.sans(13, .medium))
                            Spacer()
                        }
                        .padding(8)
                    }
                    .buttonStyle(RowStyle(selected: live, selectedFill: K.line3, radius: 6))
                    .id(-1)
                }
                .padding(8)
            }
            .onChange(of: store.detail.at) { _, v in proxy.scrollTo(v < 0 ? -1 : v) }
            .onAppear { proxy.scrollTo(store.detail.at < 0 ? -1 : store.detail.at, anchor: .bottom) }
        }
    }

    /// 右边的「现在」：这一轮还在跑就看它正在做的事（放行卡也在这里），跑完了看结论
    @ViewBuilder private var now: some View {
        let kind = store.taskKind
        if kind == .run || kind == .wait {
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
        } else {
            let final = store.task.detail?.final ?? ""
            VStack(alignment: .leading, spacing: 18) {
                if final.isEmpty {
                    Text(store.task.summary.map { $0.kind == .error ? ($0.note.isEmpty ? $0.now : $0.note) : $0.now } ?? "")
                        .font(KFont.sans(15))
                        .foregroundStyle(K.text3)
                } else {
                    MarkdownView(text: final, size: 15, spacing: 12, baseDir: store.task.summary?.workdir)
                }
                if !store.taskChanges.isEmpty {
                    ChangesList(changes: store.taskChanges, inCard: false)
                }
            }
        }
    }
}
