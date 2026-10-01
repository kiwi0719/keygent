import Foundation

// MARK: - ④ 等你的事

extension AppStore {
    var queueGroups: [(task: String, n: Int)] {
        var order: [String] = []
        var count: [String: Int] = [:]
        for w in waits {
            if count[w.taskTitle] == nil { order.append(w.taskTitle) }
            count[w.taskTitle, default: 0] += 1
        }
        let all = order.map { ($0, count[$0]!) }
        if all.count > 3 {
            let rest = all.dropFirst(3).reduce(0) { $0 + $1.1 }
            return Array(all.prefix(3)) + [("还有 \(all.count - 3) 个任务", rest)]
        }
        return all
    }

    var queueTaskCount: Int { Set(waits.map(\.task)).count }

    /// 能一键放行的（有 allow 选项的）
    var queueBulkCount: Int { waits.filter(\.isBulkApprovable).count }

    /// 「在跑 · 只显示摘要」
    var queueRunning: [TaskSummary] {
        Array(tasks.filter { $0.kind == .run || $0.kind == .queued }.prefix(3))
    }

    var queueLeft: Int { queue.list.filter { queue.decided[$0.id] == nil }.count }

    var queueCurrent: WaitItem? { queue.list.indices.contains(queue.cur) ? queue.list[queue.cur] : nil }

    func openQueue() {
        queue.expanded = false
        go(.queue)
    }

    private func qFit(_ c: Int, _ t: Int) -> Int {
        ListWindow.fit(c, top: t, count: queue.list.count)
    }

    private func qNextOpen(from: Int) -> Int? {
        let n = queue.list.count
        guard n > 0 else { return nil }
        for k in (from + 1)..<max(from + 1, n) where queue.decided[queue.list[k].id] == nil { return k }
        for k in 0..<n where queue.decided[queue.list[k].id] == nil { return k }
        return nil
    }

    func queueExpand() {
        guard !waits.isEmpty else { return }
        queue.list = waits
        queue.decided = [:]
        queue.expanded = true
        let c = qNextOpen(from: -1) ?? 0
        queue.cur = c
        queue.top = qFit(c, 0)
        queue.editDraft = ""
    }

    func queueGo(_ c: Int) {
        guard !queue.list.isEmpty else { return }
        let c = max(0, min(queue.list.count - 1, c))
        queue.cur = c
        queue.top = qFit(c, queue.top)
        queue.editDraft = ""
    }

    func queueMarkDecided(_ id: String, _ label: String) {
        queue.decided[id] = label
        queue.handledCount += 1
        if let c = qNextOpen(from: queue.cur) {
            queue.cur = c
            queue.top = qFit(c, queue.top)
        }
        queue.editDraft = ""
    }

    func queueChoose(_ choice: String, note: String? = nil) {
        guard let w = queueCurrent, queue.decided[w.id] == nil, queue.working == nil else { return }
        if choice == "edit" { openEditor(w); return }
        guard w.choices.contains(choice) else { return }
        answer(w, choice, note: note) { [weak self] ok in
            guard ok, let self else { return }
            let label = note != nil ? "\(w.label(choice))：\(note!)" : w.label(choice)
            self.queueMarkDecided(w.id, label)
        }
    }

    func queuePrimary() {
        guard let c = queueCurrent?.primaryChoice else { return }
        queueChoose(c)
    }

    func queueSecondary() {
        guard let c = queueCurrent?.secondaryChoice else { return }
        queueChoose(c)
    }

    func queueSendEdit() {
        let v = queue.editDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !v.isEmpty, let c = queueCurrent?.noteChoice else { return }
        requestFocus(nil)
        queueChoose(c, note: v)
    }

    /// 全部放行：有 allow 选项的逐个放行；「卡住了」这种没有放行选项的留着
    func queueApproveRest() {
        let targets = (queue.expanded ? queue.list.filter { queue.decided[$0.id] == nil } : waits).filter(\.isBulkApprovable)
        guard !targets.isEmpty else { return }
        Task { @MainActor in
            var ok = 0
            for w in targets {
                do {
                    _ = try await client.answer(w.id, decision: "allow")
                    ok += 1
                    waits.removeAll { $0.id == w.id }
                    queue.decided[w.id] = "放行"
                } catch let e as APIError where e.code == "conflict" {
                    waits.removeAll { $0.id == w.id }
                    queue.decided[w.id] = "已在别处处理"
                } catch {
                    handleError(error)
                    break
                }
            }
            queue.handledCount += ok
            if ok > 0 { flash("已放行 \(ok) 件") }
            if queueLeft == 0 { queue.expanded = false }
            refreshStatusSoon()
        }
    }

    // MARK: 键盘

    func queueKey(_ e: KeyEvent) -> Bool {
        if e.inInput {
            if e.key == .escape { requestFocus(nil); return true }
            if e.key == .enter, !e.cmd { queueSendEdit(); return true }
            return false
        }
        if e.cmd, e.shift, e.key == .enter { queueApproveRest(); return true }

        if !queue.expanded {
            if e.key == .space { queueExpand(); return true }
            if e.cmd, let d = e.digit {
                let r = queueRunning
                if d <= r.count { openTask(id: r[d - 1].id, summary: r[d - 1]) }
                return true
            }
            if e.key == .escape { hidePanel(); return true }
            return false
        }

        // 问你：逐件看里不直接答（⌘1–5 在这里是跳到第几件），↵ / tab 去它的问题卡
        if let w = queueCurrent, w.isQuestion, queue.decided[w.id] == nil,
           e.key == .tab || (e.plain && e.key == .enter) {
            openQuestion(w)
            return true
        }
        if e.key == .tab { requestFocus(.queueEdit); return true }
        if e.key == .escape { queue.expanded = false; return true }
        if e.plain, e.key == .up || e.key == .down {
            queueGo(queue.cur + (e.key == .down ? 1 : -1))
            return true
        }
        if e.cmd, let d = e.digit {
            if d <= QueueState.viewCount { queueGo(queue.top + d - 1) }
            return true
        }
        if e.cmd, e.key == .enter { queuePrimary(); return true }
        if e.plain, e.key == .delete { queueSecondary(); return true }
        return false
    }
}
