"""Weaver 内核：翻账本（fold）、做决定（decide）。

这里没有任何 I/O：不调模型、不读文件、不生成 id、不看时间。
同样的账本 + 同样的规则，永远得到同样的决定。设计见 design/kernel.md。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# 由内核 / Runtime 自己产生的输入，直接接受，不经审核
AUTO_ACCEPT_SOURCES = ("policy", "system", "context")
# 只提供背景（如记忆快照）的输入：不开启任务，也不算“新输入”
CONTEXT_SOURCES = ("context",)
TERMINAL = ("done", "truncated", "refusal", "budget", "cancelled", "error", "stuck")

INTERRUPTED = "执行中断，结果未知。请先检查现状，再决定是否重试。"
TRUNCATED_ARGS = "上一条回复在输出上限处被截断，工具参数可能不完整，本次未执行。请重新发起调用。"
CANCELLED = "任务已被取消，本次未执行。"
WRAPPED_UP = "预算已用完，不再执行工具。"
ANSWERED = "用户回答：{}"                    # 问用户（ask_user）的结果，见 design/ask-user.md
SKIPPED = "用户这次没回答。按你自己的判断继续，并在结论里写明你做了什么假设。"
OVERFLOW_AGAIN = "上下文超出模型窗口，压缩后仍放不下，已停止"
IMAGE_BYTES = 4500                     # 估算上下文时，一张图按约 1500 token 算
BUDGET_REMINDER = ("步数或 token 预算已用完。不要再调用工具，"
                   "直接总结已经完成的工作和还剩下的事。")


def billable(usage: dict) -> int:
    """计入预算的 token：命中缓存的输入按一折算（大致是实际价格），其余照算。
    每轮都重发整段上下文，按全价累计的话，几十轮就把预算“花光”了，实际却没花多少钱。"""
    total_in = usage.get("input_tokens", 0) or 0
    cached = min(usage.get("cached_tokens", 0) or 0, total_in)
    return total_in - cached + cached // 10 + (usage.get("output_tokens", 0) or 0)


# ---------------------------------------------------------------- 现状


@dataclass
class Reply:
    """一次模型调用的结果（来自 ActionCompleted(kind=model)）。"""
    seq: int
    content: list[dict]
    stop: str
    is_error: bool
    wrap_up: bool
    error: str = ""
    error_kind: str = ""               # context：上下文超长

    @property
    def overflow(self) -> bool:
        """模型因为上下文超长没能正常回复。"""
        return (self.is_error and self.error_kind == "context") or self.stop == "context_exceeded"

    @property
    def calls(self) -> list[dict]:
        return [p for p in self.content if p["type"] == "tool_call"]

    @property
    def text(self) -> str:
        return "".join(p["text"] for p in self.content if p["type"] == "text")


@dataclass
class Run:
    id: str
    scope: Any
    start_seq: int
    status: str = "running"
    steps: int = 0
    tokens: int = 0
    last_reply: Reply | None = None
    last_input_seq: int = 0            # 本任务里最近一条已接受输入的位置
    wrapped_up: bool = False
    cancelled: bool = False
    overflow_compactions: int = 0      # 本任务里因为“上下文超长”做过几次压缩
    calls: dict[str, dict] = field(default_factory=dict)       # 本任务里模型发出过的调用：id -> tool_call
    history: list[dict] = field(default_factory=list)          # 本任务的工具结果记录（防死循环用）
    stuck_answers: list[tuple] = field(default_factory=list)   # 判定卡住后问人的回答：(seq, 第几步, 回答)
    pending: dict[str, dict] = field(default_factory=dict)     # call_id -> tool_call，保持顺序
    in_flight: dict[str, dict] = field(default_factory=dict)   # action_id -> ActionStarted

    @property
    def active(self) -> bool:
        return self.status == "running"


@dataclass
class Wait:
    id: str
    kind: str
    payload: dict
    resolved: bool = False
    value: Any = None
    seq: int = 0                       # 开始等的位置
    closed: bool = False               # 所属的那一轮结束了，这个等待作废（不是被回答的）


@dataclass
class State:
    run: Run | None = None
    last_finish_seq: int = 0
    inputs: dict[str, dict] = field(default_factory=dict)       # input_id -> InputReceived
    judgments: dict[str, dict] = field(default_factory=dict)    # input_id -> 最新的 InputJudged
    accepted: list[tuple[int, str]] = field(default_factory=list)  # (接受时的 seq, input_id)
    waits: dict[str, Wait] = field(default_factory=dict)
    # 上下文大小估算：上一次模型回复的真实用量（或压缩后的估算）+ 之后新增内容的字节数 ÷ 3
    ctx_base: int = 0
    ctx_growth_bytes: int = 0
    last_model_seq: int = 0
    last_compact_seq: int = 0
    last_summary_seq: int = 0          # 最近一次摘要式压缩（之后 todo 可能要补回来）
    model_steps: int = 0               # 整个会话的模型调用次数（提醒的冷却按它算）
    todos: list[dict] | None = None    # 当前 todo 清单 = 最近一次成功的 todo_write 的参数
    todo_seq: int = 0
    todo_step: int = 0
    nudges: dict[str, tuple[int, int]] = field(default_factory=dict)   # 提醒类型 -> (seq, 第几步)
    folded: int = 0                    # 已经折算了账本里的前几条（接着 fold 时从这里继续）
    last_id: str = ""                  # 最后折算的那条的 id（用来确认账本没被换掉）

    @property
    def context_tokens(self) -> int:
        return self.ctx_base + self.ctx_growth_bytes // 3

    @property
    def unjudged(self) -> list[str]:
        """还没审核的外部输入，以及审核结果为 hold、且等待已经有结论的输入。"""
        out = []
        for iid, ev in self.inputs.items():
            if ev["source"] in AUTO_ACCEPT_SOURCES:
                continue
            j = self.judgments.get(iid)
            if j is None:
                out.append(iid)
            elif j["verdict"] == "hold":
                w = self.wait_for_input(iid)
                if w and w.resolved:
                    out.append(iid)
        return out

    @property
    def open_waits(self) -> list[Wait]:
        return [w for w in self.waits.values() if not w.resolved]

    def new_inputs(self) -> list[str]:
        """上一个任务结束后才被接受的输入：它们会开启新任务。"""
        return [iid for seq, iid in self.accepted if seq > self.last_finish_seq]

    def wait_for_call(self, call_id: str) -> Wait | None:
        return next((w for w in self.waits.values() if w.payload.get("call_id") == call_id), None)

    def wait_for_input(self, input_id: str) -> Wait | None:
        return next((w for w in self.waits.values() if w.payload.get("input_id") == input_id), None)


def fold(events: list[dict], state: State | None = None) -> State:
    """从头翻一遍账本，算出现状。"""
    # 可以接着上次的结果继续：fold 只是从头到尾逐条处理、不往回看，所以“一次 fold 全部”和
    # “先 fold 前一段、再接着 fold 后面”结果完全一样。账本被换掉了（变短、或接续点的 id 对不上）就从头来。
    if state is not None and len(events) >= state.folded and (
            state.folded == 0 or events[state.folded - 1].get("id") == state.last_id):
        s, events, start = state, events[state.folded:], state.folded
    else:
        s, start = State(), 0

    def accept(iid: str, seq: int) -> None:
        s.accepted.append((seq, iid))
        for p in s.inputs[iid].get("content") or []:
            s.ctx_growth_bytes += len(p.get("text", "").encode()) if p["type"] == "text" else IMAGE_BYTES
        if s.run and s.run.active:
            s.run.last_input_seq = seq

    for ev in events:
        t, seq = ev["type"], ev["seq"]
        run = s.run if s.run and s.run.active else None

        if t == "RunStarted":
            s.run = Run(id=ev["run_id"], scope=ev.get("scope"), start_seq=seq)
            news = [q for q, _ in s.accepted if q > s.last_finish_seq]
            s.run.last_input_seq = max(news, default=seq)
        elif t == "RunFinished":
            if run:
                run.status = ev["status"]
                run.in_flight.clear()
                # 这一轮里没回答的审批、“卡住了问人”随之作废（比如等审批时被取消），不再挂在“等你的事”里。
                # 外部输入的确认属于整个会话，不跟着这一轮走。
                for w in s.waits.values():
                    if not w.resolved and w.seq > run.start_seq and ("call_id" in w.payload or "sub" in w.payload or w.kind == "stuck"):
                        w.resolved, w.closed = True, True
            s.last_finish_seq = seq
        elif t == "InputReceived":
            s.inputs[ev["id"]] = ev
            if ev.get("nudge"):
                s.nudges[ev["nudge"]] = (seq, s.model_steps)
            if ev["source"] in CONTEXT_SOURCES:     # 只算进上下文大小
                s.ctx_growth_bytes += sum(len(p.get("text", "").encode()) for p in ev["content"])
            elif ev["source"] in AUTO_ACCEPT_SOURCES:
                accept(ev["id"], seq)
        elif t == "InputJudged":
            s.judgments[ev["input_id"]] = ev
            if ev["verdict"] == "accept":
                accept(ev["input_id"], seq)
        elif t == "WaitStarted":
            s.waits[ev["wait_id"]] = Wait(ev["wait_id"], ev["kind"], ev.get("payload") or {}, seq=seq)
        elif t == "WaitResolved":
            w = s.waits.get(ev["wait_id"])
            if w and not w.resolved:
                w.resolved, w.value = True, ev.get("value")
                if w.kind == "stuck" and run:
                    run.stuck_answers.append((seq, s.model_steps, w.value))
        elif t == "Cancelled":
            if run:
                run.cancelled = True
        elif t == "ActionStarted":
            if run:
                run.in_flight[ev["action_id"]] = ev
        elif t == "ActionCompleted" and ev["kind"] == "compact":
            out = ev.get("output") or {}
            if run:
                run.in_flight.pop(ev["action_id"], None)
                run.tokens += billable(out.get("usage") or {})
                if out.get("reason") == "overflow":
                    run.overflow_compactions += 1
            if out.get("mode") in ("trim", "summary"):
                s.ctx_base, s.ctx_growth_bytes = out.get("tokens_after", s.ctx_base), 0
            if out.get("mode") == "summary":
                s.last_summary_seq = seq
            s.last_compact_seq = seq
        elif t == "ActionCompleted" and ev["kind"] == "extract":
            if run:                                 # 记忆提取只是记录：不进对话、不算这一轮的预算
                run.in_flight.pop(ev["action_id"], None)
        elif t == "ActionCompleted":
            if not run:
                continue
            started = run.in_flight.pop(ev["action_id"], None)
            if ev["kind"] == "model":
                out = ev.get("output") or {}
                wrap_up = bool(started and (started.get("input") or {}).get("wrap_up"))
                reply = Reply(seq=seq, content=out.get("content") or [], stop=out.get("stop", "other"),
                              is_error=ev.get("is_error", False), wrap_up=wrap_up,
                              error=out.get("error", ""), error_kind=out.get("error_kind", ""))
                usage = out.get("usage") or {}
                if not ev.get("synthetic"):
                    run.steps += 1
                    s.model_steps += 1
                    run.tokens += billable(usage)
                run.calls.update({c["id"]: c for c in reply.calls})
                run.last_reply = reply
                run.wrapped_up = run.wrapped_up or wrap_up
                run.pending = {} if reply.is_error or reply.overflow else {c["id"]: c for c in reply.calls}
                s.last_model_seq = seq
                if not reply.is_error and not reply.overflow:
                    s.ctx_base = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    s.ctx_growth_bytes = 0
            else:
                run.pending.pop(ev["action_id"], None)
                run.tokens += billable(ev.get("usage") or {})   # 子 Agent 等工具的用量，计入本任务预算
                out = ev.get("output")
                text = out if isinstance(out, str) else str(out)
                s.ctx_growth_bytes += len(text.encode())
                call = run.calls.get(ev["action_id"]) or {}
                name, args = call.get("name", "?"), call.get("args")
                run.history.append({"step": s.model_steps, "name": name, "error": bool(ev.get("is_error")),
                                    "key": name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False),
                                    "result": text})
                if name == "todo_write" and not ev.get("is_error") and isinstance(args, dict):
                    s.todos, s.todo_seq, s.todo_step = args.get("todos") or [], seq, s.model_steps
    if events:
        s.folded, s.last_id = start + len(events), events[-1].get("id", "")
    return s


# ---------------------------------------------------------------- 要做的事


@dataclass
class Judge:            # → InputJudged
    input_id: str
    verdict: str        # accept / reject / hold
    reason: str = ""
    scope: Any = None


@dataclass
class StartRun:         # → RunStarted
    scope: Any = None


@dataclass
class CallModel:        # → ActionStarted + ActionCompleted(kind=model)，真动手
    wrap_up: bool = False
    retry_of: str | None = None


@dataclass
class Compact:          # → ActionStarted + ActionCompleted(kind=compact)，由 Runtime 决定怎么压
    reason: str = "threshold"          # threshold / overflow / manual
    retry_of: str | None = None


@dataclass
class Execute:          # → ActionStarted + ActionCompleted(kind=tool)，真动手
    call: dict


@dataclass
class Synthesize:       # → ActionCompleted(kind=tool, is_error, synthetic)，不执行
    call: dict
    message: str
    is_error: bool = True           # 问用户的回答（ask_user）不是错误


@dataclass
class StartWait:        # → WaitStarted
    kind: str
    payload: dict


@dataclass
class AddInput:         # → InputReceived（内核自己说的话）
    text: str
    source: str = "policy"
    meta: dict = field(default_factory=dict)      # 比如 {"nudge": "loop:repeat"}：记下是哪种提醒，用来冷却


@dataclass
class Finish:           # → RunFinished
    status: str
    text: str = ""


@dataclass
class Block:            # 有事在等，执行者停下
    pass


Action = Judge | StartRun | CallModel | Compact | Execute | Synthesize | StartWait | AddInput | Finish | Block


# ---------------------------------------------------------------- 做决定


def _allowed(value: Any) -> bool:
    return value is True or (isinstance(value, dict) and bool(value.get("allow")))


def _note(value: Any) -> str:
    return value.get("note", "") if isinstance(value, dict) else ""


def decide(s: State, policy) -> list[Action]:
    """看现状，按规则表（design/kernel.md 第五节）决定下一步。第一条符合的就照做。"""
    # 0. 有还没审核的输入：逐条审核
    if s.unjudged:
        acts: list[Action] = []
        for iid in s.unjudged:
            held = s.judgments.get(iid)
            if held and held["verdict"] == "hold":
                w = s.wait_for_input(iid)
                ok = _allowed(w.value)
                acts.append(Judge(iid, "accept" if ok else "reject", _note(w.value), held.get("scope")))
                continue
            v = policy.on_input(s.inputs[iid], s)
            acts.append(Judge(iid, v.verdict, v.reason, v.scope))
            if v.verdict == "hold":
                acts.append(StartWait("approval", {"input_id": iid, "reason": v.reason}))
        return acts

    # 1. 没有进行中的任务：有新输入就开一个，否则什么都不做
    r = s.run
    if r is None or not r.active:
        new = s.new_inputs()
        if not new:
            return []
        first = s.judgments.get(new[0])
        return [StartRun(scope=first.get("scope") if first else None)]

    pending = list(r.pending.values())
    last = r.last_reply

    # 2. 用户喊了取消
    if r.cancelled:
        return [Synthesize(c, CANCELLED) for c in pending] + [Finish("cancelled")]

    # 3. 开始了但没完成的动作（只在崩溃恢复时出现）
    if r.in_flight:
        acts = []
        for aid, ev in r.in_flight.items():
            if ev["kind"] == "model":
                if policy.retry_safe("model"):
                    acts.append(CallModel(wrap_up=bool(ev["input"].get("wrap_up")), retry_of=aid))
                else:
                    acts.append(Finish("error", "模型调用中断"))
            elif ev["kind"] == "compact":
                if policy.retry_safe("compact"):
                    acts.append(Compact((ev.get("input") or {}).get("reason", "threshold"), retry_of=aid))
                else:
                    acts.append(Finish("error", "压缩中断"))
            elif policy.retry_safe(ev["kind"]):
                acts.append(Execute(ev["input"]))
            else:
                acts.append(Synthesize(ev["input"], INTERRUPTED))
        return acts

    # 4. 有工具调用在等（审批等），或判定卡住后在等人：停下
    if any(w.payload.get("call_id") in r.pending or (w.payload.get("blocking") and w.payload.get("run_id") == r.id)
           for w in s.open_waits):
        return [Block()]

    # 判定卡住后问了人：选“停下”就结束；选“继续”并给了提示，就把提示交给模型
    if r.stuck_answers:
        a_seq, _, answer = r.stuck_answers[-1]
        if a_seq > (last.seq if last else 0):
            if not _allowed(answer):
                return [Finish("stuck", "判定卡住后，用户选择停下")]
            if _note(answer) and a_seq > r.last_input_seq:
                return [AddInput(f"用户看了当前的情况，给出提示：{_note(answer)}", "policy", {"nudge": "stuck_hint"})]

    # 上下文超长：强制压缩后重试一次；压缩过还超长就停
    if last and last.overflow and last.seq > r.last_input_seq:
        if s.last_compact_seq > last.seq:
            return [CallModel()]
        if r.overflow_compactions < 1:
            return [Compact("overflow")]
        return [Finish("error", OVERFLOW_AGAIN)]

    # 模型调用本身失败了（重试后仍失败），且之后没有新输入
    if last and last.is_error and last.seq > r.last_input_seq:
        return [Finish("error", last.error or "模型调用失败")]

    # 模型拒绝：不执行它的任何调用
    if last and last.stop == "refusal" and last.seq > r.last_input_seq:
        return [Synthesize(c, "模型拒绝了这次请求。") for c in pending] + [Finish("refusal")]

    # 7. 已经做过预算收尾：不再执行工具，直接结束
    if last and last.wrap_up:
        status = "truncated" if last.stop == "truncated" else "budget"
        return [Synthesize(c, WRAPPED_UP) for c in pending] + [Finish(status, last.text)]

    # 5. 被截断且带工具调用：参数可能只写了一半，不执行
    if pending and last.stop == "truncated":
        return [Synthesize(c, TRUNCATED_ARGS) for c in pending]

    # 6. 还有没执行的工具调用
    if pending:
        acts = []
        for c in pending:
            if c.get("args") is None:
                acts.append(Synthesize(c, f"参数不是合法的 JSON，本次未执行：{c.get('raw_args', '')!r}"))
                continue
            w = s.wait_for_call(c["id"])
            if w and w.kind == "question":          # 问用户（ask_user）：回答就是这次调用的结果，不执行工具
                answer = w.value.get("answer") if isinstance(w.value, dict) else None
                acts.append(Synthesize(c, ANSWERED.format(answer) if answer else SKIPPED, is_error=False))
                continue
            if w:                                   # 等待已有结论（没结论的在第 4 条就停了）
                edited = w.value.get("args") if isinstance(w.value, dict) else None
                if _allowed(w.value) and isinstance(edited, dict) and edited != c["args"]:
                    # 用户改了参数再放行：用新参数执行。人放行只代替“问人”，不代替硬拦截
                    call = {**c, "args": edited, "edited_from": c["args"]}
                    spec = policy.wait_for(call, s)
                    acts.append(Synthesize(c, f"用户改过的参数被权限规则拒绝：{spec.reason}")
                                if isinstance(spec, Deny) else Execute(call))
                elif _allowed(w.value):
                    acts.append(Execute(c))
                else:
                    note = _note(w.value)
                    acts.append(Synthesize(c, "用户拒绝了这次调用。" + (f"原因：{note}" if note else "")))
                continue
            spec = policy.wait_for(c, s)
            if isinstance(spec, Deny):              # 规则直接拒绝：不执行，也不问人
                acts.append(Synthesize(c, f"被权限规则拒绝：{spec.reason}"))
            elif spec:
                acts.append(StartWait(spec.kind, {"call_id": c["id"], "name": c["name"],
                                                  "args": c["args"], "reason": spec.reason}))
            else:
                acts.append(Execute(c))
        return acts

    # 8. 模型这次没要工具，而且之后没有新输入
    if last and not last.calls and last.seq > r.last_input_seq:
        if last.stop == "truncated":
            return [Finish("truncated", last.text)]
        if last.stop not in ("end", "tool_calls"):
            return [Finish("error", last.text or f"未知的停止原因：{last.stop}")]
        verdict = policy.on_model_done(s)
        if isinstance(verdict, Continue):
            return [AddInput(verdict.feedback, "policy", {"nudge": verdict.kind} if verdict.kind else {})]
        return [Finish("done", last.text)]

    # 上下文快满了：先压缩再问模型（刚压过就不再压，防止死循环）
    if policy.should_compact(s) and s.last_compact_seq <= s.last_model_seq:
        return [Compact("threshold")]

    # 防跑偏：卡死检测、todo 提醒、预算提醒（只在上次模型回复之后还没有新输入时检查，一次只插一条）
    if last and last.seq > r.last_input_seq:
        g = policy.guard(s)
        if isinstance(g, Stop):
            return [Finish("stuck", g.reason)]
        if isinstance(g, Escalate):
            return [StartWait("stuck", {"blocking": True, "run_id": r.id, "reason": g.reason})]
        if isinstance(g, Nudge):
            return [AddInput(g.text, "policy", {"nudge": g.kind})]

    # 9. 超出限额：禁用工具，让模型收尾
    max_steps, budget = policy.limits
    if r.steps >= max_steps or r.tokens >= budget:
        return [AddInput(BUDGET_REMINDER, "policy"), CallModel(wrap_up=True)]

    # 10. 其他情况：问模型
    return [CallModel()]


# 规则的回答类型，放在这里是为了 decide 能识别；Policy 本身在 policy.py


@dataclass
class Verdict:
    verdict: str            # accept / reject / hold
    reason: str = ""
    scope: Any = None


@dataclass
class WaitSpec:
    kind: str = "approval"
    reason: str = ""


@dataclass
class Deny:
    reason: str


@dataclass
class Continue:
    feedback: str
    kind: str = ""                  # 记进账本的提醒类型，用来判断“打回过没有”


@dataclass
class Stop:                         # 卡住了，结束任务（stuck）
    reason: str


@dataclass
class Escalate:                     # 卡住了，暂停问人
    reason: str


@dataclass
class Nudge:                        # 插一条提醒再问模型
    kind: str
    text: str
