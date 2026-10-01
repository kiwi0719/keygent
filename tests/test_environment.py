"""环境信息、通用背景输入机制、Linux 沙箱（bwrap）测试。bwrap 的真实测试只在能用 bwrap 的 Linux 上跑。"""
from __future__ import annotations

import os
import platform
import tempfile
import unittest
from pathlib import Path

from weaver.context import ContextProvider, last_digests, pending_context
from weaver.environment import Environment
from weaver.memory import Memory
from weaver.models import FakeModel, reply
from weaver.policy import Policy
from weaver.runner import Runner
from weaver.sandbox import Sandbox
from weaver.stores import MemoryEventStore
from weaver.tools import ToolBox, run_bash

LINUX_BWRAP = platform.system() == "Linux" and Sandbox("/tmp").kind == "bwrap"


def fake_which(available):
    return lambda c: f"/usr/bin/{c}" if c in available else None


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, available=("python3", "git"), **kw):
        return Environment(self.root, which=fake_which(available), today=lambda: "2026-10-01", **kw)

    def test_contents(self):
        text = self.env().render()
        self.assertIn(f"工作目录：{self.root}", text)
        self.assertIn("不用 cd 到别处", text)
        self.assertIn("可用命令：python3", text)
        self.assertIn("没有 python，请用 python3", text)
        self.assertNotIn("node", text)
        self.assertIn("不是 git 仓库", text)
        self.assertIn("日期：2026-10-01", text)
        self.assertNotIn("沙箱", text)

    def test_python_present_no_hint(self):
        self.assertNotIn("请用 python3", self.env(("python3", "python")).render())

    def test_git_branch_and_sandbox(self):
        (self.root / ".git").mkdir()
        (self.root / ".git/HEAD").write_text("ref: refs/heads/feature-x\n")
        sub = self.root / "pkg"
        sub.mkdir()
        text = Environment(sub, sandbox=Sandbox(sub), which=fake_which(()), today=lambda: "d").render()
        self.assertIn("当前分支 feature-x", text)                     # 在子目录里也找得到仓库
        self.assertIn("只能写工作目录", text) if Sandbox(sub).kind else self.assertIn("无沙箱", text)


class Provider(ContextProvider):
    def __init__(self, kind, text):
        self.kind, self.text = kind, text
        self.first_head, self.update_head = f"{kind} 开头", f"{kind} 更新"

    def render(self):
        return self.text


class Context(unittest.TestCase):
    def test_pending_order_changes_and_empty(self):
        env, mem, empty = Provider("environment", "E1"), Provider("memory", "M1"), Provider("x", "")
        first = pending_context([env, mem, empty], [])
        self.assertEqual([c["kind"] for c in first], ["environment", "memory"])   # 空的不占位置
        events = [{"type": "InputReceived", "source": "context", "context_kind": c["kind"], "digest": c["digest"]}
                  for c in first]
        self.assertEqual(pending_context([env, mem], events), [])
        env.text = "E2"
        again = pending_context([env, mem], events)
        self.assertEqual([c["kind"] for c in again], ["environment"])
        self.assertTrue(again[0]["text"].startswith("environment 更新"))

    def test_legacy_memory_digest(self):
        self.assertEqual(last_digests([{"type": "InputReceived", "source": "context", "memory_digest": "abc"}]),
                         {"memory": "abc"})

    def test_runner_puts_environment_then_memory_and_appends_updates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "AGENTS.md").write_text("项目规则")
            today = ["2026-10-01"]
            env = Environment(root, which=fake_which(("python3",)), today=lambda: today[0])
            mem = Memory(root, root / "home")
            store = MemoryEventStore()
            model = FakeModel([reply("一"), reply("二"), reply("三")])
            r = Runner("s", store, model, ToolBox(root), Policy(), "SYS", context=[env, mem])
            r.submit("第一个")
            r.run()
            ctx = [e for e in store.load("s") if e["type"] == "InputReceived" and e["source"] == "context"]
            self.assertEqual([e["context_kind"] for e in ctx], ["environment", "memory"])
            r.submit("第二个")                                        # 没变化
            r.run()
            today[0] = "2026-10-02"                                   # 过了一天
            r.submit("第三个")
            r.run()
            ctx = [e for e in store.load("s") if e["type"] == "InputReceived" and e["source"] == "context"]
            self.assertEqual([e["context_kind"] for e in ctx], ["environment", "memory", "environment"])
            self.assertIn("环境有变化", ctx[-1]["content"][0]["text"])
            calls = [[{k: v for k, v in m.items() if k != "cache"} for m in c["messages"]] for c in model.calls]
            for a, b in zip(calls, calls[1:]):
                self.assertEqual(a, b[:len(a)])                       # 前缀不变


class BwrapArgs(unittest.TestCase):
    def test_argument_order_and_options(self):
        sb = Sandbox("/work/p", net=False, extra_writable=["/cache"])
        a = sb.bwrap_args(["/bin/bash", "-c", "x"])
        s = " ".join(a)
        self.assertIn("--ro-bind / /", s)
        self.assertLess(s.index("--bind /work/p /work/p"), s.index("--ro-bind-try /work/p/.git /work/p/.git"))
        self.assertIn("--ro-bind-try /work/p/.weaver /work/p/.weaver", s)
        self.assertIn("--bind-try /cache /cache", s)
        for flag in ("--new-session", "--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-net",
                     "--cap-drop ALL", "--chdir /work/p"):
            self.assertIn(flag, s)
        self.assertEqual(a[-4:], ["--", "/bin/bash", "-c", "x"])
        s2 = " ".join(Sandbox("/work/p", git_writable=True).bwrap_args(["true"]))
        self.assertNotIn("/work/p/.git", s2)
        self.assertNotIn("--unshare-net", s2)

    @unittest.skipIf(platform.system() == "Linux", "这里测非 Linux 平台的行为")
    def test_not_used_off_linux(self):
        self.assertNotEqual(Sandbox("/tmp").kind, "bwrap")


@unittest.skipUnless(LINUX_BWRAP, "需要能用 bwrap 的 Linux")
class BwrapReal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".git").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_writable_others_not(self):
        sb = Sandbox(self.root)
        self.assertIn("退出码 0", run_bash("echo hi > ok.txt && cat ok.txt", self.root, sb))
        self.assertTrue((self.root / "ok.txt").exists())
        out = run_bash("echo x > .git/HEAD", self.root, sb)
        self.assertIn("Read-only file system", out)                   # 是被只读挂载拦下的，不是别的错
        self.assertIn("这是沙箱限制", out)
        probe = Path.home() / f"weaver_bwrap_probe_{os.getpid()}"
        run_bash(f"echo x > {probe}", self.root, sb)
        self.assertFalse(probe.exists())
        self.assertIn("退出码 0", run_bash("echo x > /tmp/weaver_bw && cat /tmp/weaver_bw", self.root, sb))

    def test_network_off(self):
        code = ("import socket\ntry:\n    socket.create_connection(('1.1.1.1', 53), 3)\n    print('CONNECTED')\n"
                "except OSError as e:\n    print('BLOCKED', e)")
        (self.root / "net.py").write_text(code)
        out = run_bash("python3 net.py", self.root, Sandbox(self.root, net=False))
        self.assertIn("BLOCKED", out)                                   # 命令真的跑了，是连接被拦
        self.assertIn("退出码 0", out)
        self.assertIn("CONNECTED", run_bash("python3 net.py", self.root, Sandbox(self.root)))


if __name__ == "__main__":
    unittest.main()
