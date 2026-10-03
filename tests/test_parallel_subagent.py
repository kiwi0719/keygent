"""并行执行工具、子 Agent 测试。全部离线。"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from weaver import kernel as k
from weaver.models import reply
from weaver.permissions import PermissionPolicy
from weaver.policy import Policy
from weaver.runner import Runner
from weaver.stores import MemoryBlobStore, MemoryEventStore
from weaver.subagent import SubAgents
from weaver.tools import Tool, ToolBox, _spec

from .test_kernel import check_invariants


class Script:
    """按剧本回复的假模型（线程安全）。"""

    def __init__(self, steps):
        self.steps, self.calls, self.lock = list(steps), [], threading.Lock()

    def create(self, system, tools, messages, tool_choice="auto", on_delta=None, cache_key=None):
        with self.lock:
            self.calls.append({"system": system, "tools": tools, "messages": messages})
            return self.steps.pop(0)


class Timeline:
    """记录工具执行：同时有几个在跑、写操作开始时有没有别人在跑。"""

    def __init__(self):
        self.lock, self.active, self.peak, self.write_overlap, self.order = threading.Lock(), 0, 0, False, []

    def tool(self, name, readonly, delay):
        def fn(tag):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                if not readonly and self.active > 1:
                    self.write_overlap = True
            time.sleep(delay(tag) if callable(delay) else delay)
            with self.lock:
                self.active -= 1
                self.order.append(tag)
            if tag == "boom":
                raise RuntimeError("炸了")
            return f"{name}:{tag}"
        return Tool(_spec(name, "", {"tag": {"type": "string"}}, ["tag"]), fn, readonly=readonly)


def box_with(timeline, delay=0.2):
    box = ToolBox(tools={})
    box.tools["r"] = timeline.tool("r", True, delay)
    box.tools["w"] = timeline.tool("w", False, delay)
    return box


def calls(*items):
    return [(f"c{i}", name, {"tag": tag}) for i, (name, tag) in enumerate(items)]


class Parallel(unittest.TestCase):
    def run_one(self, items, delay=0.2):
        tl = Timeline()
        model = Script([reply(calls=calls(*items)), reply("完成")])
        store = MemoryEventStore()
        r = Runner("s", store, model, box_with(tl, delay), Policy(), "SYS")
        r.submit("go")
        t0 = time.monotonic()
        s = r.run()
        return tl, model, store, s, time.monotonic() - t0

    def test_readonly_calls_run_together(self):
        tl, _, store, s, elapsed = self.run_one([("r", "a"), ("r", "b"), ("r", "c")])
        self.assertEqual(s.run.status, "done")
        self.assertLess(elapsed, 0.45)                               # 串行要 0.6 秒
        self.assertEqual(tl.peak, 3)
        check_invariants(self, store.load("s"))

    def test_write_is_exclusive(self):
        tl, _, store, _, _ = self.run_one([("r", "a"), ("r", "b"), ("w", "x"), ("r", "c"), ("r", "d")], 0.1)
        self.assertFalse(tl.write_overlap)
        self.assertEqual(tl.order.index("x"), 2)                      # 写操作等前面跑完，后面的等它跑完
        self.assertEqual(set(tl.order[:2]), {"a", "b"})
        check_invariants(self, store.load("s"))

    def test_results_written_back_in_call_order(self):
        tl, model, store, _, _ = self.run_one([("r", "slow"), ("r", "mid"), ("r", "fast")],
                                              delay=lambda t: {"slow": 0.3, "mid": 0.15, "fast": 0.0}[t])
        self.assertEqual(tl.order, ["fast", "mid", "slow"])          # 完成顺序是反的
        tools = [m["content"] for m in model.calls[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(tools, ["r:slow", "r:mid", "r:fast"])       # 写回仍按调用顺序

    def test_one_failure_does_not_affect_others(self):
        _, model, store, s, _ = self.run_one([("r", "a"), ("r", "boom"), ("r", "c")])
        tools = [m for m in model.calls[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([t["is_error"] for t in tools], [False, True, False])
        self.assertIn("炸了", tools[1]["content"])
        self.assertEqual(s.run.status, "done")
        check_invariants(self, store.load("s"))

    def test_bash_readonly_is_parallel(self):
        box = ToolBox().add_write_tools("s", MemoryBlobStore(), None)
        self.assertTrue(box.concurrency_safe("bash", {"command": "git status && ls"}))
        self.assertFalse(box.concurrency_safe("bash", {"command": "npm test"}))
        self.assertTrue(box.concurrency_safe("grep", {"pattern": "x"}))
        self.assertFalse(box.concurrency_safe("edit_file", {}))
        self.assertFalse(box.concurrency_safe("nope", {}))

    def test_crash_in_middle_of_batch(self):
        _, _, store, _, _ = self.run_one([("r", "a"), ("r", "b"), ("r", "c")])
        events = store.load("s")
        first_done = next(i for i, e in enumerate(events) if e["type"] == "ActionCompleted" and e["kind"] == "tool")
        store2 = MemoryEventStore()
        store2.sessions["s"] = events[:first_done + 1]               # 三个都开始了，只完成了一个
        tl = Timeline()
        r = Runner("s", store2, Script([reply("收尾")]), box_with(tl), Policy(), "SYS")
        self.assertEqual(r.run().run.status, "done")
        self.assertEqual(tl.order, [])                               # 中断的不重跑
        results = [e for e in store2.load("s") if e["type"] == "ActionCompleted" and e["kind"] == "tool"]
        self.assertEqual(sum(1 for e in results if e.get("synthetic")), 2)
        check_invariants(self, store2.load("s"))

    def test_prefix_stable(self):
        tl = Timeline()
        model = Script([reply(calls=calls(("r", "a"), ("r", "b"))), reply(calls=calls(("r", "c"), ("r", "d"))),
                        reply("完成")])
        r = Runner("s", MemoryEventStore(), model, box_with(tl, 0.05), Policy(), "SYS")
        r.submit("go")
        r.run()
        msgs = [[{kk: v for kk, v in m.items() if kk != "cache"} for m in c["messages"]] for c in model.calls]
        for a, b in zip(msgs, msgs[1:]):
            self.assertEqual(a, b[:len(a)])


class Sub(unittest.TestCase):
    """父子共用一个假模型：看 system 分辨是谁在问。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "a.py").write_text("def f():\n    raise ValueError('x')\n")
        self.store, self.blobs = MemoryEventStore(), MemoryBlobStore()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, brain, parent_steps, **kw):
        sub = SubAgents(session="p", store=self.store, model=brain, root=self.root, sandbox=None, blobs=self.blobs,
                        **kw)
        box = ToolBox(self.root).add_write_tools("p", self.blobs, None)
        box.tools["task"] = sub.tool()
        brain.parent = list(parent_steps)
        return Runner("p", self.store, brain, box, PermissionPolicy(self.root), "父 SYS"), sub

    def children(self):
        return [s for s in self.store.sessions if s.startswith("p--")]

    def test_explore_children_in_parallel_and_results_returned(self):
        brain = Brain(child_delay=0.3)
        r, _ = self.make(brain, [
            reply(calls=[("t1", "task", {"description": "查错误处理", "prompt": "找出 raise 的地方"}),
                         ("t2", "task", {"description": "查入口", "prompt": "找出入口函数"})]),
            reply("总结完毕")])
        r.submit("原话：帮我看看这个项目的错误处理")
        t0 = time.monotonic()
        s = r.run()
        self.assertLess(time.monotonic() - t0, 0.55)                 # 两个子 Agent 并行
        self.assertEqual(s.run.status, "done")
        tools = [m for m in brain.parent_calls[1]["messages"] if m["role"] == "tool"]
        self.assertIn("子结论：找出 raise 的地方", tools[0]["content"])
        self.assertIn("子 Agent「查错误处理」（explore）：done", tools[0]["content"])
        self.assertIn("子结论：找出入口函数", tools[1]["content"])
        self.assertEqual(len(self.children()), 2)
        check_invariants(self, self.store.load("p"))

    def test_child_ledger_context_and_prefix(self):
        brain = Brain()
        r, _ = self.make(brain, [reply(calls=[("t1", "task", {"description": "甲", "prompt": "任务甲"}),
                                              ("t2", "task", {"description": "乙", "prompt": "任务乙"})]),
                                 reply("好")])
        r.submit("用户原话一")
        r.run()
        for sid in self.children():
            ev = self.store.load(sid)
            self.assertTrue(all(e.get("parent_id", "").startswith("p:t") for e in ev))
            ctx = [e.get("context_kind") for e in ev if e["type"] == "InputReceived" and e["source"] == "context"]
            self.assertEqual(ctx, ["environment"])                    # 有环境，没有记忆
            user = [e for e in ev if e["type"] == "InputReceived" and e["source"] == "user"][0]
            text = user["content"][0]["text"]
            self.assertLess(text.index("用户原话一"), text.index("## 任务"))   # 原话在前，任务在后
        a, b = brain.child_calls[:2]
        self.assertEqual(a["system"], b["system"])
        self.assertEqual(a["tools"], b["tools"])
        self.assertEqual(a["messages"][0], b["messages"][0])          # [工具][system][环境] 逐字节相同
        self.assertNotEqual(a["messages"][1], b["messages"][1])

    def test_toolsets_by_mode(self):
        _, sub = self.make(Brain(), [])
        explore, general = sub.toolbox("explore"), sub.toolbox("general")
        self.assertEqual(sorted(explore.tools), ["bash", "find_files", "grep", "read_file"])
        self.assertEqual(sorted(general.tools), ["bash", "edit_file", "find_files", "grep", "read_file", "write_file"])
        tool = sub.tool()
        self.assertTrue(tool.parallel({"mode": "explore"}) and tool.parallel({}))
        self.assertFalse(tool.parallel({"mode": "general"}))

    def test_explore_bash_write_denied(self):
        brain = Brain(child_script=[reply(calls=[("b1", "bash", {"command": "rm a.py"})]), reply("只读，没删")])
        r, _ = self.make(brain, [reply(calls=[("t1", "task", {"description": "试删", "prompt": "删掉 a.py"})]),
                                 reply("好")])
        r.submit("go")
        r.run()
        self.assertTrue((self.root / "a.py").exists())
        child = self.store.load(self.children()[0])
        denied = [e for e in child if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]
        self.assertIn("explore 子 Agent 是只读的", denied["output"])

    def test_general_can_edit_and_undo_in_parent_session(self):
        brain = Brain(child_script=[
            reply(calls=[("x1", "read_file", {"path": "a.py"})]),
            reply(calls=[("x2", "edit_file", {"path": "a.py", "old_string": "ValueError", "new_string": "KeyError"})]),
            reply("改好了")])
        r, _ = self.make(brain, [reply(calls=[("t1", "task", {"description": "改", "prompt": "改异常类型",
                                                              "mode": "general"})]), reply("好")])
        r.submit("go")
        r.run()
        self.assertIn("KeyError", (self.root / "a.py").read_text())
        from weaver.tools import UndoLog
        UndoLog(self.root, "p", self.blobs).undo_last()                   # 父会话的撤销日志里有子 Agent 的改动
        self.assertIn("ValueError", (self.root / "a.py").read_text())

    def test_usage_counts_toward_parent_budget(self):
        brain = Brain(child_usage=(5000, 100))
        r, _ = self.make(brain, [reply(calls=[("t1", "task", {"description": "甲", "prompt": "查"})], usage=(10, 5)),
                                 reply("好", usage=(10, 5))])
        r.submit("go")
        s = r.run()
        self.assertGreaterEqual(s.run.tokens, 5100 + 30)
        started = [e for e in self.store.load("p") if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]
        self.assertEqual(started["usage"]["input_tokens"], 5100)

    def test_child_over_limit_still_reports(self):
        brain = Brain(child_script=[reply(calls=[(f"g{i}", "grep", {"pattern": "x"})]) for i in range(3)] +
                      [reply("没查完")])
        r, sub = self.make(brain, [reply(calls=[("t1", "task", {"description": "甲", "prompt": "查"})]), reply("好")])
        sub.limits["max_steps"] = 2
        r.submit("go")
        r.run()
        result = [e for e in self.store.load("p") if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]
        self.assertIn("budget", result["output"])
        self.assertTrue(result["is_error"])


class Warm(unittest.TestCase):
    """同时派出的子 Agent：领头的先发，跟随的等它开始流式输出（缓存已写好）再发。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def run_three(self, brain, **kw):
        sub = SubAgents(session="p", store=MemoryEventStore(), model=brain, root=self.root, sandbox=None,
                        blobs=MemoryBlobStore(), **kw)
        threads = [threading.Thread(target=sub.run, args=(f"t{i}", f"任务{i}")) for i in range(3)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return sub, time.monotonic() - t0

    def test_followers_start_after_leader_streams(self):
        brain = Brain(child_delay=0.3)
        _, elapsed = self.run_three(brain)
        starts = sorted(brain.child_starts)
        self.assertEqual(len(starts), 3)
        self.assertLess(starts[1] - starts[0], 0.1)          # 跟随者只等到领头的开始输出，不等它跑完
        self.assertLess(elapsed, 0.75)                       # 仍然是并行的（串行至少 0.9 秒；CI 的机器慢，留出余量）

    def test_followers_wait_when_leader_is_slow_to_stream(self):
        class Slow(Brain):
            def create(self, system, tools, messages, **kw):
                with self.lock:
                    self.child_starts.append(time.monotonic())
                time.sleep(0.2)                               # 0.2 秒后才开始输出
                kw.get("on_delta") and kw["on_delta"]({"type": "text", "text": ""})
                return reply("好")
        brain = Slow()
        self.run_three(brain)
        starts = sorted(brain.child_starts)
        self.assertGreaterEqual(starts[1] - starts[0], 0.18)

    def test_leader_failure_releases(self):
        class Boom(Brain):
            def create(self, system, tools, messages, **kw):
                with self.lock:
                    self.child_starts.append(time.monotonic())
                    first = len(self.child_starts) == 1
                if first:
                    time.sleep(0.1)
                    raise RuntimeError("领头的挂了")
                return reply("好")
        brain = Boom()
        _, elapsed = self.run_three(brain)
        self.assertEqual(len(brain.child_starts), 3)
        self.assertLess(elapsed, 1.0)                         # 没有等满 20 秒

    def test_warm_prefix_reused_then_expires(self):
        from weaver.subagent import WarmGate
        now = [0.0]
        g = WarmGate(ttl=240, clock=lambda: now[0])
        leader, ev, stamp = g.enter("explore")
        self.assertTrue(leader)
        self.assertFalse(g.enter("explore")[0])               # 同一批：跟随
        self.assertTrue(g.enter("general")[0])                # 另一种模式前缀不同，单独预热
        g.open(ev, stamp)
        now[0] = 100
        self.assertFalse(g.enter("explore")[0])               # 4 分钟内：直接跟随，不用等
        now[0] = 300
        self.assertTrue(g.enter("explore")[0])                # 过期：重新选领头的


class Budget(unittest.TestCase):
    def test_cached_input_counts_one_tenth(self):
        self.assertEqual(k.billable({"input_tokens": 30000, "cached_tokens": 28000, "output_tokens": 100}),
                         2000 + 2800 + 100)
        self.assertEqual(k.billable({"input_tokens": 1000, "output_tokens": 10}), 1010)
        self.assertEqual(k.billable({"input_tokens": 100, "cached_tokens": 500}), 10)     # 缓存数不会超过输入
        self.assertEqual(k.billable({}), 0)


class Brain:
    """父子共用的假模型。父按 self.parent 剧本；子默认直接回答“子结论：<任务>”，也可以给子剧本。"""

    def __init__(self, child_delay=0.0, child_script=None, child_usage=(10, 5)):
        self.child_delay, self.child_script, self.child_usage = child_delay, child_script, child_usage
        self.parent, self.parent_calls, self.child_calls, self.child_starts = [], [], [], []
        self.lock = threading.Lock()
        self.name = "fake"

    def create(self, system, tools, messages, tool_choice="auto", on_delta=None, cache_key=None):
        if system.startswith("你是主 Agent 派出的"):
            with self.lock:
                self.child_calls.append({"system": system, "tools": tools, "messages": messages})
                if self.child_script:
                    return self.child_script.pop(0)
            with self.lock:
                self.child_starts.append(time.monotonic())
            if on_delta:
                on_delta({"type": "text", "text": ""})      # 像真模型一样：先开始流式输出（输入已处理完）
            time.sleep(self.child_delay)
            task = messages[-1]["content"][0]["text"].split("## 任务\n")[-1]
            return reply(f"子结论：{task}", usage=self.child_usage)
        with self.lock:
            self.parent_calls.append({"system": system, "tools": tools, "messages": messages})
            return self.parent.pop(0)


if __name__ == "__main__":
    unittest.main()
