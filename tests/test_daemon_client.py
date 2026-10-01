"""命令行薄客户端、系统通知、launchd 配置。HTTP 是真的（本机端口），模型是假的；不碰 launchctl 和真的系统通知。"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from weaver.daemon import client as cl
from weaver.daemon.control import env_file, plist
from weaver.daemon.notify import Notifier, _quote
from weaver.models import reply

from .test_daemon_http import Base, Stream
from .test_todo_loop import call


class ClientBase(Base):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "home"
        self.srv.write_info(self.home / "daemon.json")
        patcher = mock.patch.dict(os.environ, {"WEAVER_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = cl.Api.connect()
        self.lines: list[str] = []

    def write(self, s=""):
        self.lines.append(str(s))

    def reader(self, *answers):
        it = iter(answers)

        def read(prompt=""):
            self.lines.append(prompt)
            a = next(it)
            if isinstance(a, BaseException):
                raise a
            return a
        return read

    @property
    def text(self):
        return "\n".join(self.lines)


class Connect(ClientBase):
    def test_connect_and_stale_pid(self):
        self.assertIsNotNone(self.api)
        info = json.loads((self.home / "daemon.json").read_text())
        dead = subprocess.Popen(["true"])
        dead.wait()
        (self.home / "daemon.json").write_text(json.dumps({**info, "pid": dead.pid}))
        self.assertIsNone(cl.Api.connect())                        # 进程不在了：当作没在运行
        (self.home / "daemon.json").write_text(json.dumps({**info, "token": "wrong"}))
        self.assertIsNone(cl.Api.connect())                        # 连不上（token 不对）

    def test_cli_stream_is_not_an_app(self):
        with mock.patch.dict(os.environ):
            import queue
            import threading
            q, stop = queue.Queue(), threading.Event()
            self.api.stream(q, stop)
            app = Stream(self.srv.port, "secret")
            self.streams.append(app)
            end = time.time() + 5
            while time.time() < end and self.srv.app_clients != 1:
                time.sleep(0.02)
            self.assertEqual(self.srv.app_clients, 1)                # 命令行那条不算
            stop.set()


class RunPrompt(ClientBase):
    def test_run_with_edit_and_steps(self):
        self.next_script = [reply("先发布一下", calls=[("c1", "bash", {"command": "npm publish"})]), reply("发布完了")]
        code = cl.run_prompt(self.api, "发布", self.tmp.name, self.write,
                             self.reader("e", "npm publish --dry-run"))
        self.assertEqual(code, 0)
        t = self.text
        self.assertIn("交给 weaverd", t)
        self.assertNotIn("你：", t)                                 # 自己刚说的话不再重复显示
        self.assertIn("? 跑命令 npm publish", t)
        self.assertIn("[e] 改参数", t)
        self.assertIn("ran npm publish --dry-run", t)
        self.assertIn("发布完了", t)
        self.assertTrue(t.rstrip().endswith("[完成]" + cl.RESET))
        self.assertEqual(t.count("⏺ 跑命令 npm publish\n"), 1)

    def test_deny_and_ctrl_c_detaches(self):
        self.next_script = [reply(calls=[("c1", "bash", {"command": "rm -rf build"})]), reply("好吧不删了")]
        self.assertEqual(cl.run_prompt(self.api, "清理", self.tmp.name, self.write,
                                       self.reader("n", "还要用")), 0)
        self.assertIn("被拒绝：用户拒绝了这次调用。原因：还要用", self.text)

        self.lines.clear()
        self.next_script = [reply(calls=[("c1", "bash", {"command": "make deploy"})]), reply("好")]
        self.assertEqual(cl.run_prompt(self.api, "部署", self.tmp.name, self.write,
                                       self.reader(KeyboardInterrupt())), 0)
        self.assertIn("不看了，任务在后台继续", self.text)
        t = next(x for x in self.api.call("GET", "/v1/tasks")["tasks"] if x["title"] == "部署")
        self.assertEqual(t["status"], "waiting")                   # 任务还在，等着

        self.lines.clear()                                         # 接着看：补显示已有的，再问
        self.assertEqual(cl.attach(self.api, t["id"][:6], self.write, self.reader("y")), 0)
        self.assertIn(f"你：{cl.RESET}部署", self.text)
        self.assertIn("[完成]", self.text)

    def test_stuck_hint(self):
        self.next_script = [reply(calls=[call(i, x="same")]) for i in range(5)] + [reply("换了个思路")]
        from weaver.tools import Tool, _spec
        orig = self.mgr.factory

        def factory(meta, store, sink, on_delta):
            r = orig(meta, store, sink, on_delta)
            r.tools.tools["t"] = Tool(_spec("t", "", {"x": {"type": "string"}}, []), lambda x="": "同样的结果")
            from weaver.policy import Policy
            r.policy = Policy(interactive=True)
            return r
        self.mgr.factory = factory
        cl.run_prompt(self.api, "试试", self.tmp.name, self.write, self.reader("换个参数试试"))
        self.assertIn("它好像卡住了", self.text)
        self.assertIn("换了个思路", self.text)

    def test_subcommands(self):
        self.next_script = [reply(calls=[("c1", "bash", {"command": "ls -la /"})])]
        self.api.call("POST", "/v1/tasks", {"text": "看看根目录"})
        self.assertTrue(self.mgr.wait_idle(timeout=5))
        cl.cmd_tasks(self.api, self.write)
        self.assertRegex(self.text, r"[0-9a-f]{8}  等你 .*看看根目录")
        cl.cmd_waits(self.api, self.write)
        self.assertIn("看看根目录 · 跑命令 ls -la /", self.text)
        with self.assertRaisesRegex(cl.ClientError, "没有以 zzz 开头的任务"):
            self.api.resolve_task("zzz")


class Notify(unittest.TestCase):
    class Mgr:
        def __init__(self):
            self.fn, self.waits = None, []

        def subscribe(self, fn):
            self.fn = fn
            return lambda: None

        def detail(self, task_id, mark_seen=True):
            return {"waiting": self.waits}

    def setUp(self):
        self.mgr, self.sent, self.apps, self.now = self.Mgr(), [], [0], [1000.0]
        self.n = Notifier(self.mgr, lambda: self.apps[0], send=lambda *a: self.sent.append(a),
                          long_task=60, clock=lambda: self.now[0])

    def summary(self, status, **kw):
        self.mgr.fn("task", "t1", {"title": "Q3 流失分析", "status": status, "note": "", "now": "", "waiting": 0, **kw})

    def test_wait_error_and_long_done(self):
        self.summary("running")
        self.mgr.waits = [{"id": "w1", "kind": "approval", "title": "跑命令 npm publish"}]
        self.summary("waiting", waiting=1)
        self.summary("waiting", waiting=1)                         # 同一件只通知一次
        self.assertEqual(self.sent, [("Weaver", "跑命令 npm publish", "Q3 流失分析 · 等你放行")])
        self.now[0] += 30
        self.summary("running")
        self.summary("done", now="结论")                           # 只跑了 30 秒：不通知
        self.assertEqual(len(self.sent), 1)
        self.summary("running")
        self.now[0] += 61
        self.summary("done", now="结论：华北最多")
        self.assertEqual(self.sent[-1], ("Weaver", "结论：华北最多", "Q3 流失分析 · 做完了"))
        self.summary("error", note="预算用完，已收尾")
        self.assertEqual(self.sent[-1], ("Weaver", "预算用完，已收尾", "Q3 流失分析 · 出错了"))

    def test_app_connected_suppresses(self):
        self.apps[0] = 1
        self.summary("error", note="炸了")
        self.assertEqual(self.sent, [])

    def test_quote(self):
        self.assertEqual(_quote('说 "hi" \\ 了'), '"说 \\"hi\\" \\\\ 了"')


class Launchd(unittest.TestCase):
    def test_plist(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("X=1")
            self.assertEqual(env_file(Path(d)), env.resolve())
            data = plist(env, python_path=Path("/proj"), weaver_home=Path(d))
            self.assertEqual(data["ProgramArguments"][1:], ["-m", "weaver.daemon", "--env", str(env)])
            self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(data["EnvironmentVariables"]["PYTHONPATH"], "/proj")
            self.assertEqual(data["EnvironmentVariables"]["WEAVER_HOME"], d)
            p = Path(d) / "x.plist"
            p.write_bytes(plistlib.dumps(data))
            if Path("/usr/bin/plutil").exists():
                self.assertEqual(subprocess.run(["plutil", "-lint", str(p)], capture_output=True).returncode, 0)
            with mock.patch.dict(os.environ, {"WEAVER_HOME": d}):
                self.assertEqual(env_file(Path(d) / "nowhere"), Path(d) / ".env")


if __name__ == "__main__":
    unittest.main()
