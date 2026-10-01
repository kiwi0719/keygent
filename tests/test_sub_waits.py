"""子 Agent 的审批升到主任务的“等你的事”：放行、改参数、拒绝、主任务取消。真 ToolBox + 真 bash，假模型。"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from weaver.daemon.humanize import steps
from weaver.daemon.manager import TaskManager
from weaver.daemon.tasks import TaskStore
from weaver.models import FakeModel, reply
from weaver.permissions import PermissionPolicy
from weaver.runner import Runner
from weaver.subagent import SubAgents
from weaver.tools import ToolBox

T = 10


def until(pred, timeout=T):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class SubWaits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script: list = []

        def factory(meta, store, sink, on_delta):
            model = FakeModel(self.script)
            tools = ToolBox(meta.workdir).add_write_tools(meta.id, None, None)
            parent: list = []
            subs = SubAgents(session="ledger", store=store, model=model, root=Path(meta.workdir).resolve(),
                             sandbox=None, blobs=None, approver=lambda w, info: parent[0].ask_up(w, info, poll=0.02),
                             approver_info=True)
            tools.tools["task"] = subs.tool()
            tools.on_interrupt.append(subs.cancel_all)
            parent.append(Runner("ledger", store, model, tools, PermissionPolicy(meta.workdir, interactive=True),
                                 "SYS", sink=sink, on_delta=on_delta))
            return parent[0]
        self.mgr = TaskManager(TaskStore(self.root / "tasks", scratch=self.root / "s"), factory)
        self.work = self.root / "w"
        self.work.mkdir()

    def tearDown(self):
        self.mgr.close(wait=True)
        self.tmp.cleanup()

    def start(self, command="touch made.txt"):
        self.script[:] = [
            reply("派个子 Agent", calls=[("p1", "task", {"description": "建文件", "prompt": "建文件", "mode": "general"})]),
            reply("我来建", calls=[("k1", "bash", {"command": command})]),       # 子 Agent
            reply("建好了"),                                                       # 子 Agent 汇报
            reply("子 Agent 做完了"),                                              # 主 Agent
        ]
        t = self.mgr.create("建个文件", workdir=str(self.work))["id"]
        self.assertTrue(until(lambda: self.mgr.waits()))
        return t, self.mgr.waits()[0]

    def test_allow_from_parent_inbox(self):
        t, w = self.start()
        self.assertEqual((w["kind"], w["title"], w["from_agent"]), ("approval", "跑命令 touch made.txt", "建文件"))
        self.assertIn("来自子 Agent：建文件", w["body"])
        self.assertEqual(w["choices"], ["allow", "deny", "always", "edit"])
        self.assertEqual(self.mgr.summary(t)["status"], "waiting")
        st = [s for s in steps(self.mgr.events(t)) if s["kind"] == "step"]
        self.assertEqual((st[0]["title"], st[0]["status"]), ("派子 Agent：建文件（可改文件）", "waiting"))
        self.mgr.answer(w["id"], "allow")
        self.assertTrue(self.mgr.wait_idle(t, T))
        self.assertTrue((self.work / "made.txt").exists())
        self.assertEqual(self.mgr.summary(t)["status"], "done")

    def test_edit_args(self):
        t, w = self.start("touch prod.txt")
        self.mgr.answer(w["id"], "allow", args={"command": "touch test.txt"})
        self.assertTrue(self.mgr.wait_idle(t, T))
        self.assertTrue((self.work / "test.txt").exists())
        self.assertFalse((self.work / "prod.txt").exists())

    def test_deny(self):
        t, w = self.start()
        self.mgr.answer(w["id"], "deny", note="别建")
        self.assertTrue(self.mgr.wait_idle(t, T))
        self.assertFalse((self.work / "made.txt").exists())
        self.assertEqual(self.mgr.summary(t)["status"], "done")

    def test_cancel_parent_while_child_waits(self):
        t, w = self.start()
        self.mgr.cancel(t)
        self.assertTrue(self.mgr.wait_idle(t, T))
        self.assertEqual((self.mgr.summary(t)["status"], self.mgr.waits()), ("cancelled", []))
        self.assertFalse((self.work / "made.txt").exists())
        child = [p for p in self.mgr.store.dir(t).glob("ledger--*.jsonl")]
        self.assertEqual(len(child), 1)
        from weaver.stores import JsonlEventStore
        from weaver import kernel as k
        self.assertEqual(k.fold(JsonlEventStore(child[0].parent).load(child[0].stem)).run.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
