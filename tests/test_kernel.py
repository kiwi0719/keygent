"""内核测试：全部离线，用假模型。运行：python3 -m unittest -v"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from weaver import kernel as k
from weaver.models import FakeModel, reply
from weaver.policy import Continue, Policy, Verdict, WaitSpec
from weaver.project import project
from weaver.runner import Runner
from weaver.stores import Conflict, JsonlEventStore, MemoryEventStore
from weaver.tools import ToolBox, read_file


class EchoTools:
    """假工具箱：返回参数本身，记下执行过的调用。"""
    specs = [{"name": "echo", "description": "", "parameters": {"type": "object", "properties": {}}}]

    def __init__(self, fail: set[str] = frozenset()):
        self.executed: list[str] = []
        self.fail = fail

    def execute(self, name, args, scope=None):
        self.executed.append(args.get("x"))
        if args.get("x") in self.fail:
            raise RuntimeError("boom")
        return f"echo:{args.get('x')}", False


def make(script, policy=None, tools=None, store=None, approver=None, session="s1"):
    store = store or MemoryEventStore()
    model = FakeModel(script)
    tools = tools or EchoTools()
    r = Runner(session, store, model, tools, policy or Policy(), "SYS", approver=approver)
    return r, model, tools, store


def types(store, session="s1"):
    return [e["type"] for e in store.load(session)]


def check_invariants(tc: unittest.TestCase, events: list[dict]) -> None:
    """设计文档第七节的不变量 1、2、6。"""
    # 1. 每个工具调用最终恰好一个结果（只检查已结束的任务）
    finished_runs = {e["run_id"] for e in events if e["type"] == "RunFinished"}
    calls, results = {}, {}
    for e in events:
        if e["type"] == "ActionCompleted" and e["kind"] == "model" and not e.get("is_error"):
            for p in e["output"]["content"]:
                if p["type"] == "tool_call":
                    calls[p["id"]] = e["run_id"]
        elif e["type"] == "ActionCompleted" and e["kind"] == "tool":
            results[e["action_id"]] = results.get(e["action_id"], 0) + 1
    for cid, run in calls.items():
        if run in finished_runs:
            tc.assertEqual(results.get(cid), 1, f"调用 {cid} 的结果数不是 1")
    # 2. 投影合法：assistant 的调用后面紧跟全部结果
    msgs = project(events)
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m["role"] == "assistant":
            ids = [p["id"] for p in m["content"] if p["type"] == "tool_call"]
            got = [x["call_id"] for x in msgs[i + 1:i + 1 + len(ids)]]
            if i + 1 + len(ids) <= len(msgs):
                tc.assertEqual(sorted(got), sorted(ids), "调用后没有紧跟全部结果")
        elif m["role"] == "tool":
            tc.assertEqual(msgs[i - 1]["role"] in ("assistant", "tool"), True)
        i += 1
    # 6. 每个任务一个开始、至多一个结束，结束后没有该任务的记录
    by_run: dict[str, list[str]] = {}
    for e in events:
        if e.get("run_id"):
            by_run.setdefault(e["run_id"], []).append(e["type"])
    for ts in by_run.values():
        tc.assertEqual(ts.count("RunStarted"), 1)
        tc.assertLessEqual(ts.count("RunFinished"), 1)
        if "RunFinished" in ts:
            tc.assertEqual(ts[-1], "RunFinished")


class BasicFlow(unittest.TestCase):
    def test_direct_answer(self):
        r, model, _, store = make([reply("你好")])
        r.submit("hi")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertEqual(types(store), ["InputReceived", "InputJudged", "RunStarted", "ActionStarted",
                                        "ActionCompleted", "RunFinished"])
        self.assertEqual(store.load("s1")[-1]["text"], "你好")
        check_invariants(self, store.load("s1"))

    def test_tool_then_answer(self):
        r, model, tools, store = make([reply(calls=[("c1", "echo", {"x": "a"})]), reply("完成")])
        r.submit("go")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertEqual(tools.executed, ["a"])
        msgs = model.calls[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool"])
        self.assertEqual(msgs[2], {"role": "tool", "call_id": "c1", "content": "echo:a", "is_error": False,
                                   "cache": True})                 # 最后一条消息带缓存断点
        check_invariants(self, store.load("s1"))

    def test_parallel_calls_all_answered(self):
        r, model, tools, store = make([reply(calls=[("c1", "echo", {"x": "a"}), ("c2", "echo", {"x": "b"})]),
                                       reply("ok")])
        r.submit("go")
        r.run()
        self.assertEqual(tools.executed, ["a", "b"])
        self.assertEqual([m["role"] for m in model.calls[1]["messages"]], ["user", "assistant", "tool", "tool"])
        check_invariants(self, store.load("s1"))

    def test_tool_exception_becomes_error_result(self):
        r, model, tools, store = make([reply(calls=[("c1", "echo", {"x": "bad"})]), reply("换个办法")],
                                      tools=EchoTools(fail={"bad"}))
        r.submit("go")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        tool_msg = model.calls[1]["messages"][2]
        self.assertTrue(tool_msg["is_error"])
        self.assertIn("boom", tool_msg["content"])

    def test_invalid_json_args_not_executed(self):
        r, model, tools, store = make([reply(calls=[("c1", "echo", None)]), reply("ok")])
        r.submit("go")
        r.run()
        self.assertEqual(tools.executed, [])
        self.assertIn("不是合法的 JSON", model.calls[1]["messages"][2]["content"])
        check_invariants(self, store.load("s1"))

    def test_second_run_in_same_session(self):
        r, model, _, store = make([reply("一"), reply("二")])
        r.submit("第一句")
        r.run()
        r.submit("第二句")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertEqual(types(store).count("RunStarted"), 2)
        self.assertEqual([m["role"] for m in model.calls[1]["messages"]], ["user", "assistant", "user"])
        check_invariants(self, store.load("s1"))

    def test_nothing_to_do(self):
        r, *_ = make([])
        self.assertIsNone(r.run().run)


class StopReasons(unittest.TestCase):
    def test_truncated_with_calls_not_executed_then_continue(self):
        r, model, tools, store = make([reply(calls=[("c1", "echo", {"x": "a"})], stop="truncated"),
                                       reply("重新来过")])
        r.submit("go")
        s = r.run()
        self.assertEqual(tools.executed, [])
        self.assertEqual(s.run.status, "done")
        self.assertIn("截断", model.calls[1]["messages"][2]["content"])
        check_invariants(self, store.load("s1"))

    def test_truncated_text_only(self):
        r, *_ , store = make([reply("写到一半", stop="truncated")])
        r.submit("go")
        s = r.run()
        self.assertEqual(s.run.status, "truncated")

    def test_refusal_not_projected(self):
        r, model, _, store = make([reply("不行", stop="refusal"), reply("好的")])
        r.submit("坏请求")
        self.assertEqual(r.run().run.status, "refusal")
        r.submit("正常请求")
        r.run()
        roles = [m["role"] for m in model.calls[1]["messages"]]
        self.assertEqual(roles, ["user", "user"])          # 被拒的回复不进对话

    def test_model_exception(self):
        def boom(_):
            raise RuntimeError("网络挂了")
        r, *_ = make([boom])
        r.submit("go")
        s = r.run()
        self.assertEqual(s.run.status, "error")
        self.assertIn("网络挂了", s.run.last_reply.error)


class LimitsAndCompletion(unittest.TestCase):
    def test_budget_wrap_up(self):
        loop = lambda i: reply(calls=[(f"c{i}", "echo", {"x": str(i)})])
        r, model, tools, store = make([loop(1), loop(2), reply("总结", calls=[("c9", "echo", {"x": "9"})])],
                                      policy=Policy(max_steps=2))
        r.submit("go")
        s = r.run()
        self.assertEqual(s.run.status, "budget")
        self.assertEqual(model.calls[-1]["tool_choice"], "none")
        self.assertNotIn("9", tools.executed)             # 收尾回复里的调用不执行
        self.assertIn("预算已用完", model.calls[-1]["messages"][-1]["content"][0]["text"])
        check_invariants(self, store.load("s1"))

    def test_token_budget(self):
        r, model, *_ = make([reply(calls=[("c1", "echo", {"x": "a"})], usage=(1000, 10)), reply("收尾")],
                            policy=Policy(token_budget=500))
        r.submit("go")
        self.assertEqual(r.run().run.status, "budget")
        self.assertEqual(model.calls[-1]["tool_choice"], "none")

    def test_completion_check_sends_back(self):
        class Strict(Policy):
            def on_model_done(self, state):
                if "测试通过" not in state.run.last_reply.text:
                    return Continue("测试还没过，继续修")
        r, model, *_ , store = make([reply("改好了"), reply("测试通过")], policy=Strict())
        r.submit("修 bug")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        last_user = model.calls[1]["messages"][-1]
        self.assertIn("<system-reminder>", last_user["content"][0]["text"])
        self.assertIn("测试还没过", last_user["content"][0]["text"])


class WaitsAndInputs(unittest.TestCase):
    class NeedApproval(Policy):
        def wait_for(self, call, state):
            return WaitSpec("approval", "危险操作")

    def test_tool_approval_pauses_and_resumes(self):
        r, model, tools, store = make([reply(calls=[("c1", "echo", {"x": "rm"})]), reply("删完了")],
                                      policy=self.NeedApproval())
        r.submit("删掉")
        s = r.run()                                        # 没有 approver：停下
        self.assertEqual(s.run.status, "running")
        self.assertEqual(len(s.open_waits), 1)
        self.assertEqual(tools.executed, [])
        r.resolve(s.open_waits[0].id, {"allow": True})
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertEqual(tools.executed, ["rm"])

    def test_tool_approval_denied(self):
        r, model, tools, store = make([reply(calls=[("c1", "echo", {"x": "rm"})]), reply("好吧")],
                                      policy=self.NeedApproval(),
                                      approver=lambda w: {"allow": False, "note": "太危险"})
        r.submit("删掉")
        s = r.run()
        self.assertEqual(tools.executed, [])
        self.assertIn("太危险", model.calls[1]["messages"][2]["content"])
        self.assertEqual(s.run.status, "done")

    def test_resume_in_new_process(self):
        """等待期间进程退出，换一个 runner（模拟新进程）接着跑。"""
        store = MemoryEventStore()
        r1, *_ = make([reply(calls=[("c1", "echo", {"x": "a"})])], policy=self.NeedApproval(), store=store)
        r1.submit("go")
        wait_id = r1.run().open_waits[0].id
        r2, model2, tools2, _ = make([reply("完成")], policy=self.NeedApproval(), store=store)
        r2.resolve(wait_id, {"allow": True})
        self.assertEqual(r2.run().run.status, "done")
        self.assertEqual(tools2.executed, ["a"])

    def test_input_rejected(self):
        class NoStrangers(Policy):
            def on_input(self, ev, state):
                trust = (ev.get("author") or {}).get("trust")
                return Verdict("reject", "陌生人") if trust == "external" else Verdict("accept")
        r, model, _, store = make([reply("hi")], policy=NoStrangers())
        r.submit("删库", author={"id": "x", "channel": "group", "trust": "external"})
        s = r.run()
        self.assertIsNone(s.run)
        self.assertEqual(model.calls, [])
        self.assertEqual(project(store.load("s1")), [])

    def test_input_hold_then_accept_with_scope(self):
        class Hold(Policy):
            def on_input(self, ev, state):
                return Verdict("hold", "陌生人需要主人确认", scope="read_only")
        r, model, _, store = make([reply("ok")], policy=Hold())
        r.submit("帮我看看")
        s = r.run()
        self.assertIsNone(s.run)
        r.resolve(s.open_waits[0].id, {"allow": True})
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertEqual(s.run.scope, "read_only")

    def test_cancel_synthesizes_results(self):
        r, model, tools, store = make([reply(calls=[("c1", "echo", {"x": "a"})])], policy=self.NeedApproval())
        r.submit("go")
        r.run()
        r.cancel("不要了")
        s = r.run()
        self.assertEqual(s.run.status, "cancelled")
        self.assertEqual(tools.executed, [])
        check_invariants(self, store.load("s1"))

    def test_steering_input_goes_after_results(self):
        """工具结果还没补齐时插进来的话，投影时排在结果后面。"""
        box = {}

        class Tools(EchoTools):
            def execute(self, name, args, scope=None):
                if args["x"] == "a":
                    box["r"].submit("顺便加个单测")
                return super().execute(name, args, scope)
        r, model, _, store = make([reply(calls=[("c1", "echo", {"x": "a"}), ("c2", "echo", {"x": "b"})]),
                                   reply("好")], tools=Tools())
        box["r"] = r
        r.submit("go")
        r.run()
        roles = [m["role"] for m in model.calls[1]["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool", "tool", "user"])
        check_invariants(self, store.load("s1"))


class CrashRecovery(unittest.TestCase):
    @staticmethod
    def brain(messages):
        """由对话决定回复的假模型：用户说完就调工具，工具结果回来就回答。"""
        last = messages[-1]
        if last["role"] == "user":
            n = sum(1 for m in messages if m["role"] == "tool")
            return reply(calls=[(f"c{n}", "echo", {"x": str(n)})])
        return reply("完成")

    def full_log(self):
        r, *_, store = make(self.brain)
        r.submit("go")
        r.run()
        return store.load("s1")

    def test_every_crash_point_recovers(self):
        """在账本任意位置截断（模拟崩溃），换新 runner 接着跑，都能走到结束且不变量成立。"""
        full = self.full_log()
        for cut in range(1, len(full) + 1):
            with self.subTest(cut=cut, last=full[cut - 1]["type"]):
                store = MemoryEventStore()
                store.sessions["s1"] = [dict(e) for e in full[:cut]]
                r, model, tools, _ = make(self.brain, store=store)
                s = r.run()
                self.assertIsNotNone(s.run)
                self.assertIn(s.run.status, k.TERMINAL)
                check_invariants(self, store.load("s1"))

    def test_interrupted_tool_not_rerun(self):
        full = self.full_log()
        cut = next(i for i, e in enumerate(full) if e["type"] == "ActionStarted" and e["kind"] == "tool") + 1
        store = MemoryEventStore()
        store.sessions["s1"] = full[:cut]
        r, model, tools, _ = make(self.brain, store=store)
        r.run()
        self.assertEqual(tools.executed, [])
        self.assertIn("执行中断", model.calls[0]["messages"][2]["content"])

    def test_interrupted_model_call_retried(self):
        full = self.full_log()
        cut = next(i for i, e in enumerate(full) if e["type"] == "ActionStarted" and e["kind"] == "model") + 1
        store = MemoryEventStore()
        store.sessions["s1"] = full[:cut]
        r, model, tools, _ = make(self.brain, store=store)
        self.assertEqual(r.run().run.status, "done")
        started = [e for e in store.load("s1") if e["type"] == "ActionStarted" and e["kind"] == "model"]
        self.assertEqual(started[0]["action_id"], started[1]["action_id"])   # 用同一个 id 重做


class Purity(unittest.TestCase):
    def test_decide_is_deterministic(self):
        r, *_, store = make(CrashRecovery.brain)
        r.submit("go")
        r.run()
        events = store.load("s1")
        for cut in range(1, len(events) + 1):
            a = k.decide(k.fold(events[:cut]), Policy())
            b = k.decide(k.fold(events[:cut]), Policy())
            self.assertEqual(a, b)


class Stores(unittest.TestCase):
    def test_jsonl_roundtrip_and_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            st = JsonlEventStore(d)
            st.append("x", [{"type": "A"}], 0)
            st.append("x", [{"type": "B"}, {"type": "C"}], 1)
            self.assertEqual([(e["seq"], e["type"]) for e in st.load("x")], [(1, "A"), (2, "B"), (3, "C")])
            with self.assertRaises(Conflict):
                st.append("x", [{"type": "D"}], 1)
            with st.path("x").open("a") as f:
                f.write('{"type": "half')                    # 写到一半崩了
            self.assertEqual(len(st.load("x")), 3)

    def test_runner_on_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            r, *_ = make([reply("ok")], store=JsonlEventStore(d))
            r.submit("hi")
            self.assertEqual(r.run().run.status, "done")


class ReadFile(unittest.TestCase):
    def test_offset_limit_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text("\n".join(f"line{i}" for i in range(1, 11)))
            out = read_file(str(p), limit=1)
            self.assertIn("line1", out)
            self.assertNotIn("line2", out)
            self.assertIn("offset=2", out)
            self.assertIn("line10", read_file(str(p), offset=10))

    def test_errors_become_results(self):
        out, err = ToolBox().execute("read_file", {"path": "/nope/x"})
        self.assertTrue(err)
        out, err = ToolBox().execute("nope", {})
        self.assertTrue(err)
        out, err = ToolBox().execute("read_file", {})
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()


class JsonlIncremental(unittest.TestCase):
    """账本存储：只读新增部分、外部追加、文件被换掉、崩溃残行、中间坏行。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = JsonlEventStore(self.tmp.name)
        self.p = self.st.path("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_only_new_part(self):
        self.st.append("x", [{"type": "A"}], 0)
        self.st.load("x")
        reads = []
        orig = Path.open

        def spy(path, mode="r", *a, **k):
            f = orig(path, mode, *a, **k)
            if "r" in mode and "b" in mode and path == self.p:
                orig_read = f.read
                f.read = lambda *x: reads.append(1) or orig_read(*x)
            return f
        Path.open = spy
        try:
            self.st.load("x")                                   # 没变化：不读文件内容
            self.assertEqual(reads, [])
            self.st.append("x", [{"type": "B"}], 1)             # 自己写的：直接记进缓存，不读
            self.assertEqual([e["type"] for e in self.st.load("x")], ["A", "B"])
            self.assertEqual(reads, [])
        finally:
            Path.open = orig

    def test_external_append_and_replace(self):
        self.st.append("x", [{"type": "A"}], 0)
        other = JsonlEventStore(self.tmp.name)                   # 另一个进程追加
        other.append("x", [{"type": "B"}], 1)
        self.assertEqual([e["seq"] for e in self.st.load("x")], [1, 2])
        with self.assertRaises(Conflict):
            self.st.append("x", [{"type": "C"}], 1)
        self.p.unlink()
        self.p.write_text(json.dumps({"type": "Z", "seq": 1}) + "\n")   # 文件被换掉
        self.assertEqual([e["type"] for e in self.st.load("x")], ["Z"])

    def test_partial_last_line_is_backed_up_then_truncated(self):
        self.st.append("x", [{"type": "A"}], 0)
        with self.p.open("a") as f:
            f.write('{"type": "half')                            # 写到一半崩了
        self.assertEqual(len(self.st.load("x")), 1)
        self.st.append("x", [{"type": "B"}], 1)
        fresh = JsonlEventStore(self.tmp.name)                    # 新进程从头读
        self.assertEqual([e["type"] for e in fresh.load("x")], ["A", "B"])
        backups = list(Path(self.tmp.name).glob("x.jsonl.corrupt-*"))
        self.assertEqual(backups[0].read_text(), '{"type": "half')

    def test_corrupt_middle_line_backed_up(self):
        self.st.append("x", [{"type": "A"}], 0)
        with self.p.open("a") as f:
            f.write("这不是 JSON\n" + json.dumps({"type": "lost", "seq": 3}) + "\n")
        self.assertEqual([e["type"] for e in self.st.load("x")], ["A"])
        self.st.append("x", [{"type": "B"}], 1)
        self.assertEqual([e["type"] for e in JsonlEventStore(self.tmp.name).load("x")], ["A", "B"])
        backup = next(Path(self.tmp.name).glob("x.jsonl.corrupt-*")).read_text()
        self.assertIn("这不是 JSON", backup)
        self.assertIn("lost", backup)                            # 坏行后面的也留着，没真删

    def test_returns_new_list_each_time(self):
        self.st.append("x", [{"type": "A"}], 0)
        a = self.st.load("x")
        a.append("垃圾")
        self.assertEqual(len(self.st.load("x")), 1)


class IncrementalFold(unittest.TestCase):
    """接着 fold 和一次 fold 全部，结果必须完全一样。"""

    def ledgers(self):
        from tests.test_compaction import Brain, make as make_compaction
        from tests.test_todo_loop import call, run as run_todo, todos
        out = []
        r, *_, store = make(CrashRecovery.brain)
        r.submit("go")
        r.run()
        out.append(store.load("s"))
        r2, store2 = make_compaction(Brain(reads=25))
        r2.submit("go")
        r2.run()
        out.append(store2.load("s"))
        script = [reply(calls=[call(0, "todo_write", todos=todos(("a", "in_progress")))])] + \
                 [reply(calls=[call(i, x="same")]) for i in range(1, 7)]
        out.append(run_todo(script)[2].load("s"))
        return out

    def test_equal_at_every_split(self):
        for events in self.ledgers():
            full = k.fold(events)
            for cut in range(0, len(events) + 1, 3):
                with self.subTest(n=len(events), cut=cut):
                    self.assertEqual(k.fold(events, k.fold(events[:cut])), full)

    def test_replaced_ledger_starts_over(self):
        events = self.ledgers()[0]
        s = k.fold(events)
        other = [dict(e, id="x" + str(e["id"])) for e in events]          # 同样长，但不是同一本
        self.assertEqual(k.fold(other, s), k.fold(other))
        self.assertEqual(k.fold(events[:3], s), k.fold(events[:3]))        # 变短了


class WaitsCloseWithRun(unittest.TestCase):
    def test_cancel_while_waiting_closes_approval(self):
        class Ask(Policy):
            def wait_for(self, call, state):
                return WaitSpec("approval", "")
        r, model, tools, store = make([reply(calls=[("c1", "echo", {"x": "a"})]), reply("第二轮")], policy=Ask())
        r.submit("go")
        self.assertEqual(len(r.run().open_waits), 1)
        r.cancel("不要了")
        s = r.run()
        self.assertEqual((s.run.status, s.open_waits), ("cancelled", []))
        w = list(s.waits.values())[0]
        self.assertTrue(w.closed)
        r.submit("新的一轮")                                   # 旧的审批不会影响新一轮
        self.assertEqual(r.run().run.status, "done")
