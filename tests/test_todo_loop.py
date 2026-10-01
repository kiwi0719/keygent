"""todo 清单与防死循环测试。全部离线。"""
from __future__ import annotations

import unittest

from weaver import kernel as k
from weaver.guards import detect
from weaver.models import FakeModel, reply
from weaver.policy import Policy
from weaver.runner import Runner
from weaver.stores import MemoryEventStore
from weaver.todo import render, todo_write, tool as todo_tool
from weaver.tools import Tool, ToolBox, _spec

from .test_kernel import check_invariants


def todos(*items):
    return [{"content": c, "status": s} for c, s in items]


class Tools(ToolBox):
    """todo_write + 一个可控的 echo：result 参数决定返回什么，err 决定是否报错。"""

    def __init__(self):
        super().__init__(tools={})
        self.tools["todo_write"] = todo_tool()

        def echo(x="", result=None, err=False):
            if err:
                raise RuntimeError(result or "失败")
            return result if result is not None else f"echo {x}"
        self.tools["echo"] = Tool(_spec("echo", "", {"x": {"type": "string"}}, []), echo)
        self.tools["bash"] = Tool(_spec("bash", "", {"command": {"type": "string"}}, []),
                                  lambda command="", err=False: (_ for _ in ()).throw(RuntimeError("命令失败"))
                                  if err else f"ok\n\n[退出码 0，用时 0.{len(command)} 秒]")


def call(i, name="echo", **args):
    return (f"c{i}", name, args)


def run(script, policy=None, approver=None, store=None):
    store = store or MemoryEventStore()
    model = FakeModel(script)
    r = Runner("s", store, model, Tools(), policy or Policy(max_steps=50), "SYS", approver=approver)
    r.submit("go")
    return r, model, store, r.run()


def nudges(store, prefix=""):
    return [e["nudge"] for e in store.load("s")
            if e["type"] == "InputReceived" and e.get("nudge") and e["nudge"].startswith(prefix)]


class TodoTool(unittest.TestCase):
    def test_validation_and_render(self):
        out = todo_write(todos(("读代码", "completed"), ("改 bug", "in_progress"), ("跑测试", "pending")))
        self.assertIn("[✓] 读代码\n[→] 改 bug\n[ ] 跑测试", out)
        self.assertIn("还有 2 项没完成", out)
        with self.assertRaisesRegex(ValueError, "一次只做一步"):
            todo_write(todos(("a", "in_progress"), ("b", "in_progress")))
        with self.assertRaisesRegex(ValueError, "status"):
            todo_write([{"content": "a", "status": "doing"}])
        with self.assertRaisesRegex(ValueError, "content"):
            todo_write([{"status": "pending"}])
        self.assertEqual(render([]), "（清单是空的）")

    def test_list_comes_from_ledger(self):
        script = [reply(calls=[call(1, "todo_write", todos=todos(("a", "in_progress"), ("b", "pending")))]),
                  reply(calls=[call(2, "todo_write", todos=[{"content": "坏的", "status": "x"}])]),    # 报错的不算
                  reply(calls=[call(3, "todo_write", todos=todos(("a", "completed"), ("b", "completed")))]),
                  reply("完成")]
        _, _, store, s = run(script)
        self.assertEqual(s.run.status, "done")
        self.assertEqual([t["status"] for t in k.fold(store.load("s")).todos], ["completed", "completed"])
        cut = next(i for i, e in enumerate(store.load("s")) if e["type"] == "ActionCompleted" and e["kind"] == "tool")
        s2 = k.fold(store.load("s")[:cut + 1])                         # 崩溃在第一次写清单之后
        self.assertEqual([t["content"] for t in s2.todos], ["a", "b"])


class TodoNudges(unittest.TestCase):
    def test_stale_reminder_and_cooldown(self):
        script = [reply(calls=[call(0, "todo_write", todos=todos(("a", "in_progress"), ("b", "pending")))])]
        script += [reply(calls=[call(i, x=str(i))]) for i in range(1, 24)]
        script += [reply(calls=[call(99, "todo_write", todos=todos(("a", "completed"), ("b", "completed")))]),
                   reply("完成")]
        _, model, store, s = run(script)
        self.assertEqual(nudges(store, "todo_stale"), ["todo_stale", "todo_stale"])   # 第 10 步和第 20 步各一次
        text = [e for e in store.load("s") if e.get("nudge") == "todo_stale"][0]["content"][0]["text"]
        self.assertIn("[→] a", text)
        self.assertEqual(s.run.status, "done")
        check_invariants(self, store.load("s"))

    def test_done_with_unfinished_is_sent_back_once(self):
        script = [reply(calls=[call(1, "todo_write", todos=todos(("a", "completed"), ("b", "pending")))]),
                  reply("我做完了"), reply("真的做完了")]
        _, model, store, s = run(script)
        self.assertEqual(s.run.status, "done")
        self.assertEqual(nudges(store, "todo_done"), ["todo_done"])                     # 只打回一次
        self.assertIn("[ ] b", model.calls[2]["messages"][-1]["content"][0]["text"])

    def test_done_with_everything_finished_passes(self):
        script = [reply(calls=[call(1, "todo_write", todos=todos(("a", "completed"), ("b", "cancelled")))]),
                  reply("完成")]
        _, _, store, s = run(script)
        self.assertEqual((s.run.status, nudges(store)), ("done", []))

    def test_restore_after_summary(self):
        script = [reply(calls=[call(1, "todo_write", todos=todos(("a", "in_progress")))]), reply("好")]
        r, model, store, s = run(script)
        r.policy.compact_threshold = 1
        r.compactor.keep_recent = 1
        r.model.script = [reply("## 目标\n摘要"), reply(calls=[call(2, x="y")]),
                          reply(calls=[call(3, "todo_write", todos=todos(("a", "completed")))]), reply("好")]
        self.assertEqual(r.compact()["mode"], "summary")
        r.policy.compact_threshold = 10 ** 9
        r.submit("接着做")
        r.run()
        self.assertIn("todo_restore", nudges(store))


class Loops(unittest.TestCase):
    def test_detect_patterns(self):
        h = lambda key, err=False, res="r", name=None: {"step": 0, "key": key, "name": name or key.split(":")[0],
                                                        "error": err, "result": res}
        self.assertEqual([f.kind for f in detect([h("a:1")] * 3)], ["repeat"])
        progress = [h("a:1", res=str(i)) for i in range(5)]                           # 有进展的重复
        self.assertEqual(detect(progress), [])
        kinds = {f.kind for f in detect([h("a:1", err=True, res=str(i)) for i in range(3)])}
        self.assertEqual(kinds, {"same_error", "tool_errors"} - {"tool_errors"})
        self.assertIn("tool_errors", {f.kind for f in detect([h(f"read:{i}", True, name="read") for i in range(4)])})
        self.assertEqual(detect([h(f"bash:{i}", True, name="bash") for i in range(6)]), [])  # bash 换命令失败不算
        alt = [h("a:1", res="x"), h("b:1", res="y")] * 3
        self.assertIn("alternating", {f.kind for f in detect(alt)})
        timing = [h("bash:x", res=f"ok\n[退出码 0，用时 0.{i} 秒]", name="bash") for i in range(3)]
        self.assertEqual(detect(timing)[0].kind, "repeat")                           # 忽略用时

    def test_repeat_nudges_then_stops_unattended(self):
        script = [reply(calls=[call(i, x="same")]) for i in range(10)] + [reply("好")]
        _, model, store, s = run(script)
        self.assertEqual(s.run.status, "stuck")
        self.assertEqual(nudges(store, "loop:"), ["loop:repeat"])
        self.assertIn("连续 3 次调用 echo", [e for e in store.load("s") if e.get("nudge")][0]["content"][0]["text"])
        self.assertEqual(sum(1 for e in store.load("s") if e["type"] == "ActionCompleted" and e["kind"] == "tool"), 5)
        check_invariants(self, store.load("s"))

    def test_progressing_repeats_are_fine(self):
        script = [reply(calls=[call(i, x="poll", result=f"进度 {i * 10}%")]) for i in range(8)] + [reply("好")]
        _, _, store, s = run(script)
        self.assertEqual((s.run.status, nudges(store)), ("done", []))

    def test_interactive_asks_then_continue_resets(self):
        answers = [{"allow": True, "note": "换成读 README"}]
        script = [reply(calls=[call(i, x="same")]) for i in range(5)] + \
                 [reply(calls=[call(50 + i, x="same")]) for i in range(2)] + [reply("好")]
        _, model, store, s = run(script, Policy(max_steps=50, interactive=True), approver=lambda w: answers.pop(0))
        self.assertEqual(s.run.status, "done")                     # 继续后清零，后面 2 次不再触发
        hint = [e for e in store.load("s") if e.get("nudge") == "stuck_hint"]
        self.assertIn("换成读 README", hint[0]["content"][0]["text"])
        self.assertEqual([e["kind"] for e in store.load("s") if e["type"] == "WaitStarted"], ["stuck"])

    def test_interactive_stop(self):
        script = [reply(calls=[call(i, x="same")]) for i in range(6)]
        _, _, store, s = run(script, Policy(max_steps=50, interactive=True), approver=lambda w: {"allow": False})
        self.assertEqual(s.run.status, "stuck")

    def test_interactive_waits_across_processes(self):
        script = [reply(calls=[call(i, x="same")]) for i in range(5)]
        _, _, store, s = run(script, Policy(max_steps=50, interactive=True))     # 没有 approver：停下等
        self.assertEqual(s.run.status, "running")
        self.assertEqual([w.kind for w in s.open_waits], ["stuck"])

    def test_same_error_and_nudge_appended_at_end(self):
        script = [reply(calls=[call(i, x="a", err=True)]) for i in range(4)] + [reply("放弃了")]
        _, model, store, s = run(script)
        self.assertIn("loop:same_error", nudges(store))
        msgs = [[{kk: v for kk, v in m.items() if kk != "cache"} for m in c["messages"]] for c in model.calls]
        for a, b in zip(msgs, msgs[1:]):
            self.assertEqual(a, b[:len(a)])                                   # 提醒追加在末尾，前缀不变


class BudgetNudge(unittest.TestCase):
    def test_warn_once_at_80_percent(self):
        script = [reply(calls=[call(i, x=str(i))]) for i in range(10)] + [reply("收尾")]
        _, model, store, s = run(script, Policy(max_steps=10))
        self.assertEqual(nudges(store, "budget"), ["budget"])
        self.assertIn("8/10 步", [e for e in store.load("s") if e.get("nudge") == "budget"][0]["content"][0]["text"])


class SubagentStuck(unittest.TestCase):
    def test_child_stuck_reported_to_parent(self):
        import tempfile
        from pathlib import Path
        from weaver.stores import MemoryBlobStore
        from weaver.subagent import SubAgents

        class Brain:
            name = "fake"

            def __init__(self):
                self.parent = [reply(calls=[("t1", "task", {"description": "甲", "prompt": "查"})]), reply("好")]

            def create(self, system, tools, messages, **kw):
                if system.startswith("你是主 Agent 派出的"):
                    n = sum(1 for m in messages if m["role"] == "assistant")
                    return reply(calls=[(f"g{n}", "grep", {"pattern": "zzz_never"})])
                return self.parent.pop(0)
        with tempfile.TemporaryDirectory() as d:
            store = MemoryEventStore()
            sub = SubAgents(session="p", store=store, model=Brain(), root=Path(d), sandbox=None, blobs=MemoryBlobStore())
            out, meta = sub.run("甲", "查")
            self.assertIn("stuck", out)
            self.assertTrue(meta["is_error"])


if __name__ == "__main__":
    unittest.main()
