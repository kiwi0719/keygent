import SwiftUI

/// ④ 等你的事：总览 + 逐件看（10 件也能键盘过完）
struct QueueView: View {
    @Environment(AppStore.self) private var store
    @FocusState private var focused: InputField?

    var body: some View {
        let Q = store.queue

        VStack(spacing: 0) {
            HStack(spacing: 12) {
                BackButton(label: Q.expanded ? "回到总览" : "关闭", showEsc: store.focusedField == nil) {
                    if Q.expanded { store.queue.expanded = false } else { store.hidePanel() }
                }
                Text(Q.expanded ? "逐件看" : "等你的事")
                    .font(KFont.sans(17, .black))
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(Q.expanded ? "第 \(Q.cur + 1) / \(Q.list.count) 件 · 还剩 \(store.queueLeft) 件待定" : "按时间从早到晚")
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text3)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 14)

            HLine()

            Group {
                if store.connection == .offline {
                    OfflineNotice()
                } else if Q.expanded {
                    ReviewPane(focused: $focused)
                } else {
                    OverviewPane()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)

            KeyHintBar(mode: mode, keys: keys)
        }
        .frame(width: 860, height: 700)
        .syncFocus(store, $focused)
    }

    private var mode: String {
        if !store.queue.expanded { return "总览" }
        return store.focusedField == .queueEdit ? "输入中" : "逐件看"
    }

    private var keys: [KeyHint] {
        if !store.queue.expanded {
            var k = [KeyHint("空格", "逐件看")]
            if store.queueBulkCount > 0 {
                k.append(KeyHint("⌘⇧↵", store.queueBulkCount == store.waits.count ? "全部放行" : "放行其中 \(store.queueBulkCount) 件"))
            }
            let r = store.queueRunning.count
            if r > 0 { k.append(KeyHint(r == 1 ? "⌘1" : "⌘1–\(r)", "打开在跑的任务")) }
            return k
        }
        if store.focusedField == .queueEdit {
            return [KeyHint("↵", "发给它"), KeyHint("esc", "离开输入框")]
        }
        var k = [KeyHint("↑↓", "换一件"), KeyHint("⌘1–5", "跳到眼前第几件")]
        if let w = store.queueCurrent, w.isQuestion {
            k.append(KeyHint("↵", "去回答"))
        } else if let w = store.queueCurrent {
            if let p = w.primaryChoice { k.append(KeyHint("⌘↵", w.label(p))) }
            if let s = w.secondaryChoice { k.append(KeyHint("⌫", w.label(s))) }
            if w.noteChoice != nil { k.append(KeyHint("tab", w.kind == "stuck" ? "给个提示" : "说明原因")) }
        }
        k.append(KeyHint("⌘⇧↵", "放行剩下"))
        return k
    }
}

// MARK: - 总览

private struct OverviewPane: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        VStack(spacing: 10) {
            if !store.waits.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("\(store.waits.count) 件事等你")
                            .font(KFont.sans(15, .black))
                        Spacer()
                        Text("来自 \(store.queueTaskCount) 个任务")
                            .font(KFont.mono(11))
                    }
                    VStack(spacing: 4) {
                        ForEach(store.queueGroups, id: \.task) { g in
                            HStack {
                                Text(g.task)
                                Spacer()
                                Text("\(g.n) 件").font(KFont.mono(13)).foregroundStyle(K.text3)
                            }
                        }
                    }
                    .font(KFont.sans(13))
                    .foregroundStyle(K.text2)
                    HStack(spacing: 8) {
                        InkButton(title: "逐件看", kbd: "空格", height: 38, bold: true) { store.queueExpand() }
                        if store.queueBulkCount > 0 {
                            OutlineButton(title: store.queueBulkCount == store.waits.count ? "全部放行" : "放行其中 \(store.queueBulkCount) 件",
                                          kbd: "⌘⇧↵", height: 38, stroke: K.ink) { store.queueApproveRest() }
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
                .background(RoundedRectangle(cornerRadius: 12).fill(K.amberBg2))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(K.ink, lineWidth: 2))
            } else {
                HStack {
                    Text(store.queue.handledCount > 0 ? "处理完了 · 这次处理了 \(store.queue.handledCount) 件" : "没有等你的事")
                        .font(KFont.sans(15, .bold))
                    Spacer()
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
                .background(RoundedRectangle(cornerRadius: 12).fill(K.greenBg))
            }

            SectionLabel("在跑 · 只显示摘要")
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8)
                .padding(.top, 4)

            if store.queueRunning.isEmpty {
                Text("没有在跑的任务")
                    .font(KFont.sans(13))
                    .foregroundStyle(K.text4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 10)
            }

            ForEach(Array(store.queueRunning.enumerated()), id: \.element.id) { i, r in
                Button { store.openTask(id: r.id, summary: r) } label: {
                    HStack(spacing: 14) {
                        Dot(color: r.kind.color, size: 9)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(r.title).font(KFont.sans(14, .bold)).lineLimit(1)
                            Text(r.line).font(KFont.sans(12)).foregroundStyle(K.text3).lineLimit(1)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        Text(r.kind.label).font(KFont.sans(12)).foregroundStyle(K.text3)
                        Kbd("⌘\(i + 1)")
                    }
                    .foregroundStyle(K.ink)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                }
                .buttonStyle(RowStyle(selected: false))
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
    }
}

// MARK: - 逐件看

private struct ReviewPane: View {
    @Environment(AppStore.self) private var store
    var focused: FocusState<InputField?>.Binding

    var body: some View {
        @Bindable var store = store
        let Q = store.queue
        let N = Q.list.count
        let V = QueueState.viewCount

        HStack(spacing: 0) {
            // 左：列表（窗口式，编号跟着滚动走）
            VStack(alignment: .leading, spacing: 2) {
                Text(Q.top > 0 ? "↑ 上面还有 \(Q.top) 件" : "编号跟着滚动走")
                    .font(KFont.sans(11))
                    .foregroundStyle(K.text4)
                    .padding(.horizontal, 8)
                    .padding(.top, 2)
                    .padding(.bottom, 6)
                ForEach(Q.top..<min(N, Q.top + V), id: \.self) { i in
                    let w = Q.list[i]
                    let d = Q.decided[w.id]
                    Button { store.queueGo(i) } label: {
                        HStack(spacing: 10) {
                            Dot(color: d == nil ? (w.kind == "stuck" ? K.red : w.kind == "trust" ? K.dash : K.amber) : K.green, size: 7)
                            VStack(alignment: .leading, spacing: 1) {
                                Text("\(w.taskTitle) · \(w.title)")
                                    .font(KFont.sans(13, .bold))
                                    .lineLimit(1)
                                    .truncationMode(.tail)
                                Text(d ?? "待定")
                                    .font(KFont.sans(11))
                                    .foregroundStyle(K.text3)
                                    .lineLimit(1)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            Kbd("⌘\(i - Q.top + 1)", active: i == Q.cur)
                        }
                        .foregroundStyle(K.ink)
                        .padding(8)
                    }
                    .buttonStyle(RowStyle(selected: i == Q.cur, selectedFill: .white, radius: 6))
                }
                Text(Q.top + V < N ? "↓ 下面还有 \(N - Q.top - V) 件" : "到底了")
                    .font(KFont.sans(11))
                    .foregroundStyle(K.text4)
                    .padding(.horizontal, 8)
                    .padding(.top, 6)
                Spacer(minLength: 0)
            }
            .padding(8)
            .frame(width: 300)
            .frame(maxHeight: .infinity, alignment: .top)

            VLine(color: K.line)

            // 右：当前这件
            if let w = store.queueCurrent {
                let d = Q.decided[w.id]
                VStack(alignment: .leading, spacing: 8) {
                    Button { store.openTask(id: w.task) } label: {
                        Text("来自 \(w.taskTitle) · \(TimeText.relative(w.ts)) →")
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text3)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableStyle())
                    Text(w.title)
                        .font(KFont.sans(16, .bold))
                    if !w.body.isEmpty {
                        Text(w.body).foregroundStyle(K.text2)
                    }
                    if w.isQuestion {
                        ForEach(Array(w.questionOptions.enumerated()), id: \.offset) { i, o in
                            Text("\(i + 1). \(o)").font(KFont.sans(13)).foregroundStyle(K.text2)
                        }
                        if d == nil {
                            InkButton(title: "去回答", kbd: "↵") { store.openQuestion(w) }
                                .padding(.top, 4)
                        }
                    }
                    if w.isElicit, d == nil {
                        if let fs = w.fields, !fs.isEmpty {
                            Text("要填：" + fs.map { $0.title }.joined(separator: "、")).font(KFont.sans(13)).foregroundStyle(K.text2)
                        }
                        InkButton(title: "去回答", kbd: "↵") { store.openTask(id: w.task) }
                            .padding(.top, 4)
                    }
                    if let diff = w.diff, !diff.isEmpty {
                        DiffCard(title: w.diffTitle, diff: diff, maxHeight: 220)
                            .padding(.top, 4)
                    } else if let call = w.call {
                        SmallCaps(call.name)
                            .padding(.top, 4)
                        ScrollView {
                            Text(w.callPreview ?? "")
                                .font(KFont.mono(12))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .frame(maxHeight: 220)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                        .background(RoundedRectangle(cornerRadius: 6).fill(K.line3))
                    }
                    Text(w.sub)
                        .font(KFont.sans(11))
                        .foregroundStyle(K.text4)

                    Spacer(minLength: 0)

                    if Q.working == w.id {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.mini)
                            Text("正在发给 Weaver……")
                        }
                        .font(KFont.sans(12))
                        .foregroundStyle(K.green)
                    }

                    if d == nil && Q.working != w.id {
                        if w.noteChoice != nil {
                            HStack(spacing: 10) {
                                Kbd("tab")
                                TextField("", text: $store.queue.editDraft,
                                          prompt: Text(w.kind == "stuck" ? "给它一个提示，↵ 发送" : "不同意的话告诉它为什么，↵ 发送")
                                            .foregroundColor(K.text4))
                                    .textFieldStyle(.plain)
                                    .font(KFont.sans(13))
                                    .focused(focused, equals: .queueEdit)
                                    .frame(height: 30)
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 4)
                            .background(RoundedRectangle(cornerRadius: 8).fill(.white))
                            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(store.focusedField == .queueEdit ? K.ink : K.border))
                        }

                        HStack(spacing: 8) {
                            if let p = w.primaryChoice {
                                InkButton(title: w.label(p), kbd: "⌘↵") { store.queueChoose(p) }
                            }
                            if let s = w.secondaryChoice {
                                OutlineButton(title: w.label(s), kbd: "⌫") { store.queueChoose(s) }
                            }
                            ForEach(w.choices.filter { ["always", "edit"].contains($0) }, id: \.self) { c in
                                OutlineButton(title: w.label(c)) { store.queueChoose(c) }
                            }
                            Spacer()
                            if store.queueLeft > 1, Q.list.contains(where: { Q.decided[$0.id] == nil && $0.isBulkApprovable }) {
                                Button { store.queueApproveRest() } label: {
                                    HStack(spacing: 8) {
                                        Text("放行剩下的")
                                        Kbd("⌘⇧↵")
                                    }
                                    .font(KFont.sans(12))
                                    .foregroundStyle(K.text2)
                                    .padding(.horizontal, 12)
                                    .frame(height: 34)
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(PressableStyle())
                            }
                        }
                    }

                    if let d {
                        Text("\(d) · ↓ 下一件")
                            .font(KFont.sans(12))
                            .foregroundStyle(K.green)
                    }
                }
                .font(KFont.sans(13))
                .lineSpacing(4)
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            } else {
                Text("没有等你的事")
                    .foregroundStyle(K.text4)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}
