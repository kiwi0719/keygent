"""后台命令、取消时打断前台命令、recall。全部离线（真跑 /bin/bash，不用沙箱）。"""
from __future__ import annotations

import json
import re
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from weaver.daemon.manager import TaskManager
from weaver.daemon.tasks import TaskStore
from weaver.jobs import Jobs
from weaver.models import FakeModel, reply
from weaver.policy import Policy
from weaver.runner import Runner
from weaver.stores import MemoryEventStore
from weaver.tools import ToolBox
from weaver.tools.recall import recall

T = 10


def alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def job_id(out: str) -> str:
    return re.match(r"后台命令 (j[0-9a-f]+)", out).group(1)


def until(pred, timeout=T):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Background(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.notes: list[str] = []
        self.jobs = self.make()

    def make(self, **kw):
        return Jobs(self.root, self.root / "jobs", notify=self.notes.append, poll=0.05, **kw)

    def tearDown(self):
        self.jobs.close()
        self.tmp.cleanup()

    def test_start_output_and_notify(self):
        out = self.jobs.start("echo hi; sleep 0.5; echo bye", wait=0.2)
        jid = job_id(out)
        self.assertIn("在后台运行中", out)
        self.assertIn("hi", out)
        self.assertTrue(until(lambda: self.notes))
        self.assertIn(f"后台命令 {jid}（echo hi; sleep 0.5; echo bye）结束了，退出码 0", self.notes[0])
        self.assertIn("bye", self.notes[0])
        o = self.jobs.output(jid)
        self.assertIn("结束了，退出码 0", o)
        self.assertIn("bye", o)
        self.assertNotIn("hi", o)                                    # 只给上次之后的新输出
        self.assertIn("(没有新输出)", self.jobs.output(jid))
        reg = json.loads((self.root / "jobs/registry.json").read_text())
        self.assertEqual(reg[0]["status"], "done")

    def test_quick_command_returns_result_directly(self):
        out = self.jobs.start("echo fast; exit 3")
        self.assertIn("已经结束，结束了，退出码 3", out)
        time.sleep(0.3)
        self.assertEqual(self.notes, [])                             # 结果已经直接返回了，不再另外通知

    def test_kill_is_silent_and_kills_children(self):
        out = self.jobs.start("sleep 30 & sleep 30", wait=0.1)
        jid = job_id(out)
        pid = self.jobs.jobs[jid].pid
        self.assertTrue(alive(pid))
        self.assertIn("已结束", self.jobs.kill(jid))
        self.assertFalse(alive(pid))
        self.assertIn("已被结束", self.jobs.list())
        time.sleep(0.2)
        self.assertEqual(self.notes, [])                             # 主动结束的不通知
        with self.assertRaises(ValueError):
            self.jobs.output("nope")

    def test_idle_limit_and_log_limit(self):
        self.jobs.close()
        self.jobs = self.make(idle_limit=0.5, log_limit=2000)
        out = self.jobs.start("head -c 5000 /dev/zero | tr '\\\\0' x; sleep 30", wait=0.3)
        jid = job_id(out)
        self.assertTrue(until(lambda: self.notes))
        self.assertIn("没有新输出，自动结束", self.notes[0])
        log = (self.root / "jobs" / f"{jid}.log").read_bytes()
        self.assertLessEqual(len(log), 2000)
        self.assertTrue(log.startswith("[...前面的输出超过上限已丢弃...]".encode()))

    def test_leftovers_cleaned_on_restart(self):
        out = self.jobs.start("sleep 30", wait=0.1)
        jid = job_id(out)
        pid = self.jobs.jobs[jid].pid
        self.jobs._stop.set()                                        # 模拟进程被强杀：什么都没清理
        # 另一个不相干的进程组，登记表里写着它的 PID 但命令对不上：不能误杀
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)
        reg = json.loads((self.root / "jobs/registry.json").read_text())
        reg.append({**reg[0], "id": "jfake", "pid": other.pid, "ident": "Thu Jan 1 00:00:00 1970"})   # 启动时间对不上
        (self.root / "jobs/registry.json").write_text(json.dumps(reg))
        try:
            again = self.make()
            self.assertEqual(sorted(j["id"] for j in again.cleaned), sorted([jid, "jfake"]))
            self.assertFalse(alive(pid))
            self.assertTrue(alive(other.pid))
            self.assertEqual(again.jobs[jid].status, "killed")
        finally:
            other.kill()
            other.wait()


class CancelInterrupts(unittest.TestCase):
    def test_cancel_kills_running_foreground_command(self):
        with tempfile.TemporaryDirectory() as d:
            tools = ToolBox(d).add_write_tools("s", None, None)
            r = Runner("s", MemoryEventStore(), FakeModel([reply(calls=[("c1", "bash", {"command": "sleep 30"})]),
                                                         reply("不该走到这")]),
                       tools, Policy(), "SYS")
            r.submit("跑")
            threading.Timer(0.5, r.cancel, args=("不要了",)).start()
            started = time.time()
            state = r.run()
            self.assertLess(time.time() - started, 5)
            self.assertEqual(state.run.status, "cancelled")
            out = [e for e in r.store.load("s") if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]
            self.assertIn("任务被取消，命令被终止", out["output"])
            from weaver.daemon.humanize import steps
            self.assertEqual([x["status"] for x in steps(r.store.load("s")) if x["kind"] == "step"], ["cancelled"])


class DaemonJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ts = TaskStore(root / "tasks", scratch=root / "scratch")
        self.script: list = []

        def factory(meta, store, sink, on_delta):
            tools = ToolBox(meta.workdir)
            tools.add_write_tools(meta.id, None, None, jobs=Jobs(meta.workdir, store.root / "jobs", poll=0.05))
            return Runner("ledger", store, FakeModel(self.script), tools, Policy(), "SYS", sink=sink, on_delta=on_delta)
        self.mgr = TaskManager(self.ts, factory)

    def tearDown(self):
        self.mgr.close(wait=True)
        self.tmp.cleanup()

    def test_finish_wakes_task(self):
        self.script[:] = [reply(calls=[("c1", "bash", {"command": "sleep 4; echo 构建完成", "background": True})]),
                          reply("已经在后台跑了，结束会通知我"),
                          reply("构建完成了")]
        t = self.mgr.create("后台构建")["id"]
        self.assertTrue(until(lambda: sum(e["type"] == "RunFinished" for e in self.mgr.events(t)) == 2))
        self.assertTrue(self.mgr.wait_idle(t, timeout=T))
        notes = [e for e in self.mgr.events(t) if e["type"] == "InputReceived" and e["source"] == "system"]
        self.assertEqual(len(notes), 1)
        self.assertIn("构建完成", notes[0]["content"][0]["text"])
        self.assertEqual(self.mgr.summary(t)["now"], "构建完成了")

    def test_archive_ends_jobs(self):
        self.script[:] = [reply(calls=[("c1", "bash", {"command": "sleep 30", "background": True})]), reply("在跑")]
        t = self.mgr.create("开发服务器")["id"]
        self.assertTrue(self.mgr.wait_idle(t, timeout=T))
        jobs = self.mgr.runner(t).tools.jobs
        pid = jobs.running()[0].pid
        self.mgr.archive(t)
        self.assertFalse(alive(pid))


class Recall(unittest.TestCase):
    def test_search_and_seq(self):
        ev = [
            {"type": "InputReceived", "seq": 1, "source": "context", "content": [{"type": "text", "text": "环境 token"}]},
            {"type": "InputReceived", "seq": 2, "source": "user", "content": [{"type": "text", "text": "看看报错"}]},
            {"type": "ActionCompleted", "seq": 3, "kind": "tool", "output": "Traceback\nKeyError: 'user_id' in handler.py"},
            {"type": "ActionCompleted", "seq": 4, "kind": "model",
             "output": {"content": [{"type": "text", "text": "是 handler.py 里 user_id 没取到"}]}},
        ]
        out = recall(ev, "user_id handler")
        self.assertTrue(out.startswith("找到 2 条"))
        self.assertLess(out.index("#4"), out.index("#3"))             # 新的在前
        self.assertIn("没有同时包含", recall(ev, "token"))            # 背景信息不搜
        self.assertEqual(recall(ev, seq=3), "#3 工具结果\nTraceback\nKeyError: 'user_id' in handler.py")
        self.assertIn("没有第 9 条", recall(ev, seq=9))
        self.assertIn("请给 query", recall(ev))


if __name__ == "__main__":
    unittest.main()
