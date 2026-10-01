"""记忆自动提取：前缀不变、只许 remember、什么时候提取、scratch 任务没有项目层、文件锁。全部离线。"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from weaver.memory import Memory
from weaver.models import FakeModel, reply
from weaver.policy import Policy
from weaver.runner import Runner
from weaver.stores import MemoryEventStore
from weaver.tools import Tool, ToolBox, _spec


def box():
    b = ToolBox(tools={})
    b.tools["read_file"] = Tool(_spec("read_file", "", {"path": {"type": "string"}}, ["path"]), lambda path="": "内容")
    return b


def remember(name, typ="user", **kw):
    return ("r-" + name, "remember", {"name": name, "type": typ, "description": f"关于 {name}", "content": "正文", **kw})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "proj").mkdir()
        self.mem = Memory(self.root / "proj", home=self.root / "home")

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, script, memory=None):
        tools = box()
        tools.tools.update((memory or self.mem).tools())
        self.model = FakeModel(script)
        return Runner("s", MemoryEventStore(), self.model, tools, Policy(), "SYS", extract_memory=memory or self.mem)

    def three_steps(self, final="做完了"):
        return [reply(calls=[(f"c{i}", "read_file", {"path": f"f{i}"})]) for i in range(2)] + [reply(final)]


class TaskEnd(Base):
    def test_extracts_after_done_with_same_prefix(self):
        r = self.runner(self.three_steps() + [reply("记一下", calls=[remember("prefer-pnpm"),
                                                                  ("x", "bash", {"command": "ls"})]),
                                              reply("没有了")])
        r.submit("帮我看看")
        r.run()
        last_req = self.model.calls[-1]
        out = r.maybe_extract()
        self.assertEqual(out["saved"], ["prefer-pnpm"])
        ex = self.model.calls[len(self.model.calls) - 2]               # 提取的第一次请求
        self.assertEqual((ex["system"], ex["tools"]), (last_req["system"], last_req["tools"]))
        self.assertEqual(ex["messages"][:len(last_req["messages"]) - 1], last_req["messages"][:-1])
        self.assertIn("只能用 remember 和 forget", ex["messages"][-1]["content"][0]["text"])
        second = self.model.calls[-1]["messages"]
        self.assertIn("只能用 remember 和 forget", [m for m in second if m["role"] == "tool"][-1]["content"])
        self.assertTrue((self.root / "home/memory/prefer-pnpm.md").exists())
        self.assertIsNone(r.maybe_extract())                            # 同一轮只提取一次
        ev = [e for e in r.store.load("s") if e["type"] == "ActionCompleted" and e["kind"] == "extract"][0]
        self.assertEqual(ev["output"]["reason"], "task_end")
        from weaver import kernel as k
        self.assertEqual(k.fold(r.store.load("s")).run.status, "done")   # 不影响任务状态

    def test_skips(self):
        r = self.runner([reply("很快")])                                # 少于 3 步
        r.submit("hi")
        r.run()
        self.assertIsNone(r.maybe_extract())
        r = self.runner([reply(calls=[remember("x")]), reply(calls=[("c", "read_file", {"path": "a"})]),
                         reply("好")])                                   # 这一轮自己记过
        r.submit("hi")
        r.run()
        self.assertIsNone(r.maybe_extract())
        r = self.runner(self.three_steps())
        r.submit("hi")
        r.run()
        r.cancel("x")                                                   # 结束后才取消：不影响
        with mock.patch.dict(os.environ, {"WEAVER_MEMORY_EXTRACT": "0"}):
            self.assertIsNone(r.maybe_extract())

    def test_not_after_error(self):
        def boom(_):
            raise RuntimeError("挂了")
        r = self.runner([reply(calls=[("c0", "read_file", {"path": "a"})]),
                         reply(calls=[("c1", "read_file", {"path": "b"})]), boom])
        r.submit("hi")
        r.run()
        self.assertIsNone(r.maybe_extract())


class BeforeCompact(Base):
    def test_extract_before_summary_only(self):
        from weaver.compaction import Compactor
        calls = []
        c = Compactor(keep_recent=10, min_gain=1)
        events = []
        r = self.runner([reply(calls=[(f"c{i}", "read_file", {"path": "x" * 50})]) for i in range(6)] + [reply("好")])
        r.submit("开始 " + "很长的说明 " * 200)
        r.run()
        events = r.store.load("s")
        c.compact(events, FakeModel([reply("摘要")]), "SYS", [], 100_000, 4000, before_summary=lambda: calls.append(1))
        self.assertEqual(calls, [1])


class NoProject(Base):
    def test_scratch_memory_is_user_only(self):
        m = Memory(self.root / "proj", home=self.root / "home", project=False)
        out = m.remember("deploy", "project", "部署方式", "用 fly.io")
        self.assertIn("这个任务没有项目，存成了用户级", out)
        self.assertTrue((self.root / "home/memory/deploy.md").exists())
        self.assertFalse((self.root / "proj/.weaver").exists())
        self.assertNotIn("项目级记忆索引", m.snapshot())
        r = self.runner(self.three_steps() + [reply("没有")], memory=m)
        r.submit("hi")
        r.run()
        r.maybe_extract()
        self.assertIn("这个任务没有项目", self.model.calls[-1]["messages"][-1]["content"][0]["text"])

    def test_concurrent_writes_keep_index(self):
        threads = [threading.Thread(target=self.mem.remember, args=(f"m{i}", "user", f"第 {i} 条", "x"))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        index = (self.root / "home/memory/MEMORY.md").read_text()
        self.assertEqual(len(index.strip().splitlines()), 20)


class Daemon(unittest.TestCase):
    def test_manager_extracts_after_done(self):
        from weaver.daemon.manager import TaskManager
        from weaver.daemon.tasks import TaskStore
        from weaver.daemon.humanize import steps
        with tempfile.TemporaryDirectory() as d:
            mem = Memory(d, home=Path(d) / "home")
            script = [reply(calls=[(f"c{i}", "read_file", {"path": "a"})]) for i in range(2)] + \
                     [reply("做完了"), reply(calls=[remember("likes-tea")]), reply("好")]

            def factory(meta, store, sink, on_delta):
                tools = box()
                tools.tools.update(mem.tools())
                return Runner("ledger", store, FakeModel(script), tools, Policy(), "SYS", sink=sink,
                              on_delta=on_delta, extract_memory=mem)
            mgr = TaskManager(TaskStore(Path(d) / "tasks", scratch=Path(d) / "s"), factory)
            t = mgr.create("hi")["id"]
            self.assertTrue(mgr.wait_idle(timeout=5))
            self.assertEqual(steps(mgr.events(t))[-1]["title"], "记住了 1 条")
            self.assertEqual(mgr.summary(t)["status"], "done")
            mgr.close(wait=True)


if __name__ == "__main__":
    unittest.main()
