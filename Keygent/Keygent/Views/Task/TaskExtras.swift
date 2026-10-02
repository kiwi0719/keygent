import SwiftUI

// design/keygent-gaps.md：任务页上的清单卡、改过的文件、diff 弹层、子 Agent 的过程。
// 都照宣传视频里的样子：白卡、表头一行小字、一行 = 记号 + 动作 + 对象，右边只在要说话时才有字。

// MARK: - 清单（todo_write）

struct TodoCard: View {
    let todos: [TodoItem]
    /// 这一轮结束了、清单也全做完了：折成一行
    var settled = false
    @State private var open = false

    var body: some View {
        let done = todos.filter { $0.status == "completed" || $0.status == "cancelled" }.count
        let folded = settled && done == todos.count && !open
        VStack(spacing: 0) {
            Button { if settled { open.toggle() } } label: {
                HStack {
                    Text("清单")
                    Spacer()
                    Text(folded ? "\(done)/\(todos.count) · 全部完成" : "\(done)/\(todos.count)")
                        .font(KFont.mono(11))
                    if settled { Image(systemName: open ? "chevron.up" : "chevron.down").font(.system(size: 9)) }
                }
                .font(KFont.sans(12))
                .foregroundStyle(K.text3)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .contentShape(Rectangle())
            }
            .buttonStyle(PressableStyle())
            .overlay(alignment: .bottom) { if !folded { HLine(color: K.line2) } }

            if !folded {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(todos.enumerated()), id: \.offset) { _, t in
                        TodoRow(item: t)
                    }
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background(RoundedRectangle(cornerRadius: 10).fill(.white))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(K.line))
    }
}

/// 清单的一行：绿勾 = 做完，转圈 = 正在做，空圈 = 没做，删除线 = 不做了
struct TodoRow: View {
    let item: TodoItem
    var size: CGFloat = 13

    var body: some View {
        HStack(spacing: 10) {
            TodoMark(status: item.status)
                .frame(width: 16)
            Text(item.content)
                .font(KFont.sans(size, item.status == "in_progress" ? .medium : .regular))
                .foregroundStyle(item.status == "in_progress" ? K.ink : (item.status == "pending" ? K.text2 : K.text3))
                .strikethrough(item.status == "cancelled", color: K.text4)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
    }
}

struct TodoMark: View {
    let status: String
    var body: some View {
        switch status {
        case "completed":
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 12))
                .foregroundStyle(K.greenSoft)
        case "in_progress":
            ProgressView().controlSize(.mini).scaleEffect(0.8)
        case "cancelled":
            Image(systemName: "minus.circle")
                .font(.system(size: 12))
                .foregroundStyle(K.dash)
        default:
            Circle().strokeBorder(K.dash, lineWidth: 1.2).frame(width: 11, height: 11)
        }
    }
}

// MARK: - 改过的文件（结果卡底部，视频 0:19 那一行）

struct ChangesList: View {
    @Environment(AppStore.self) private var store
    let changes: [FileChange]
    /// 放在结果卡里（上面有分隔线、没有自己的外框）还是自己一张卡
    var inCard = true

    var body: some View {
        let T = store.task
        let limit = 3
        let shown = T.changesOpen ? changes : Array(changes.prefix(limit))
        VStack(spacing: 0) {
            if !inCard {
                HStack {
                    Text("改过的文件")
                    Spacer()
                    Text("\(changes.count) 个").font(KFont.mono(11))
                }
                .font(KFont.sans(12))
                .foregroundStyle(K.text3)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .overlay(alignment: .bottom) { HLine(color: K.line2) }
            }
            VStack(spacing: 0) {
                ForEach(Array(shown.enumerated()), id: \.element.id) { i, c in
                    row(c, selected: i == min(T.changePick, changes.count - 1) && changes.count > 1)
                }
                if changes.count > limit {
                    Button { store.task.changesOpen.toggle() } label: {
                        Text(T.changesOpen ? "收起" : "还有 \(changes.count - limit) 个文件")
                            .font(KFont.sans(12))
                            .foregroundStyle(K.text3)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 6)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableStyle())
                }
            }
            .padding(.vertical, 6)
        }
        .overlay(alignment: .top) { if inCard { HLine(color: K.line2) } }
        .background {
            if !inCard {
                RoundedRectangle(cornerRadius: 10).fill(.white)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay { if !inCard { RoundedRectangle(cornerRadius: 10).strokeBorder(K.line) } }
    }

    private func row(_ c: FileChange, selected: Bool) -> some View {
        let armed = store.armed == "undo:\(c.path)"
        return Button { store.openDiff(c.path) } label: {
            HStack(spacing: 10) {
                Text(c.rel)
                    .font(KFont.mono(12))
                    .foregroundStyle(c.undone ? K.text4 : K.text2)
                    .strikethrough(c.undone, color: K.text4)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if c.created && !c.undone {
                    Text("新建").font(KFont.sans(10.5, .medium)).foregroundStyle(K.text3)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Capsule().fill(K.line3))
                }
                if !c.undone { ChangeCount(added: c.added, removed: c.removed, size: 11.5) }
                Spacer(minLength: 8)
                if c.undone {
                    Text("已撤销").font(KFont.sans(12)).foregroundStyle(K.text4)
                } else if armed {
                    Text("再按一次 ⌘Z 撤销").font(KFont.sans(12, .medium)).foregroundStyle(K.amber)
                } else if c.canUndo {
                    Button { store.undoFile(c) } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "arrow.uturn.backward").font(.system(size: 10))
                            Text("撤销")
                        }
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text3)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableStyle())
                    .help("恢复到这个任务改它之前 · ⌘Z")
                } else {
                    Text(c.why).font(KFont.sans(11.5)).foregroundStyle(K.text4)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 5)
            .background(selected ? K.line3 : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .help("⌘D 看改了什么")
    }
}

// MARK: - diff 弹层（⌘D）

struct DiffSheetView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        if let d = store.diffSheet, d.files.indices.contains(d.cur) {
            let f = d.files[d.cur]
            let armed = store.armed == "undo:\(f.path)"
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(f.rel).font(KFont.mono(14, .medium)).lineLimit(1).truncationMode(.middle)
                        HStack(spacing: 8) {
                            if f.undone { Text("已撤销").foregroundStyle(K.text4) }
                            else { ChangeCount(added: f.added, removed: f.removed, size: 11) }
                            if f.created { Text("新建的文件") }
                            if d.files.count > 1 { Text("第 \(d.cur + 1) / \(d.files.count) 个") }
                        }
                        .font(KFont.sans(12))
                        .foregroundStyle(K.text3)
                    }
                    Spacer()
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 12)
                HLine(color: K.line2)

                ScrollView {
                    if let r = d.diffs[f.path] {
                        if r.diff.isEmpty {
                            Text(r.why ?? "没有可显示的改动")
                                .font(KFont.sans(13)).foregroundStyle(K.text4).padding(18)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        } else {
                            DiffView(r.diff, size: 12.5).padding(.vertical, 6)
                        }
                    } else if let e = d.error {
                        Text(e).font(KFont.sans(13)).foregroundStyle(K.red).padding(18)
                    } else {
                        VStack(spacing: 6) { SkeletonBar(height: 14); SkeletonBar(height: 14); SkeletonBar(height: 14) }
                            .padding(18)
                    }
                }
                .frame(maxHeight: min(460, store.panelMaxHeight - 160))
                .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 14) {
                    if d.files.count > 1 {
                        HStack(spacing: 5) { Kbd("↑↓"); Text("换文件") }
                    }
                    if !d.archived && f.canUndo && !f.undone {
                        HStack(spacing: 5) { Kbd("⌘Z"); Text(armed ? "再按一次撤销" : "撤销这个文件") }
                            .foregroundStyle(armed ? K.amber : K.text2)
                    }
                    Spacer()
                    HStack(spacing: 5) { Kbd("esc"); Text("关闭") }
                }
                .font(KFont.sans(12))
                .foregroundStyle(K.text2)
                .padding(.horizontal, 18)
                .padding(.vertical, 10)
                .background(K.bar)
            }
            .frame(width: 700)
            .background(Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .shadow(color: .black.opacity(0.28), radius: 24, y: 12)
        }
    }
}

// MARK: - 子 Agent 的过程

struct AgentSheetView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        if let a = store.agentView {
            let steps = store.agentSteps
            let cs = store.agentCurrentStep
            let win = store.agentStepWindow
            let created = steps.first?.ts ?? 0
            let kind = TaskKind(status: a.detail?.status ?? "running")
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 10) {
                    Image(systemName: "person.2").font(.system(size: 12)).foregroundStyle(K.text3)
                    Text("子 Agent").font(KFont.sans(12)).foregroundStyle(K.text3)
                    Text(a.detail?.title ?? "…").font(KFont.sans(16, .bold)).lineLimit(1)
                    Spacer()
                    if let u = a.detail?.usage {
                        Text("\(u.steps) 步 · \(u.tokens) tokens").font(KFont.mono(11)).foregroundStyle(K.text4)
                    }
                    HStack(spacing: 6) {
                        Dot(color: kind.color, size: 7)
                        Text(kind.label).font(KFont.sans(12)).foregroundStyle(K.text2)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 12)
                HLine(color: K.line2)

                if let e = a.error, a.detail == nil {
                    Text(e).font(KFont.sans(13)).foregroundStyle(K.red).padding(18)
                } else {
                    HStack(spacing: 0) {
                        VStack(alignment: .leading, spacing: 2) {
                            if steps.isEmpty {
                                Text(a.detail == nil ? "正在读取…" : "还没有步骤")
                                    .font(KFont.sans(13)).foregroundStyle(K.text4).padding(16)
                            }
                            ForEach(win, id: \.self) { i in
                                let p = steps[i]
                                Button { store.agentView?.step = i } label: {
                                    HStack(spacing: 10) {
                                        StepDot(step: p)
                                        Text(p.title)
                                            .font(KFont.sans(p.status == "note" ? 12 : 13))
                                            .foregroundStyle(p.kind == .step && p.status != "note" ? K.ink : K.text2)
                                            .strikethrough(p.status == "denied", color: K.text4)
                                            .lineLimit(1)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                        Text(TimeText.offset(p.ts, from: created))
                                            .font(KFont.mono(11)).foregroundStyle(K.text4)
                                        Kbd("⌘\(i - win.lowerBound + 1)", active: i == cs)
                                    }
                                    .padding(8)
                                }
                                .buttonStyle(RowStyle(selected: i == cs, selectedFill: K.line3, radius: 6))
                            }
                            if steps.count > ListWindow.size {
                                WindowBar(window: win, total: steps.count, unit: "步", up: "↑", down: "↓") { store.agentScroll($0) }
                                    .padding(.horizontal, 8)
                                    .padding(.top, 4)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(8)
                        .frame(width: 320)
                        .frame(maxHeight: .infinity, alignment: .top)

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
                    .frame(height: 300)

                    if let f = a.detail?.final, !f.isEmpty {
                        HLine(color: K.line2)
                        VStack(alignment: .leading, spacing: 6) {
                            SmallCaps("它的汇报")
                            ScrollView {
                                MarkdownView(text: f)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .frame(maxHeight: 140)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.horizontal, 18)
                        .padding(.vertical, 12)
                    }
                }

                HStack(spacing: 14) {
                    HStack(spacing: 5) { Kbd("↑↓"); Text("选步骤") }
                    HStack(spacing: 5) { Kbd("⌘1–5"); Text("眼前第几步") }
                    Spacer()
                    HStack(spacing: 5) { Kbd("esc"); Text("回到主任务") }
                }
                .font(KFont.sans(12))
                .foregroundStyle(K.text2)
                .padding(.horizontal, 18)
                .padding(.vertical, 10)
                .background(K.bar)
            }
            .frame(width: 740)
            .background(Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .shadow(color: .black.opacity(0.28), radius: 24, y: 12)
        }
    }
}

// MARK: - 归档的任务（启动器 ⌘⇧A）

struct ArchivedList: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        let L = store.launcher
        let win = store.archWindow
        VStack(spacing: 2) {
            HStack {
                SectionLabel("已归档 · \(L.archived.count) 个")
                Spacer()
                Text("↵ 打开 · ⌘R 恢复 · esc 返回")
                    .font(KFont.sans(12))
                    .foregroundStyle(K.text4)
            }
            .padding(.horizontal, 8)
            .padding(.top, 4)
            .padding(.bottom, 8)

            if L.archLoading && L.archived.isEmpty {
                VStack(spacing: 8) { SkeletonBar(); SkeletonBar() }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
            } else if L.archived.isEmpty {
                Text("没有归档的任务")
                    .font(KFont.sans(13))
                    .foregroundStyle(K.text4)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 22)
            }
            ForEach(win, id: \.self) { i in
                let t = L.archived[i]
                Button {
                    if store.launcher.archPick == i { store.openArchivedTask(t) } else { store.launcher.archPick = i }
                } label: {
                    HStack(spacing: 14) {
                        Dot(color: K.dash, size: 8, hollow: false)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(t.title).font(KFont.sans(14, .bold)).foregroundStyle(K.text2).lineLimit(1)
                            HStack(spacing: 0) {
                                if !Workspaces.isScratch(t.workdir) {
                                    Text("\(Workspaces.name(t.workdir)) · ").font(KFont.mono(11)).foregroundStyle(K.text4)
                                }
                                Text(t.line).font(KFont.sans(12)).foregroundStyle(K.text3)
                            }
                            .lineLimit(1)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        Text("归档于 " + TimeText.relative(t.archivedAt ?? t.updated))
                            .font(KFont.mono(12))
                            .foregroundStyle(K.text4)
                        Kbd("⌘\(i - win.lowerBound + 1)", active: L.archPick == i, size: 12, weight: .medium, minWidth: 34)
                    }
                    .padding(.vertical, 10)
                    .padding(.horizontal, 12)
                }
                .buttonStyle(RowStyle(selected: L.archPick == i, selectedFill: .white, radius: 8, selectedStroke: K.ink))
            }
            if L.archived.count > ListWindow.size {
                WindowBar(window: win, total: L.archived.count) { store.archScroll($0) }
                    .padding(.horizontal, 8)
                    .padding(.top, 8)
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 8)
    }
}
