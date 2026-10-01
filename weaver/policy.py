"""规则（Policy）：内核在固定关卡上问的“该不该”。

换一套规则就是不同的产品，内核不用改。默认规则很宽松。
"""
from __future__ import annotations

from . import guards
from .kernel import Continue, Deny, Escalate, Nudge, State, Stop, Verdict, WaitSpec
from .todo import render as render_todos, unfinished

TODO_STALE = 10                 # 清单没做完、这么多步没更新就提醒
BUDGET_WARN = 0.8               # 步数或预算用到这个比例时提醒一次


def compact_threshold(window: int, max_output: int = 8192, tool_reserve: int = 30_000,
                      ratio: float = 0.85, soft_cap: int | None = None) -> int:
    """上下文到多大就该压缩。见 design/compaction.md 第四节。

    两种算法取较小的：小窗口靠“预留”保证不撑爆，大窗口靠“百分比”保证不压得太满。
    “不低于一半”只兜住预留减过头的情况；软上限是用户主动要求早压，放在最后。
    """
    threshold = max(min(int(window * ratio), window - max_output - tool_reserve), window // 2)
    return min(threshold, soft_cap) if soft_cap else threshold


class Policy:
    """默认规则：输入都收；工具都直接执行；模型说完就算完；超限给一次收尾；调模型和压缩可以重做。"""

    def __init__(self, max_steps: int = 20, token_budget: int = 200_000, context_window: int = 128_000,
                 max_output: int = 8192, tool_reserve: int = 30_000, compact_ratio: float = 0.85,
                 compact_at: int | None = None, interactive: bool = False, loop_thresholds: dict | None = None):
        self.limits = (max_steps, token_budget)
        self.interactive = interactive          # 有人值守：卡住时先问人，而不是直接停
        self.loop_thresholds = {**guards.THRESHOLDS, **(loop_thresholds or {})}
        self.context_window = context_window
        self.compact_threshold = compact_threshold(context_window, max_output, tool_reserve,
                                                   compact_ratio, compact_at)

    def on_input(self, input_event: dict, state: State) -> Verdict:
        """这条输入收不收。返回 accept（可带 scope）/ reject / hold。"""
        return Verdict("accept")

    def wait_for(self, call: dict, state: State) -> WaitSpec | Deny | None:
        """这个工具调用：None 直接执行；WaitSpec 先等（比如等人审批）；Deny 直接拒绝。"""
        return None

    def on_model_done(self, state: State) -> Continue | None:
        """模型说做完了，真的完了吗。返回 Continue(feedback) 表示没完。
        默认：清单里还有没做完的，打回一次（同一张清单只打回一次，模型坚持就放行，免得死循环）。"""
        left = unfinished(state.todos)
        given = state.nudges.get("todo_done")
        if left and not (given and given[0] > state.todo_seq):
            return Continue("任务清单里还有这些没完成：\n" + render_todos(left) +
                            "\n确实做完了，就用 todo_write 把它们标成 completed；不做了就标 cancelled 并说明原因；"
                            "还没做完就接着做。", kind="todo_done")
        return None

    def guard(self, state: State) -> Stop | Escalate | Nudge | None:
        """防跑偏：卡死检测（先提醒，到次数再问人或停下）、todo 提醒、预算提醒。一次只给一个。"""
        r = state.run
        since = max((step for _, step, v in r.stuck_answers), default=-1)   # 问过人之后重新计数
        history = [h for h in r.history if h["step"] > since]
        found = guards.detect(history)
        for f in found:                                                      # 1. 到“停下”次数
            nudge_at, stop_at = self.loop_thresholds[f.kind]
            if f.count >= stop_at:
                reason = f"{f.desc}（已提醒过仍在继续）"
                return Escalate(reason) if self.interactive else Stop(reason)
        for f in found:                                                      # 2. 到“提醒”次数
            nudge_at, _ = self.loop_thresholds[f.kind]
            last = state.nudges.get(f"loop:{f.kind}")
            if f.count >= nudge_at and not (last and last[1] >= f.start_step):
                return Nudge(f"loop:{f.kind}", f"{f.desc}：换个思路，或者说明卡在哪里、需要用户做什么。")
        left = unfinished(state.todos)
        if left:                                                             # 3. todo
            restored = state.nudges.get("todo_restore")
            if state.last_summary_seq > state.todo_seq and not (restored and restored[0] > state.last_summary_seq):
                return Nudge("todo_restore", "上下文刚压缩过。这是当前的任务清单，按它继续：\n" +
                             render_todos(state.todos))
            stale = state.nudges.get("todo_stale")
            since_update = state.model_steps - max(state.todo_step, stale[1] if stale else 0)
            if since_update >= TODO_STALE:
                return Nudge("todo_stale", f"任务清单已经 {since_update} 步没更新了。这是当前清单：\n" +
                             render_todos(state.todos) + "\n做完的请标 completed；计划变了请更新清单。")
        max_steps, budget = self.limits                                      # 4. 预算
        warned = state.nudges.get("budget")
        if not (warned and warned[0] > r.start_seq) and not r.wrapped_up and (
                r.steps >= BUDGET_WARN * max_steps or r.tokens >= BUDGET_WARN * budget):
            return Nudge("budget", f"提醒：这个任务已用 {r.steps}/{max_steps} 步、"
                                   f"约 {r.tokens * 100 // max(budget, 1)}% 的 token 预算。请抓紧收尾："
                                   "先完成最重要的部分，用完之前总结做了什么、还剩什么。")
        return None

    def should_compact(self, state: State) -> bool:
        """上下文快满了，该压缩了。"""
        return state.context_tokens >= self.compact_threshold

    def retry_safe(self, kind: str) -> bool:
        """崩溃后这类动作能不能重做。"""
        return kind in ("model", "compact")


__all__ = ["Policy", "Verdict", "WaitSpec", "Deny", "Continue", "compact_threshold"]
