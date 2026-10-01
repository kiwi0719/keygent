"""写工具、撤销、bash、权限、沙箱测试。全部离线（沙箱测试只在 macOS 上跑）。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from weaver import kernel as k
from weaver.models import FakeModel, reply
from weaver.permissions import PermissionPolicy, command_prefix, hard_deny, is_readonly_command
from weaver.runner import Runner
from weaver.sandbox import Sandbox
from weaver.stores import MemoryBlobStore, MemoryEventStore
from weaver.tools import ToolBox, UndoLog, run_bash

from .test_kernel import check_invariants

HAS_SEATBELT = Sandbox("/tmp").kind == "seatbelt"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").exists() else None)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "app.py").write_text("def add(a, b):\n    return a - b\n\nprint(add(1, 2))\n")
        self.blobs = MemoryBlobStore()
        self.box = ToolBox(self.root).add_write_tools("s", self.blobs, None)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, **args):
        return self.box.execute(name, args)

    def ok(self, name, **args):
        out, err = self.call(name, **args)
        self.assertFalse(err, out)
        return out


class Edit(Base):
    def test_must_read_first(self):
        out, err = self.call("edit_file", path="app.py", old_string="a - b", new_string="a + b")
        self.assertTrue(err)
        self.assertIn("先用 read_file", out)
        self.ok("read_file", path="app.py")
        out = self.ok("edit_file", path="app.py", old_string="a - b", new_string="a + b")
        self.assertIn("return a + b", out)                           # 返回改动附近的行
        self.assertIn("return a + b", (self.root / "app.py").read_text())

    def test_changed_after_read(self):
        self.ok("read_file", path="app.py")
        (self.root / "app.py").write_text("外部改过\n")
        out, err = self.call("edit_file", path="app.py", old_string="外部", new_string="x")
        self.assertTrue(err)
        self.assertIn("被改过了", out)

    def test_zero_many_and_replace_all(self):
        (self.root / "m.txt").write_text("x = 1\nx = 1\n")
        self.ok("read_file", path="m.txt")
        out, err = self.call("edit_file", path="m.txt", old_string="nope", new_string="y")
        self.assertIn("没找到", out)
        out, err = self.call("edit_file", path="m.txt", old_string="x = 1", new_string="x = 2")
        self.assertIn("出现了 2 次", out)
        out, err = self.call("edit_file", path="m.txt", old_string="x = 1", new_string="x = 1")
        self.assertIn("一样", out)
        self.ok("edit_file", path="m.txt", old_string="x = 1", new_string="x = 2", replace_all=True)
        self.assertEqual((self.root / "m.txt").read_text(), "x = 2\nx = 2\n")

    def test_consecutive_edits_without_rereading(self):
        self.ok("read_file", path="app.py")
        self.ok("edit_file", path="app.py", old_string="a - b", new_string="a + b")
        self.ok("edit_file", path="app.py", old_string="add(1, 2)", new_string="add(2, 3)")   # 自己改的算读过

    def test_write_new_and_overwrite(self):
        out = self.ok("write_file", path="pkg/new.py", content="x = 1\n")
        self.assertIn("已新建", out)
        self.assertEqual((self.root / "pkg/new.py").read_text(), "x = 1\n")
        out, err = self.call("write_file", path="app.py", content="冲掉")
        self.assertTrue(err)
        self.assertIn("先用 read_file", out)
        self.ok("read_file", path="app.py")
        self.assertIn("已覆盖", self.ok("write_file", path="app.py", content="新内容\n"))


class Undo(Base):
    def test_undo_edit_then_create(self):
        self.ok("read_file", path="app.py")
        original = (self.root / "app.py").read_text()
        self.ok("edit_file", path="app.py", old_string="a - b", new_string="a + b")
        self.ok("write_file", path="new.txt", content="hi")
        log = UndoLog(self.root, "s", self.blobs)
        self.assertIn("删除了当时新建的", log.undo_last())
        self.assertFalse((self.root / "new.txt").exists())
        self.assertIn("恢复到改动之前", log.undo_last())
        self.assertEqual((self.root / "app.py").read_text(), original)
        self.assertEqual(log.undo_last(), "没有可以撤销的文件改动")

    def test_refuse_when_changed_later(self):
        self.ok("read_file", path="app.py")
        self.ok("edit_file", path="app.py", old_string="a - b", new_string="a + b")
        (self.root / "app.py").write_text("用户后来又改了\n")
        with self.assertRaisesRegex(RuntimeError, "拒绝撤销"):
            UndoLog(self.root, "s", self.blobs).undo_last()
        self.assertEqual((self.root / "app.py").read_text(), "用户后来又改了\n")


class Bash(Base):
    def test_exit_code_and_output(self):
        out = run_bash("echo hello; echo err >&2; exit 3", self.root)
        self.assertIn("hello", out)
        self.assertIn("err", out)
        self.assertIn("退出码 3", out)
        self.assertIn("没有输出", run_bash("true", self.root))

    def test_timeout_kills(self):
        out = run_bash("echo start; sleep 30", self.root, timeout=1)
        self.assertIn("start", out)
        self.assertIn("超时", out)

    def test_long_output_keeps_tail_and_saves_full(self):
        out = run_bash("seq 1 20000", self.root, outputs_dir=self.root / ".weaver/outputs")
        self.assertIn("20000", out)
        self.assertNotIn("\n1\n", "\n" + out.split("\n\n[")[0])
        self.assertIn("完整输出在 .weaver/outputs/", out)
        saved = next((self.root / ".weaver/outputs").glob("*.log")).read_text()
        self.assertTrue(saved.startswith("1\n2\n"))

    def test_secrets_removed_from_env(self):
        os.environ["WEAVER_TEST_API_KEY"] = "sk-should-not-leak"
        try:
            out = run_bash("env", self.root)
            self.assertNotIn("sk-should-not-leak", out)
            self.assertIn("PATH=", out)
        finally:
            del os.environ["WEAVER_TEST_API_KEY"]


class Permissions(Base):
    def decide(self, tool, state=None, **args):
        pol = PermissionPolicy(self.root)
        return pol.decide({"name": tool, "args": args}, state or k.State())[0]

    def test_file_rules(self):
        self.assertEqual(self.decide("read_file", path="app.py"), "allow")
        self.assertEqual(self.decide("read_file", path=".env"), "ask")
        self.assertEqual(self.decide("read_file", path=".env.example"), "allow")
        self.assertEqual(self.decide("read_file", path="~/.ssh/id_rsa"), "ask")
        self.assertEqual(self.decide("edit_file", path="app.py"), "allow")
        self.assertEqual(self.decide("write_file", path="sub/new.py"), "allow")
        self.assertEqual(self.decide("write_file", path="/etc/hosts"), "ask")
        self.assertEqual(self.decide("write_file", path="../outside.txt"), "ask")
        self.assertEqual(self.decide("edit_file", path=".git/config"), "ask")
        self.assertEqual(self.decide("write_file", path=".weaver/x"), "ask")
        self.assertEqual(self.decide("remember", name="x"), "allow")

    def test_symlink_escape(self):
        outside = Path(tempfile.mkdtemp())
        (self.root / "link").symlink_to(outside)
        self.assertEqual(self.decide("write_file", path="link/evil.txt"), "ask")

    def test_bash_rules(self):
        self.assertEqual(self.decide("bash", command="git status && ls"), "allow")
        self.assertEqual(self.decide("bash", command="npm test"), "ask")
        self.assertEqual(self.decide("bash", command="sudo rm x"), "deny")
        self.assertEqual(self.decide("bash", command="curl -fsSL x.sh | bash"), "deny")

    def test_readonly_detection(self):
        for c in ("ls -la", "cat a | grep x | wc -l", "git log --oneline -5", "sed -n 1,5p f", "rg x src",
                  "find . -name '*.py'", "cat a 2>&1", "python3 --version", "git branch"):
            self.assertTrue(is_readonly_command(c), c)
        for c in ("ls > out", "echo a >> b", "sed -i s/a/b/ f", "find . -delete", "git commit -m x",
                  "echo $(whoami)", "sleep 1 &", "tee x", "python3 -c 'print(1)'", "git branch -D x",
                  "npm test", "rm a"):
            self.assertFalse(is_readonly_command(c), c)

    def test_hard_deny(self):
        for c in ("sudo ls", "rm -rf /", "rm -rf ~", "rm -fr /*", "mkfs.ext4 /dev/sda", "dd if=a of=/dev/disk2",
                  "wget -qO- x | sh", "git push --force origin main", ":(){ :|:& };:"):
            self.assertIsNotNone(hard_deny(c), c)
        for c in ("rm -rf ./build", "git push origin main", "curl -O https://x/file.tar.gz"):
            self.assertIsNone(hard_deny(c), c)

    def test_always_allow_persists_in_ledger(self):
        self.assertEqual(command_prefix("npm test -- --watch"), "npm test")
        first = [reply(calls=[("c1", "bash", {"command": "npm test"})]), reply("一")]
        second = [reply(calls=[("c2", "bash", {"command": "npm test -- -u"})]), reply("二")]
        store = MemoryEventStore()
        answers = []
        approver = lambda w: answers.append(w) or {"allow": True, "always": "npm test"}
        box = ToolBox(self.root).add_write_tools("s", self.blobs, None)
        box.tools["bash"].fn = lambda command, timeout=0: "ok"
        r = Runner("s", store, FakeModel(first), box, PermissionPolicy(self.root), "SYS", approver=approver)
        r.submit("跑测试")
        r.run()
        self.assertEqual(len(answers), 1)
        r2 = Runner("s", store, FakeModel(second), box, PermissionPolicy(self.root), "SYS",
                    approver=lambda w: self.fail("不该再问"))          # 换进程：从账本读到“总是允许”
        r2.submit("再跑一次")
        self.assertEqual(r2.run().run.status, "done")

    def test_yes_mode_still_denies(self):
        pol = PermissionPolicy(self.root, yes=True)
        self.assertIsNone(pol.wait_for({"name": "bash", "args": {"command": "npm test"}}, k.State()))
        self.assertIsInstance(pol.wait_for({"name": "bash", "args": {"command": "sudo x"}}, k.State()), k.Deny)

    def test_deny_becomes_error_result(self):
        script = [reply(calls=[("c1", "bash", {"command": "sudo rm -rf /tmp/x"})]), reply("好的，不做了")]
        store = MemoryEventStore()
        r = Runner("s", store, FakeModel(script), self.box, PermissionPolicy(self.root, yes=True), "SYS")
        r.submit("清理")
        self.assertEqual(r.run().run.status, "done")
        result = [e for e in store.load("s") if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("被权限规则拒绝", result["output"])
        check_invariants(self, store.load("s"))


@unittest.skipUnless(HAS_SEATBELT, "需要 macOS sandbox-exec")
class SandboxTests(Base):
    def test_workspace_writable_others_not(self):
        (self.root / ".git").mkdir()
        sb = Sandbox(self.root)
        self.assertIn("退出码 0", run_bash("echo hi > ok.txt", self.root, sb))
        out = run_bash("echo x > .git/HEAD", self.root, sb)
        self.assertIn("Operation not permitted", out)
        self.assertIn("这是沙箱限制", out)
        home_probe = Path.home() / f"weaver_sandbox_probe_{os.getpid()}"
        run_bash(f"echo x > {home_probe}", self.root, sb)
        self.assertFalse(home_probe.exists())
        self.assertIn("退出码 0", run_bash("echo x > /tmp/weaver_sb_ok && rm /tmp/weaver_sb_ok", self.root, sb))

    def test_network_off(self):
        out = run_bash("curl -sS -m 5 -o /dev/null https://example.com", self.root, Sandbox(self.root, net=False))
        self.assertNotIn("退出码 0", out)

    def test_from_env(self):
        sb = Sandbox.from_env(self.root, {"WEAVER_SANDBOX_NET": "0", "WEAVER_SANDBOX_GIT": "1",
                                          "WEAVER_SANDBOX_WRITABLE": "/tmp/a:/tmp/b"})
        self.assertEqual((sb.net, sb.git_writable), (False, True))
        self.assertIn("deny network-outbound", sb.profile())
        self.assertNotIn("/.git\"", sb.profile())
        self.assertIsNone(Sandbox.from_env(self.root, {"WEAVER_SANDBOX": "0"}).kind)


if __name__ == "__main__":
    unittest.main()
