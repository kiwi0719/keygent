"""子 Agent：task 工具。见 design/parallel-subagent.md 第二部分。

子 Agent 就是递归跑同一个内核：新建一个执行者，自己一本账本，全新上下文（环境信息 + 用户原话摘录 + 任务），
不加载记忆，没有 task 工具（深度 1）。explore 只读、可以并行；general 能改文件、独占执行。
最终回答作为 task 的工具结果交回，用量计入父任务预算。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .compaction import Compactor
from .environment import Environment
from .permissions import PermissionPolicy, is_readonly_command
from .kernel import Deny
from .project import project
from .runner import Runner, new_id
from .tools import Tool, ToolBox, _spec

MODES = ("explore", "general")
MAX_REPORT = 8_000              # 交回主 Agent 的汇报最多多少字（要求是 2000 字以内，这里是兜底）
QUOTE_CHARS = 2_000
WARM_TTL = 240                  # 预热过的前缀多久内算“还在缓存里”（Anthropic 默认 5 分钟，留点余量）
WARM_WAIT = 20                  # 跟随者最多等领头的多久

SUB_SYSTEM = {
    "explore": ("你是主 Agent 派出的只读子 Agent，负责查资料、读代码、定位问题。你看不到主对话，只有下面的任务说明。"
                "找东西先用 grep / find_files 定位，再用 read_file 只读相关的几行；互不依赖的查找可以一轮里同时发起。"
                "你没有改文件的工具，bash 也只能跑只读命令；如果发现需要修改，写在汇报里，由主 Agent 决定。"
                "不要问用户问题。完成后用一段话汇报，控制在 2000 字以内：先写结论，再列证据（文件:行号），最后写不确定的地方。"),
    "general": ("你是主 Agent 派出的子 Agent，负责独立完成一个子任务，可以改文件、跑命令。你看不到主对话，只有下面的任务说明。"
                "找东西先用 grep / find_files 定位，再用 read_file 读相关的几行；改代码用 edit_file（改之前先读），"
                "改完用 bash 跑测试或运行来验证。被拒绝或被沙箱拦下的操作不要硬来，写进汇报。"
                "不要问用户问题。完成后用一段话汇报，控制在 2000 字以内：做了什么、改了哪些文件、验证结果、还剩什么问题。"),
}

MAX_BACKGROUND = 3              # 每个任务同时最多几个后台子 Agent
TYPE_FOOTER = ("\n\n---\n你是主 Agent 派出的子 Agent，看不到主对话，只有下面的任务说明。不要问用户问题。"
               "完成后用一段话汇报，控制在 2000 字以内：先写结论，再列证据或做了什么，最后写不确定的地方。")
FORK_DENIED = ("task", "remember", "forget", "todo_write", "ask_user")


class ForkBox:
    """分叉的子 Agent 用主 Agent 的工具箱：工具清单一字不差（前缀才能和主对话相同），不允许的在执行时拒绝。"""

    def __init__(self, box):
        self.box = box

    def __getattr__(self, name):
        return getattr(self.box, name)

    def execute(self, name, args, scope=None, call_id=None):
        if name in FORK_DENIED:
            return f"分叉的子 Agent 不能用 {name}，需要的话写进汇报，由主 Agent 处理。", True
        return self.box.execute(name, args, scope, call_id=call_id)


def _progress(child) -> str:
    """后台子 Agent 的进度：走了几步、最后在做什么。"""
    from .daemon.humanize import steps
    events = child.store.load(child.session_id)
    st = [x for x in steps(events) if x["kind"] == "step"]
    state = child._state
    n = state.run.steps if state and state.run else 0
    return f"已经走了 {n} 步" + (f"，最近一步：{st[-1]['title']}" if st else "")


GUIDE = """
## 子 Agent（task 工具）
需要大量搜索、读很多文件才能回答的问题，用 task 派子 Agent 去查，它在自己的上下文里查完只交回结论，能省下你的上下文。
- 只是查资料、读代码、找原因：mode=explore（只读，几个可以一次并行派出）。
- 要改文件、跑命令完成一个独立子任务：mode=general（一次只跑一个）。
- 子 Agent 看不到这段对话：prompt 里写清背景、要查或要做什么、要交回什么。
- 一两次查找就能搞定的事自己做，别派。
"""


class ExplorePolicy(PermissionPolicy):
    """explore 子 Agent：bash 只准只读命令，写命令直接拒绝（不问人）。它本来就没有写文件的工具。"""

    def wait_for(self, call, state):
        if call["name"] == "bash" and not is_readonly_command((call.get("args") or {}).get("command", "")):
            return Deny("explore 子 Agent 是只读的，不能执行会改动东西的命令；需要的话写进汇报，由主 Agent 处理")
        return super().wait_for(call, state)


class WarmGate:
    """同一种模式的子 Agent 前缀 [工具][system][环境] 逐字节相同。同时派出时，让第一个（领头的）先发请求，
    其余的（跟随的）等它收到第一个流式片段——那时服务端已经处理完输入、写好了缓存——再发，第一轮就能命中缓存。
    领头的出错也会放行；最多等 WARM_WAIT 秒；超过 WARM_TTL 没预热过就重新选领头的。"""

    def __init__(self, ttl: float = WARM_TTL, wait: float = WARM_WAIT, clock=time.monotonic):
        self.ttl, self.wait, self.clock = ttl, wait, clock
        self.lock = threading.Lock()
        self.gates: dict[str, tuple[threading.Event, list]] = {}    # 模式 → (放行信号, [放行时间])

    def enter(self, mode: str) -> tuple[bool, threading.Event, list]:
        with self.lock:
            gate = self.gates.get(mode)
            fresh = gate and (not gate[0].is_set() or self.clock() - gate[1][0] < self.ttl)
            if fresh:
                return False, gate[0], gate[1]
            gate = (threading.Event(), [0.0])
            self.gates[mode] = gate
            return True, gate[0], gate[1]

    def open(self, event: threading.Event, stamp: list) -> None:
        if not event.is_set():
            stamp[0] = self.clock()
            event.set()


class LeaderModel:
    """包住模型：第一次请求收到第一个流式片段（或请求结束）时放行跟随者。"""

    def __init__(self, model, release: Callable[[], None]):
        self.model, self.release = model, release
        self.name = getattr(model, "name", "?")

    def create(self, system, tools, messages, tool_choice="auto", on_delta=None, cache_key=None):
        def first(delta):
            self.release()
            if on_delta:
                on_delta(delta)
        try:
            return self.model.create(system, tools, messages, tool_choice=tool_choice, on_delta=first,
                                     cache_key=cache_key)
        finally:
            self.release()


class SubAgents:
    def __init__(self, *, session: str, store, model, root, sandbox, blobs, yes: bool = False,
                 approver: Callable | None = None, display: Callable[[str, dict], None] | None = None,
                 max_steps: int = 30, token_budget: int = 200_000, context_window: int = 128_000,
                 max_output: int = 8192, compact_at: int | None = None, max_parallel: int = 4, skills=None,
                 mcp=None, approver_info: bool = False, agent_types=None, model_for: Callable | None = None,
                 jobs=None, fork_source: dict | None = None):
        """approver_info=True：approver(wait, info) 多收一个 info（哪个子 Agent、父任务里哪次 task 调用），
        并且不排队——常驻服务把审批升到父任务的“等你的事”，几个子 Agent 可以同时在等。"""
        self.session, self.store, self.model = session, store, model
        self.root, self.sandbox, self.blobs, self.yes = root, sandbox, blobs, yes
        self.display = display or (lambda desc, ev: None)
        self.limits = dict(max_steps=max_steps, token_budget=token_budget, context_window=context_window,
                           max_output=max_output, compact_at=compact_at)
        self.slots = threading.Semaphore(max_parallel)        # 子 Agent 同时最多几个
        # 子 Agent 也能用 skills（列表 + skill 工具），但不给只属于主 Agent 的流程类（要问人、要派子 Agent）
        self.skills = skills.for_subagent() if skills is not None else None
        self.mcp = mcp                                        # MCP 工具：explore 只给只读的
        self.warm = WarmGate()
        ask_lock = threading.Lock()                           # 并行的子 Agent 同时要问人时，排队问

        def approve(wait):
            with ask_lock:
                return approver(wait)
        self.approver = approve if approver else None
        self._raw_approver, self.approver_info = approver, approver_info
        self.agent_types = agent_types                       # 自定义子 Agent 类型（weaver/agents.py）
        self.model_for = model_for                           # 自定义类型指定了模型：按名字造一个
        self.jobs = jobs                                     # 后台子 Agent 用后台任务登记表（weaver/jobs.py）
        self.fork_source = fork_source                       # 分叉用：主 Agent 的 {model, system, tools}
        self.active: set[Runner] = set()                     # 正在跑的子执行者：父任务取消时一起取消
        self._active_lock = threading.Lock()

    def cancel_all(self, reason: str = "主任务被取消") -> int:
        with self._active_lock:
            children = list(self.active)
        for c in children:
            c.cancel(reason)
        return len(children)

    # ------------------------------------------------ 组装子 Agent

    def toolbox(self, mode: str) -> ToolBox:
        box = ToolBox(self.root).add_write_tools(self.session, self.blobs, self.sandbox)   # 撤销记在父会话里
        if mode == "explore":
            for name in ("write_file", "edit_file"):
                box.tools.pop(name)
        if self.skills is not None:
            box.tools["skill"] = self.skills.tool()
        if self.mcp is not None:
            box.tools.update(self.mcp.tools(readonly_only=mode == "explore"))
        return box

    def user_quotes(self) -> str:
        return Compactor(quote_chars=QUOTE_CHARS).user_quotes(self.store.load(self.session), 10 ** 12)

    def brief(self, prompt: str) -> str:
        quotes = self.user_quotes()
        parts = []
        if quotes:
            parts.append("## 背景：用户最近的原话（原样摘录，从新到旧；仅供理解意图，任务以下面为准）\n" + quotes)
        parts.append("## 任务\n" + prompt.strip())
        return "\n\n".join(parts)

    def run(self, description: str, prompt: str, mode: str = "explore", agent: str = "", background: bool = False,
            context: str = "fresh", _call_id: str | None = None):
        if mode not in MODES:
            raise ValueError(f"mode 只能是 {' / '.join(MODES)}")
        if context not in ("fresh", "fork"):
            raise ValueError("context 只能是 fresh 或 fork")
        atype = None
        if agent:
            atype = (self.agent_types.available() if self.agent_types else {}).get(agent)
            if atype is None:
                names = sorted(self.agent_types.available()) if self.agent_types else []
                raise ValueError(f"没有名为 {agent} 的子 Agent 类型" + (f"，可用：{'、'.join(names)}" if names else ""))
        readonly = atype.readonly if atype else mode == "explore"
        if context == "fork":
            if atype or mode != "general":
                raise ValueError("分叉（context=fork）只用于 mode=general，不能配自定义类型")
            if background:
                raise ValueError("分叉的子 Agent 会改文件，不能放后台")
            if self.fork_source is None:
                raise ValueError("这里不支持分叉")
        if background:
            if not readonly:
                raise ValueError("后台只能派只读的子 Agent（mode=explore，或工具全是只读的自定义类型）；"
                                 "会改文件的放后台会和你同时改同一批文件")
            if self.jobs is None:
                raise ValueError("这里不支持后台子 Agent")
        child, label, gate_info = self._child(description, mode, atype, context, _call_id)
        if background:
            return self.jobs.start_agent(
                description, lambda: self._drive(child, prompt, gate_info, label),
                cancel=lambda: child.cancel("后台子 Agent 被结束"), progress=lambda: _progress(child),
                limit=MAX_BACKGROUND)
        return self._drive(child, prompt, gate_info, label)

    def _child(self, description: str, mode: str, atype, context: str, call_id):
        child_id = f"{self.session}--{new_id()}"
        sink = lambda ev: self.display(description, ev)
        approver = self.approver
        if self.approver_info and self._raw_approver:
            info = {"agent": description, "mode": mode, "call_id": call_id, "session": child_id}
            approver = lambda w: self._raw_approver(w, info)
        common = dict(approver=approver, sink=sink, parent_id=f"{self.session}:{call_id}", max_parallel=8)
        if context == "fork":                       # 前缀和主对话逐字节相同：同样的 system、工具清单、对话
            src = self.fork_source
            events = self.store.load(self.session)
            upto = next((e["seq"] for e in events if e["type"] == "ActionCompleted" and e.get("kind") == "model"
                         and any(p.get("id") == call_id for p in (e.get("output") or {}).get("content") or [])), 0) - 1
            if upto <= 0:
                raise ValueError("找不到发出这次 task 调用的那条回复，没法分叉")
            prefix = project([e for e in events if e["seq"] <= upto])
            policy = PermissionPolicy(self.root, yes=self.yes, mcp=self.mcp, **self.limits)
            child = Runner(child_id, self.store, src["model"], ForkBox(src["tools"]), policy, src["system"],
                           prefix_messages=prefix, **common)
            child._append([child._event("InputReceived", content=[{"type": "text", "text": "（从主对话分叉）"}],
                                        source="context", author=None, context_kind="fork",
                                        parent_session=self.session, upto=upto)])
            return child, f"{description}」（分叉）", None
        if atype:
            policy_cls = ExplorePolicy if atype.readonly else PermissionPolicy
            box = self.toolbox("explore" if atype.readonly else "general")
            if atype.tools is not None:
                box.tools = {n: t for n, t in box.tools.items() if n in atype.tools}
            model = self.model_for(atype.model) if atype.model and self.model_for else self.model
            system = atype.prompt + TYPE_FOOTER
            warm_key = f"agent:{atype.name}"
            label = f"{description}」（{atype.name}）"
        else:
            policy_cls = ExplorePolicy if mode == "explore" else PermissionPolicy
            box, model, system, warm_key, label = self.toolbox(mode), self.model, SUB_SYSTEM[mode], mode, \
                f"{description}」（{mode}）"
        policy = policy_cls(self.root, yes=self.yes, mcp=self.mcp, **self.limits)
        leader, gate, stamp = self.warm.enter(warm_key)
        if leader:
            model = LeaderModel(model, lambda: self.warm.open(gate, stamp))
        child = Runner(child_id, self.store, model, box, policy, system,
                       context=[Environment(self.root, self.sandbox)] + ([self.skills] if self.skills else []),
                       **common)
        return child, label, (leader, gate)

    def _drive(self, child: Runner, prompt: str, gate_info, label: str):
        """跑子执行者到结束，返回 (汇报, 附加信息)。"""
        with self.slots:
            with self._active_lock:
                self.active.add(child)
            try:
                child.submit(self.fork_brief(prompt) if gate_info is None else self.brief(prompt))
                if gate_info and not gate_info[0]:
                    gate_info[1].wait(self.warm.wait)   # 等领头的把共同前缀写进缓存
                state = child.run()
            finally:
                with self._active_lock:
                    self.active.discard(child)
        run = state.run
        status = run.status if run else "error"
        finished = [e for e in self.store.load(child.session_id) if e["type"] == "RunFinished"]
        text = (finished[-1].get("text") if finished else "") or "（子 Agent 没有给出汇报）"
        if len(text) > MAX_REPORT:
            text = text[:MAX_REPORT] + f"\n…[汇报超过 {MAX_REPORT} 字，已截断；完整内容见子账本]"
        steps, tokens = (run.steps, run.tokens) if run else (0, 0)
        footer = f"[子 Agent「{label}：{status}，{steps} 步，{tokens} token，子账本 {child.session_id}]"
        return f"{text}\n\n{footer}", {"is_error": status != "done", "usage": {"input_tokens": tokens}}

    @staticmethod
    def fork_brief(prompt: str) -> str:
        return ("你是从上面这段主对话分叉出来的子 Agent，看得到上面的全部内容，主对话里已经做过的决定照样算数。"
                "现在只做下面这件事，做完用一段话汇报（2000 字以内）：做了什么、改了哪些文件、验证结果、还剩什么问题。"
                "不能再派子 Agent，不能写记忆，不能改任务清单（这些工具会被拒绝）。不要问用户问题。\n\n## 任务\n"
                + prompt.strip())

    def tool(self) -> Tool:
        def parallel(args):
            if args.get("background"):
                return True                         # 立刻返回，不占时间
            if args.get("agent"):
                t = (self.agent_types.available() if self.agent_types else {}).get(args["agent"])
                return bool(t and t.readonly)
            return args.get("mode", "explore") == "explore" and args.get("context", "fresh") != "fork"
        return Tool(_spec(
            "task",
            "派一个子 Agent 去完成子任务，它在自己的上下文里做完，只把结论交回来。"
            "mode=explore：只读，查资料、读代码、找原因，几个可以同时派出并行；"
            "mode=general：能改文件、跑命令，完成一个独立的子任务，一次只跑一个。"
            "只是查资料一律用 explore。子 Agent 看不到这段对话，prompt 要写全：背景、要查或要做什么、要交回什么。"
            "agent：用自定义的子 Agent 类型（可用的类型列在背景信息里）。"
            "background=true：放到后台跑，立刻返回任务 id，做完会通知你，需要结果时用 job_wait 等（只能派只读的）。"
            "context=fork：分叉，子 Agent 看得到这段对话的全部内容（只用于 general，改代码需要知道前情时用）。",
            {"description": {"type": "string", "description": "3~5 个字的任务名，显示用"},
             "prompt": {"type": "string", "description": "完整的任务说明"},
             "mode": {"type": "string", "enum": list(MODES), "description": "explore（默认，只读）或 general（能改东西）"},
             "agent": {"type": "string", "description": "自定义子 Agent 类型的名字（可选）"},
             "background": {"type": "boolean", "description": "放到后台跑（只能是只读的子 Agent）"},
             "context": {"type": "string", "enum": ["fresh", "fork"],
                         "description": "fresh（默认）：全新上下文；fork：继承这段对话（只用于 general）"}},
            ["description", "prompt"]),
            self.run, readonly=False, wants_call_id=True, parallel=parallel)
