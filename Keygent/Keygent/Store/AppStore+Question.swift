import Foundation

// MARK: - 问你（ask_user，api.md v1.6）：问题卡、自动弹出、输入锁。见 design/ask-user.md 第四节。

extension AppStore {
    /// 自动弹出后吞掉按键的时长：防止你正在别处打的字落进回答框
    static let questionLock: TimeInterval = 0.5

    /// 这个任务在等的问题（有审批排在前面也照样是它：问题卡和放行卡分开显示）
    var taskQuestion: WaitItem? { taskWaits.first { $0.isQuestion } }

    /// 输入框是不是“回答”模式（任务页、详情页共用）
    var answerMode: Bool { taskQuestion != nil }

    // MARK: 来了一个问题

    /// 新的问题到来：按弹出规则决定弹面板 / 只出横幅 / 排着
    func questionArrived(_ w: WaitItem) {
        guard !dismissedQuestions.contains(w.id) else { return }
        // 已经有一张问题卡开着：排着，答完那个再出这个（questionFinished）
        if panelVisible, taskQuestion != nil, route == .task || route == .detail(.task) || route == .detail(.queue) { return }
        if !panelVisible {
            panelVisible = true                    // 同步置上：同一批到的第二个问题要排着，不能再弹一次
            poppedQuestion = w.id
            questionLockUntil = Date().addingTimeInterval(Self.questionLock)
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.questionLock) { [weak self] in
                guard let self, let t = self.questionLockUntil, t <= Date() else { return }
                self.questionLockUntil = nil
            }
            popPanel()
            openQuestion(w)
            return
        }
        if userBusy {
            showBanner("\(w.taskTitle) 问你 · 点这里回答", for: w)
            return
        }
        openQuestion(w)
    }

    /// 面板开着时，你是不是正在忙别的（打字、改参数、逐件看）：这时不打断，只出横幅
    var userBusy: Bool {
        if settings != nil || editor != nil || (route == .queue && queue.expanded) { return true }
        switch route {
        case .launcher: return !launcher.query.isEmpty || !launcher.attachments.isEmpty
        case .task: return !task.draft.isEmpty
        case .detail: return !detail.draft.isEmpty
        case .queue: return false
        }
    }

    func showBanner(_ text: String, for w: WaitItem? = nil) {
        bannerWork?.cancel()
        banner = text
        bannerFor = w
        let work = DispatchWorkItem { [weak self] in self?.banner = nil; self?.bannerFor = nil }
        bannerWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: work)
    }

    /// 打开这个问题的问题卡，光标在回答框
    func openQuestion(_ w: WaitItem) {
        banner = nil
        if task.id != w.task || route == .launcher || route == .queue {
            openTask(id: w.task)
        } else if case .detail = route {
            // 详情页里也能答：留在原地
        } else {
            go(.task)
        }
        task.optionPick = nil
        requestFocus(route == .task ? .task : .detail)
    }

    /// 下一件还没被你收起的问题
    var nextQuestion: WaitItem? {
        waits.first { $0.isQuestion && !dismissedQuestions.contains($0.id) }
    }

    /// 点胶囊 / 点横幅该去的问题：横幅上那个 → 没收起的 → 任何一个（收起的也算，这就是“随时回来”）
    var questionTarget: WaitItem? {
        if let w = bannerFor, waits.contains(where: { $0.id == w.id }) { return w }
        return nextQuestion ?? waits.first { $0.isQuestion }
    }

    // MARK: 回答

    func questionSend(_ w: WaitItem, _ text: String) {
        let v = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !v.isEmpty, task.answering == nil else { return }
        let (taskDraft, detailDraft) = (task.draft, detail.draft)
        task.draft = ""
        detail.draft = ""
        answer(w, "answer", note: v) { [weak self] ok in
            guard let self else { return }
            if ok {
                self.questionFinished(w)
            } else {                                // 没发出去：把打的字放回去
                self.task.draft = taskDraft
                self.detail.draft = detailDraft
            }
        }
    }

    func questionOption(_ w: WaitItem, _ n: Int) {
        let opts = w.questionOptions
        guard n >= 1, n <= opts.count else { return }              // 超出选项个数：什么都不做
        questionSend(w, opts[n - 1])
    }

    func questionSkip(_ w: WaitItem) {
        guard task.answering == nil else { return }
        answer(w, "skip") { [weak self] ok in if ok { self?.questionFinished(w) } }
    }

    /// esc：稍后再答。这个问题不再自动弹，胶囊一直提醒；面板收起，键盘回到原来的 App
    func questionLater(_ w: WaitItem) {
        dismissedQuestions.insert(w.id)
        if poppedQuestion == w.id { poppedQuestion = nil }
        task.optionPick = nil
        hidePanel()
    }

    /// 答完一个：还有没收起的问题就接着出；这个是自动弹出来的就收起面板，回到原来的 App
    private func questionFinished(_ w: WaitItem) {
        task.optionPick = nil
        let popped = poppedQuestion == w.id
        if popped { poppedQuestion = nil }
        if let next = nextQuestion, next.id != w.id {
            if popped { poppedQuestion = next.id }
            openQuestion(next)
            return
        }
        if popped { hidePanel() }
    }

    // MARK: 按键（任务页、详情页共用；不是问题时返回 nil，交给原来的处理）

    func questionKey(_ e: KeyEvent, draft: String) -> Bool? {
        guard let w = taskQuestion else { return nil }
        if e.key == .escape { questionLater(w); return true }
        if e.cmd, e.key == .enter, !e.shift { questionSkip(w); return true }
        // ⌘数字：选第几个选项立即发送（任务页展开过程时，⌘数字 仍是选步骤）
        if e.cmd, let d = e.digit, !(route == .task && task.proc) {
            questionOption(w, d)
            return true
        }
        guard e.inInput else { return nil }
        let empty = draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        // ⇧↵ ⌥↵ ⌃↵：输入框是单行的，不换行也不发送，免得习惯性换行时发出半句、或被当成普通追问
        if e.key == .enter, !e.plain || e.shift { return true }
        if e.key == .enter, e.plain {
            if !empty {
                questionSend(w, draft)
            } else if let p = task.optionPick {
                questionOption(w, p + 1)
            }
            return true                                             // 框空、没高亮：不做事
        }
        if empty, e.plain, e.key == .up || e.key == .down, !w.questionOptions.isEmpty {
            let n = w.questionOptions.count
            if let p = task.optionPick {
                task.optionPick = max(0, min(n - 1, p + (e.key == .down ? 1 : -1)))
            } else {
                task.optionPick = e.key == .down ? 0 : n - 1
            }
            return true
        }
        return nil
    }
}
