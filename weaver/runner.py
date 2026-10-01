"""执行者（runner）：翻账本 → 内核决定 → 去做 → 把结果记进账本，反复。

所有 I/O 都在这里：生成 id 和时间、调模型、执行工具、问人。
本地版遇到“等待”会调 approver 当场问；没有 approver 就停下，等外部把等待解决后再调 run()。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from . import kernel as k
from . import extract
from . import redact as rd
from .cache import mark
from .compaction import Compactor
from .context import pending_context
from .project import project


MIN_EXTRACT_STEPS = 3


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Runner:
    def __init__(self, session_id: str, store, model, tools, policy, system: str,
                 approver: Callable[[k.Wait], object] | None = None,
                 sink: Callable[[dict], None] | None = None, on_delta: Callable[[dict], None] | None = None,
                 parent_id: str | None = None, compactor: Compactor | None = None, memory=None,
                 context: list | None = None, max_parallel: int = 8, redactor=None, extract_memory=None,
                 prefix_messages: list | None = None):
        self.session_id = session_id
        self.store, self.model, self.tools, self.policy = store, model, tools, policy
        self.system = system
        self.approver = approver
        self.sink = sink or (lambda ev: None)        # 账本事件
        self.on_delta = on_delta                        # 流式片段等瞬时消息，不落账本
        self.parent_id = parent_id
        self.compactor = compactor or Compactor()
        self.max_parallel = max(1, max_parallel)        # 一批里最多同时跑几个工具
        self._lock = threading.RLock()                  # 并行时多个线程写账本：同一会话仍只有一个写者
        self.redactor = redactor or rd.default()        # 工具结果进账本前脱敏（见 design/redaction.md）
        self.extract_memory = extract_memory              # 记忆：给了就自动提取（压缩写摘要前、一轮成功结束后）
        self.prefix_messages = list(prefix_messages or [])  # 分叉的子 Agent：父对话到分叉点的投影（固定不变）
        self._state: k.State | None = None             # 上次 fold 的结果：每次只接着处理新增的事件
        # 背景提供者（环境、记忆等，见 weaver/context.py）；memory= 是旧写法，等于把它放进列表
        self.context = list(context or []) + ([memory] if memory is not None and memory not in (context or []) else [])
        self.before_run: list[Callable[[], None]] = []  # 每次 run() 开始前做的事（常驻服务：MCP 连接、换工具清单）

    # ------------------------------------------------ 外部往账本里记的事

    def submit(self, content: list[dict] | str, source: str = "user", author: dict | None = None) -> str:
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if source == "user":                        # 会话开头放背景信息；变了就在末尾追加更新
            self.sync_context()
        ev = self._event("InputReceived", content=content, source=source, author=author)
        self._append([ev])
        return ev["id"]

    def sync_context(self) -> None:
        """背景信息变了就在账本末尾追加更新（平时在用户输入前做；before_run 里的变化也可以主动调）。"""
        for c in pending_context(self.context, self._load()) if self.context else []:
            self._append([self._event("InputReceived", content=[{"type": "text", "text": c["text"]}],
                                      source="context", author=None, context_kind=c["kind"],
                                      digest=c["digest"])])

    def cancel(self, reason: str = "") -> None:
        self._append([self._event("Cancelled", reason=reason)])
        interrupt = getattr(self.tools, "interrupt", None)
        if interrupt:                                  # 正在跑的命令一起结束，执行者才能尽快回到内核收尾
            interrupt()

    def compact(self, reason: str = "manual") -> dict:
        """手动压缩一次（不经过内核决定）。返回压缩结果。"""
        state = self._fold()
        self._do(k.Compact(reason), state)
        return self._load()[-1].get("output") or {}

    # ------------------------------------------------ 记忆提取（design/round3.md 第一节）

    def extract(self, reason: str, run_id: str | None = None) -> dict | None:
        """提取一次记忆，记进账本（kind=extract，内核只当记录）。没开就返回 None。"""
        if self.extract_memory is None or not extract.enabled():
            return None
        out = extract.extract(self.model, self.system, self.tools.specs, self._load(), self.extract_memory,
                              cache_key=self.session_id)
        out["reason"] = reason
        self._append([self._event("ActionCompleted", run_id=run_id, action_id=new_id(), kind="extract",
                                  output=out, is_error=bool(out.get("error")))])
        return out

    def maybe_extract(self) -> dict | None:
        """一轮成功结束后提取：只在 done、至少 3 步、这一轮没自己 remember 过、还没提取过、后面没有新输入时。"""
        if self.extract_memory is None or not extract.enabled():
            return None
        state = self._fold()
        r = state.run
        if (r is None or r.active or r.status != "done" or r.steps < MIN_EXTRACT_STEPS or state.new_inputs()
                or state.unjudged):
            return None
        events = self._load()
        if any(e["type"] == "ActionCompleted" and e.get("kind") == "extract" and e.get("run_id") == r.id
               and (e.get("output") or {}).get("reason") == "task_end" for e in events):
            return None
        if extract.remembered_in_run(events, r.id):
            return None
        return self.extract("task_end", r.id)

    def ask_up(self, wait: k.Wait, info: dict, poll: float = 0.2, timeout: float | None = None):
        """子 Agent 要人回答（审批、卡住了）：在这个（父）任务里记一件等待，阻塞到被回答。
        父任务的工作线程此时正卡在 task 这个工具调用里，所以不冲突。父任务被取消就当拒绝。"""
        state = self._fold()
        run_id = state.run.id if state.run and state.run.active else None
        if run_id is None:
            return {"allow": False, "note": "主任务已经结束"}
        p = wait.payload or {}
        payload = {"sub": info.get("agent", "子 Agent"), "sub_kind": wait.kind, "sub_session": info.get("session"),
                   "parent_call": info.get("call_id"), "reason": p.get("reason", "")}
        if "call_id" in p:
            payload["call"] = {"id": p["call_id"], "name": p.get("name"), "args": p.get("args") or {}}
        wait_id = new_id()
        start = len(self._load())                   # 只看这之后的（先记位置再记等待：回答来得再快也不会漏）
        self._append([self._event("WaitStarted", run_id=run_id, wait_id=wait_id, kind=wait.kind, payload=payload)])
        deadline = (time.monotonic() + timeout) if timeout else None
        while deadline is None or time.monotonic() < deadline:
            events = self._load()
            for e in events[start:]:
                if e["type"] == "WaitResolved" and e.get("wait_id") == wait_id:
                    return e.get("value")
                if e["type"] == "Cancelled" or (e["type"] == "RunFinished" and e.get("run_id") == run_id):
                    return {"allow": False, "note": "主任务被取消"}
            start = len(events)
            time.sleep(poll)
        return {"allow": False, "note": "等太久没人回答"}

    def resolve(self, wait_id: str, value) -> None:
        self._append([self._event("WaitResolved", wait_id=wait_id, value=value)])

    # ------------------------------------------------ 主循环

    def run(self, max_iterations: int = 10_000) -> k.State:
        for fn in self.before_run:
            fn()
        for _ in range(max_iterations):
            state = self._fold()
            actions = k.decide(state, self.policy)
            if not actions or isinstance(actions[0], k.Block):
                if self.approver and state.open_waits:
                    for w in state.open_waits:
                        self.resolve(w.id, self.approver(w))
                    continue
                return state
            self._do_all(actions, state)
        raise RuntimeError("runner 迭代次数超限，可能出现了死循环")

    # ------------------------------------------------ 执行单个动作

    def _do(self, a, state: k.State) -> None:
        run_id = state.run.id if state.run and state.run.active else None
        ev = lambda t, **f: self._event(t, run_id=run_id, **f)

        if isinstance(a, k.Judge):
            self._append([ev("InputJudged", input_id=a.input_id, verdict=a.verdict,
                             reason=a.reason, scope=a.scope)])
        elif isinstance(a, k.StartRun):
            self._append([self._event("RunStarted", run_id=new_id(), scope=a.scope)])
        elif isinstance(a, k.StartWait):
            self._append([ev("WaitStarted", wait_id=new_id(), kind=a.kind, payload=a.payload)])
        elif isinstance(a, k.AddInput):
            self._append([ev("InputReceived", content=[{"type": "text", "text": a.text}],
                             source=a.source, author=None, **a.meta)])
        elif isinstance(a, k.Synthesize):
            self._append([ev("ActionCompleted", action_id=a.call["id"], kind="tool",
                             output=a.message, is_error=a.is_error, synthetic=True)])
        elif isinstance(a, k.Finish):
            self._append([ev("RunFinished", status=a.status, text=a.text)])
        elif isinstance(a, k.CallModel):
            self._call_model(a, ev)
        elif isinstance(a, k.Compact):
            self._compact(a, ev, state)
        elif isinstance(a, k.Execute):
            self._execute(a, ev, state)
        else:
            raise TypeError(f"未知动作：{a!r}")

    def _call_model(self, a: k.CallModel, ev) -> None:
        action_id = a.retry_of or new_id()
        tool_choice = "none" if a.wrap_up else "auto"
        self._append([ev("ActionStarted", action_id=action_id, kind="model",
                         input={"model": getattr(self.model, "name", "?"), "wrap_up": a.wrap_up,
                                "tool_choice": tool_choice})])
        messages = mark(self.prefix_messages + project(self._load()))
        try:
            reply = self.model.create(self.system, self.tools.specs, messages, tool_choice=tool_choice,
                                      on_delta=self.on_delta, cache_key=self.session_id)
            out, is_error = reply, False
        except Exception as e:  # 重试已在适配器里做过，这里只把失败记成结果
            out, is_error = {"content": [], "stop": "other", "usage": {},
                             "error": self.redactor.redact(f"{type(e).__name__}: {e}")[0],
                             "error_kind": getattr(e, "kind", "")}, True
        self._append([ev("ActionCompleted", action_id=action_id, kind="model",
                         output=out, is_error=is_error)])

    def _compact(self, a: k.Compact, ev, state: k.State) -> None:
        action_id = a.retry_of or new_id()
        self._append([ev("ActionStarted", action_id=action_id, kind="compact", input={"reason": a.reason})])
        if self.on_delta:
            self.on_delta({"type": "compact_start", "reason": a.reason})
        try:
            run_id = state.run.id if state.run and state.run.active else None
            before = (lambda: self.extract("compact", run_id)) if self.extract_memory is not None else None
            out = self.compactor.compact(self._load(), self.model, self.system, self.tools.specs,
                                         state.context_tokens, self.policy.compact_threshold, a.reason,
                                         before_summary=before)
            is_error = False
        except Exception as e:  # 压缩本身出错：记下来，本轮不压，照常继续
            out, is_error = {"reason": a.reason, "mode": "none", "error": f"{type(e).__name__}: {e}"}, True
        self._append([ev("ActionCompleted", action_id=action_id, kind="compact", output=out, is_error=is_error)])

    # ------------------------------------------------ 执行工具（相邻的可并行调用凑成一批一起跑）

    def _safe(self, a) -> bool:
        check = getattr(self.tools, "concurrency_safe", None)
        return isinstance(a, k.Execute) and check is not None and check(a.call["name"], a.call.get("args") or {})

    def _do_all(self, actions: list, state: k.State) -> None:
        """按顺序执行；相邻的、能并行的 Execute 凑成一批并发（Claude Code 的分批法），其余逐个来。"""
        i = 0
        while i < len(actions):
            j = i + 1
            if self._safe(actions[i]):
                while j < len(actions) and self._safe(actions[j]):
                    j += 1
            if j - i > 1:
                self._execute_batch(actions[i:j], state)
            else:
                self._do(actions[i], state)
            i = j

    def _tool_event(self, state: k.State):
        run_id = state.run.id if state.run and state.run.active else None
        return lambda t, **f: self._event(t, run_id=run_id, **f)

    def _run_tool(self, call: dict, scope) -> tuple[str, bool, dict]:
        try:
            try:
                r = self.tools.execute(call["name"], call["args"], scope, call_id=call["id"])
            except TypeError as e:               # 不认 call_id 的工具箱（比如测试里的假工具）
                if "call_id" not in str(e):
                    raise
                r = self.tools.execute(call["name"], call["args"], scope)
        except Exception as e:  # 工具出错也是结果，交给模型换做法
            return f"{type(e).__name__}: {e}", True, {}
        return (r[0], r[1], r[2]) if len(r) == 3 else (r[0], r[1], {})

    def _completed(self, ev, call: dict, result: tuple[str, bool, dict]) -> dict:
        output, is_error, meta = result
        if isinstance(output, str):
            output, hits = self.redactor.redact(output)
            if hits:
                output += rd.note(hits)
        if call.get("edited_from") is not None and isinstance(output, str):   # 模型看到的是它原来的调用，这里说清实际跑的是什么
            output = f"[用户把参数改成了：{json.dumps(call['args'], ensure_ascii=False)}]\n{output}"
        extra = {"usage": meta["usage"]} if meta.get("usage") else {}
        return ev("ActionCompleted", action_id=call["id"], kind="tool", output=output, is_error=is_error, **extra)

    def _execute(self, a: k.Execute, ev, state: k.State) -> None:
        call = a.call
        self._append([ev("ActionStarted", action_id=call["id"], kind="tool", input=call)])
        scope = state.run.scope if state.run else None
        self._append([self._completed(ev, call, self._run_tool(call, scope))])

    def _execute_batch(self, batch: list, state: k.State) -> None:
        """先把这批的 ActionStarted 都记下，再并发执行；谁先完成先记谁（投影时再按调用顺序排）。"""
        ev = self._tool_event(state)
        scope = state.run.scope if state.run else None
        calls = [a.call for a in batch]
        self._append([ev("ActionStarted", action_id=c["id"], kind="tool", input=c) for c in calls])
        with ThreadPoolExecutor(max_workers=min(len(calls), self.max_parallel)) as pool:
            futures = {pool.submit(self._run_tool, c, scope): c for c in calls}
            for f in as_completed(futures):
                self._append([self._completed(ev, futures[f], f.result())])

    # ------------------------------------------------ 账本读写

    def _fold(self) -> k.State:
        self._state = k.fold(self._load(), self._state)
        return self._state

    def _load(self) -> list[dict]:
        return self.store.load(self.session_id)

    def _event(self, type_: str, run_id: str | None = None, **fields) -> dict:
        return {"type": type_, "id": new_id(), "ts": time.time(), "session_id": self.session_id,
                "run_id": run_id, "parent_id": self.parent_id, **fields}

    def _append(self, events: list[dict]) -> None:
        with self._lock:
            seq = len(self._load())
            self.store.append(self.session_id, events, expected_seq=seq)
            for e in events:
                self.sink({**e, "seq": seq + 1})
                seq += 1
