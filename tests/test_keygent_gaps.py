"""Keygent 补缺（design/keygent-gaps.md）的后端：改了哪些文件 / diff / 撤销、todo、子 Agent 的过程、归档恢复、
“总是允许”的撤销、信任过的项目、记忆页。真 ToolBox（写文件、bash）+ 假模型，全部离线。"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from weaver import kernel as k
from weaver.daemon.changes import Changes, call_diff, counts, edit_diff
from weaver.daemon.manager import Conflict, NotFound, TaskManager
from weaver.daemon.server import DaemonServer
from weaver.daemon.tasks import TaskStore, revoked_keys
from weaver.daemon.uploads import Uploads
from weaver.errors import BadRequest
from weaver.models import FakeModel, reply
from weaver.permissions import PermissionPolicy
from weaver.runner import Runner
from weaver.settings import memories, permits
from weaver.settings.service import Settings
from weaver.stores import DirBlobStore
from weaver.subagent import SubAgents
from weaver.tools import ToolBox
from weaver.tools.edit import UndoLog

from .test_daemon_http import Client

T = 10


def until(pred, timeout=T):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class ChangesUnit(unittest.TestCase):
    """撤销日志按文件汇总，不经过任务管理器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.blobs = DirBlobStore(self.root / "blobs")
        self.work = self.root / "w"
        self.work.mkdir()
        self.log = UndoLog(self.work, "t1", self.blobs)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        p = self.work / name
        before = p.read_bytes() if p.exists() else None
        p.write_text(text)
        self.log.record(p, before, text.encode())
        return p

    def test_summary_counts_and_diff(self):
        self.write("a.py", "one\ntwo\nthree\n")
        self.log = UndoLog(self.work, "t1", self.blobs)
        (self.work / "b.py").write_text("x\n")
        self.write("b.py", "x\ny\n")
        self.write("b.py", "x\ny\nz\n")
        ch = Changes(self.work, "t1", self.blobs)
        s = {c["rel"]: c for c in ch.summary()}
        self.assertEqual((s["a.py"]["added"], s["a.py"]["removed"], s["a.py"]["created"]), (3, 0, True))
        self.assertEqual((s["b.py"]["added"], s["b.py"]["removed"], s["b.py"]["created"]), (2, 0, False))
        self.assertTrue(s["b.py"]["can_undo"])
        d = ch.diff("b.py")["diff"]
        self.assertIn("+y\n", d)
        self.assertIn("+z\n", d)
        self.assertIn(" x\n", d)

    def test_undo_file_restores_and_created_file_is_removed(self):
        (self.work / "b.py").write_text("orig\n")
        self.write("b.py", "first\n")
        self.write("new.py", "hello\n")
        self.write("b.py", "second\n")
        ch = Changes(self.work, "t1", self.blobs)
        self.assertEqual(ch.undo("b.py"), 2)
        self.assertEqual((self.work / "b.py").read_text(), "orig\n")
        ch.undo(str(self.work / "new.py"))
        self.assertFalse((self.work / "new.py").exists())
        s = {c["rel"]: c for c in ch.summary()}
        self.assertTrue(s["b.py"]["undone"] and s["new.py"]["undone"])
        with self.assertRaises(LookupError):
            ch.undo("b.py")

    def test_changed_afterwards_blocks_undo(self):
        (self.work / "c.py").write_text("orig\n")
        self.write("c.py", "mine\n")
        (self.work / "c.py").write_text("someone else\n")
        ch = Changes(self.work, "t1", self.blobs)
        item = ch.summary()[0]
        self.assertFalse(item["can_undo"])
        self.assertEqual(item["why"], "之后又被改过")
        with self.assertRaises(RuntimeError):
            ch.undo("c.py")
        self.assertEqual((self.work / "c.py").read_text(), "someone else\n")

    def test_old_records_without_after_blob(self):
        p = self.work / "d.py"
        p.write_text("a\n")
        self.log.record(p, b"a\n", b"a\nb\n")
        p.write_text("a\nb\n")
        lines = [json.loads(x) for x in self.log.path.read_text().splitlines()]
        for e in lines:
            e.pop("after_blob", None)
        self.log.path.write_text("".join(json.dumps(e) + "\n" for e in lines))
        item = Changes(self.work, "t1", self.blobs).summary()[0]
        self.assertEqual((item["added"], item["removed"]), (1, 0))

    def test_diffs_from_args(self):
        self.assertEqual(counts("a\nb\n", "a\nc\n"), (1, 1))
        d = edit_diff("edit_file", {"path": "x", "old_string": "a = 1\n", "new_string": "a = 2\n"})
        self.assertIn("-a = 1", d)
        self.assertIn("+a = 2", d)
        self.assertEqual(edit_diff("bash", {"command": "ls"}), "")
        (self.work / "e.py").write_text("keep\nold\nkeep\n")
        d = call_diff("edit_file", {"path": "e.py", "old_string": "old", "new_string": "new"}, self.work)
        self.assertIn(" keep\n-old\n+new\n keep", d)
        d = call_diff("write_file", {"path": str(self.root / "out.txt"), "content": "hi\n"}, self.work)
        self.assertIn("+hi", d)


class Harness(unittest.TestCase):
    """真写文件工具（撤销日志、BlobStore）+ 子 Agent + 假模型的任务管理器，再挂一个 HTTP 服务。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.work = self.root / "w"
        self.work.mkdir()
        self.blobs = DirBlobStore(self.home / "blobs")
        self.script: list = []

        def factory(meta, store, sink, on_delta):
            model = FakeModel(self.script)
            tools = ToolBox(meta.workdir).add_write_tools(meta.id, self.blobs, None)
            parent: list = []
            subs = SubAgents(session="ledger", store=store, model=model, root=Path(meta.workdir).resolve(),
                             sandbox=None, blobs=self.blobs, approver_info=True, undo_session=meta.id,
                             approver=lambda w, info: parent[0].ask_up(w, info, poll=0.02))
            tools.tools["task"] = subs.tool()
            from weaver.todo import tool as todo_tool
            tools.tools["todo_write"] = todo_tool()
            policy = PermissionPolicy(meta.workdir, interactive=True)
            policy.revoked = lambda: revoked_keys(store.root)
            parent.append(Runner("ledger", store, model, tools, policy, "SYS", sink=sink, on_delta=on_delta))
            return parent[0]

        self.mgr = TaskManager(TaskStore(self.home / "tasks", scratch=self.root / "s"), factory,
                               uploads=Uploads(self.home, self.blobs))
        self.server = DaemonServer(self.mgr, settings=Settings(self.home))
        self.server.start()
        self.c = Client(self.server.port, self.server.token)

    def tearDown(self):
        self.server.stop()
        self.mgr.close(wait=True)
        self.tmp.cleanup()

    def run_task(self, text="做事"):
        t = self.mgr.create(text, workdir=str(self.work))
        self.assertTrue(self.mgr.wait_idle(t["id"], timeout=T))
        return t["id"]


class TaskDetailExtras(Harness):
    def test_changes_todos_steps_diff_and_undo(self):
        (self.work / "app.py").write_text("x = 1\n")
        self.script[:] = [
            reply("列清单", calls=[("t1", "todo_write", {"todos": [
                {"content": "读文件", "status": "completed"}, {"content": "改文件", "status": "cancelled"}]})]),
            reply("", calls=[("r1", "read_file", {"path": "app.py"})]),
            reply("改", calls=[("e1", "edit_file", {"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"}),
                              ("w1", "write_file", {"path": "new.txt", "content": "hello\nworld\n"})]),
            reply("改好了"),
        ]
        tid = self.run_task()
        st, d = self.c.call("GET", f"/v1/tasks/{tid}")
        self.assertEqual(st, 200)
        self.assertEqual([t["status"] for t in d["todos"]], ["completed", "cancelled"])
        ch = {c["rel"]: c for c in d["changes"]}
        self.assertEqual((ch["app.py"]["added"], ch["app.py"]["removed"]), (1, 1))
        self.assertTrue(ch["new.txt"]["created"])
        edit = next(s for s in d["steps"] if s["tool"] == "edit_file")
        self.assertIn("+x = 2", edit["diff"])

        st, diff = self.c.call("GET", f"/v1/tasks/{tid}/changes?path=app.py")
        self.assertEqual(st, 200)
        self.assertIn("-x = 1\n+x = 2", diff["diff"])

        st, r = self.c.call("POST", f"/v1/tasks/{tid}/undo", {"path": "app.py"})
        self.assertEqual(st, 200)
        self.assertEqual((self.work / "app.py").read_text(), "x = 1\n")
        self.assertTrue(next(c for c in r["changes"] if c["rel"] == "app.py")["undone"])
        # 账本里一条背景输入（不开启新的一轮），步骤里一条 note
        evs = self.mgr.events(tid)
        note = [e for e in evs if e["type"] == "InputReceived" and e.get("context_kind") == "undo"]
        self.assertEqual(len(note), 1)
        self.assertFalse(k.fold(evs).new_inputs())
        _, d = self.c.call("GET", f"/v1/tasks/{tid}")
        self.assertTrue(any(s["title"] == "你撤销了 app.py 的改动" for s in d["steps"]))
        self.assertEqual(d["task"]["status"], "done")

        st, err = self.c.call("POST", f"/v1/tasks/{tid}/undo", {"path": "app.py"})
        self.assertEqual(st, 409)
        st, _ = self.c.call("POST", f"/v1/tasks/{tid}/undo", {"path": "nope.py"})
        self.assertEqual(st, 404)

    def test_undo_refuses_when_file_changed_later(self):
        (self.work / "a.txt").write_text("v1\n")
        self.script[:] = [reply("", calls=[("r", "read_file", {"path": "a.txt"})]),
                          reply("", calls=[("w", "write_file", {"path": "a.txt", "content": "v2\n"})]),
                          reply("好了")]
        tid = self.run_task()
        (self.work / "a.txt").write_text("v3 by you\n")
        _, d = self.c.call("GET", f"/v1/tasks/{tid}")
        self.assertEqual(d["changes"][0]["why"], "之后又被改过")
        st, err = self.c.call("POST", f"/v1/tasks/{tid}/undo", {"path": "a.txt"})
        self.assertEqual(st, 409)
        self.assertEqual((self.work / "a.txt").read_text(), "v3 by you\n")

    def test_approval_wait_has_diff(self):
        outside = self.root / "outside.txt"
        outside.write_text("old line\n")
        self.script[:] = [reply("", calls=[("r", "read_file", {"path": str(outside)})]),
                          reply("", calls=[("w", "edit_file", {"path": str(outside), "old_string": "old line",
                                                                "new_string": "new line"})]),
                          reply("好了")]
        t = self.mgr.create("改外面的文件", workdir=str(self.work))
        self.assertTrue(until(lambda: self.mgr.waits()))
        w = self.mgr.waits()[0]
        self.assertIn("-old line\n+new line", w["diff"])
        self.mgr.answer(w["id"], "deny")
        self.assertTrue(self.mgr.wait_idle(t["id"], timeout=T))


class SubAgentProcess(Harness):
    def test_task_step_links_to_sub_ledger_and_agent_endpoint(self):
        self.script[:] = [
            reply("派个子 Agent", calls=[("p1", "task", {"description": "改文件", "prompt": "建 a.txt", "mode": "general"})]),
            reply("我来建", calls=[("k1", "write_file", {"path": "a.txt", "content": "made\n"})]),   # 子 Agent
            reply("建好了"),                                                                          # 子 Agent 汇报
            reply("完成"),
        ]
        tid = self.run_task()
        _, d = self.c.call("GET", f"/v1/tasks/{tid}")
        step = next(s for s in d["steps"] if s["tool"] == "task")
        self.assertTrue(step["sub"].startswith("ledger--"))
        st, a = self.c.call("GET", f"/v1/tasks/{tid}/agents/{step['sub']}")
        self.assertEqual(st, 200)
        self.assertEqual((a["title"], a["status"], a["final"]), ("改文件", "done", "建好了"))
        self.assertTrue(any(s["tool"] == "write_file" for s in a["steps"]))
        # 子 Agent 的改动记在主任务的撤销日志里
        self.assertEqual([c["rel"] for c in d["changes"]], ["a.txt"])
        self.assertTrue((self.work / ".weaver" / "undo" / f"{tid}.jsonl").exists())
        self.assertFalse((self.work / ".weaver" / "undo" / "ledger.jsonl").exists())
        st, _ = self.c.call("GET", f"/v1/tasks/{tid}/agents/ledger--nope")
        self.assertEqual(st, 404)
        st, _ = self.c.call("GET", f"/v1/tasks/{tid}/agents/other")
        self.assertEqual(st, 404)


class Archive(Harness):
    def test_list_detail_restore(self):
        self.script[:] = [reply("结论：好")]
        tid = self.run_task("归档我")
        self.assertEqual(self.c.call("DELETE", f"/v1/tasks/{tid}")[0], 204)
        _, lst = self.c.call("GET", "/v1/tasks")
        self.assertEqual(lst["tasks"], [])
        _, arch = self.c.call("GET", "/v1/tasks?archived=1")
        self.assertEqual([(t["id"], t["archived"], t["status"]) for t in arch["tasks"]], [(tid, True, "done")])
        st, d = self.c.call("GET", f"/v1/tasks/{tid}?archived=1")
        self.assertEqual((st, d["final"]), (200, "结论：好"))
        self.assertEqual(self.c.call("GET", f"/v1/tasks/{tid}")[0], 404)
        st, s = self.c.call("POST", f"/v1/tasks/{tid}/restore")
        self.assertEqual((st, s["id"], s["status"]), (200, tid, "done"))
        _, lst = self.c.call("GET", "/v1/tasks")
        self.assertEqual([t["id"] for t in lst["tasks"]], [tid])
        self.assertEqual(self.c.call("GET", "/v1/tasks?archived=1")[1]["tasks"], [])
        # 恢复后能接着说
        self.script[:] = [reply("又好了")]
        self.mgr.input(tid, "再来")
        self.assertTrue(self.mgr.wait_idle(tid, timeout=T))
        self.assertEqual(self.mgr.summary(tid)["now"], "又好了")
        self.assertEqual(self.c.call("POST", "/v1/tasks/nope/restore")[0], 404)

    def test_archived_twice_lists_latest(self):
        self.script[:] = [reply("一")]
        tid = self.run_task()
        self.mgr.archive(tid)
        self.mgr.restore(tid)
        time.sleep(1.1)                              # 归档目录名精确到秒
        self.mgr.archive(tid)
        self.assertEqual(len(self.mgr.archived()), 1)


class AlwaysRules(Harness):
    def test_revoke_and_regrant(self):
        self.script[:] = [reply("", calls=[("b1", "bash", {"command": "touch one.txt"})]), reply("一")]
        t = self.mgr.create("跑命令", workdir=str(self.work))
        tid = t["id"]
        self.assertTrue(until(lambda: self.mgr.waits()))
        self.mgr.answer(self.mgr.waits()[0]["id"], "always")
        self.assertTrue(self.mgr.wait_idle(tid, timeout=T))
        _, p = self.c.call("GET", "/v1/settings/permissions")
        self.assertEqual([(r["key"], r["kind"], r["task"]) for r in p["rules"]], [("touch one.txt", "bash", tid)])
        self.assertTrue(p["builtin"])

        # 还生效：同样的命令不问
        self.script[:] = [reply("", calls=[("b2", "bash", {"command": "touch one.txt"})]), reply("二")]
        self.mgr.input(tid, "再跑")
        self.assertTrue(self.mgr.wait_idle(tid, timeout=T))
        self.assertEqual(self.mgr.waits(), [])

        st, _ = self.c.call("DELETE", "/v1/settings/permissions/rules", {"task": tid, "key": "touch one.txt"})
        self.assertEqual(st, 204)
        self.assertEqual(self.c.call("GET", "/v1/settings/permissions")[1]["rules"], [])
        self.assertEqual(self.c.call("DELETE", "/v1/settings/permissions/rules",
                                     {"task": tid, "key": "touch one.txt"})[0], 404)

        # 撤销后：重新问；再点“总是允许”就又生效
        self.script[:] = [reply("", calls=[("b3", "bash", {"command": "touch one.txt"})]), reply("三")]
        self.mgr.input(tid, "又跑")
        self.assertTrue(until(lambda: self.mgr.waits()))
        self.mgr.answer(self.mgr.waits()[0]["id"], "always")
        self.assertTrue(self.mgr.wait_idle(tid, timeout=T))
        self.assertEqual(revoked_keys(self.mgr.store.dir(tid)), set())
        self.assertEqual(len(self.c.call("GET", "/v1/settings/permissions")[1]["rules"]), 1)


class TrustedProjects(unittest.TestCase):
    def test_list_and_forget(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "tasks").mkdir()
            (home / "trusted-skills.json").write_text(json.dumps({"/p/a": "x"}))
            (home / "trusted-mcp.json").write_text(json.dumps({"/p/a::github": "y", "/p/b::fs": "z"}))
            (home / "tasks" / ".trust-denied.json").write_text(json.dumps({"/p/c": "d"}))
            got = permits.projects(home)
            self.assertEqual([(p["root"], p["what"], p["state"]) for p in got],
                             [("/p/a", ["skill", "MCP 服务器"], "trusted"), ("/p/b", ["MCP 服务器"], "trusted"),
                              ("/p/c", [], "denied")])
            permits.forget(home, "/p/a")
            permits.forget(home, "/p/c")
            self.assertEqual([p["root"] for p in permits.projects(home)], ["/p/b"])
            self.assertEqual(json.loads((home / "trusted-mcp.json").read_text()), {"/p/b::fs": "z"})
            with self.assertRaises(NotFound):
                permits.forget(home, "/p/zzz")


class MemoryPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def text(self, name, desc="说明", typ="feedback", body="用 pnpm。\n"):
        return f"---\nname: {name}\ndescription: {desc}\ntype: {typ}\n---\n\n{body}"

    def test_crud_and_rename(self):
        memories.save(self.home, "user", "", None, self.text("prefer-pnpm"))
        memories.save(self.home, "project", str(self.proj), None, self.text("api-base", typ="project"))
        got = memories.list_all(self.home, [str(self.proj), str(self.proj), "/does/not/exist"])
        self.assertEqual([(s["scope"], [i["name"] for i in s["items"]]) for s in got["scopes"]],
                         [("user", ["prefer-pnpm"]), ("project", ["api-base"])])
        self.assertIn("用 pnpm", memories.read(self.home, "user", "", "prefer-pnpm")["text"])
        # 改名：新的在，旧的归档
        new = memories.save(self.home, "user", "", "prefer-pnpm", self.text("use-pnpm", desc="新说明"))
        self.assertEqual(new, "use-pnpm")
        names = [i["name"] for i in memories.list_all(self.home, [])["scopes"][0]["items"]]
        self.assertEqual(names, ["use-pnpm"])
        self.assertIn("use-pnpm", (self.home / "memory" / "MEMORY.md").read_text())
        memories.remove(self.home, "user", "", "use-pnpm")
        self.assertEqual(memories.list_all(self.home, [])["scopes"][0]["items"], [])
        with self.assertRaises(NotFound):
            memories.remove(self.home, "user", "", "use-pnpm")

    def test_validation(self):
        with self.assertRaises(BadRequest):
            memories.save(self.home, "user", "", None, "没有 frontmatter")
        with self.assertRaises(BadRequest):
            memories.save(self.home, "user", "", None, self.text("k", body="token = sk-abcdefghijklmnopqrstuv\n"))
        with self.assertRaises(BadRequest):
            memories.save(self.home, "user", "", None, self.text("k", typ="weird"))
        memories.save(self.home, "user", "", None, self.text("a"))
        with self.assertRaises(BadRequest):
            memories.save(self.home, "user", "", None, self.text("a"))
        with self.assertRaises(BadRequest):
            memories.save(self.home, "project", "/no/such/dir", None, self.text("b"))

    def test_http(self):
        mgr = TaskManager(TaskStore(self.home / "tasks", scratch=Path(self.tmp.name) / "s"), lambda *a: None)
        server = DaemonServer(mgr, settings=Settings(self.home))
        server.start()
        try:
            c = Client(server.port, server.token)
            st, r = c.call("POST", "/v1/settings/memory", {"scope": "user", "text": self.text("x1")})
            self.assertEqual((st, r["name"]), (201, "x1"))
            st, r = c.call("GET", "/v1/settings/memory")
            self.assertEqual(r["scopes"][0]["items"][0]["name"], "x1")
            self.assertIn("name:", r["template"])
            st, r = c.call("GET", "/v1/settings/memory/x1?scope=user")
            self.assertIn("用 pnpm", r["text"])
            st, r = c.call("PUT", "/v1/settings/memory/x1?scope=user", {"text": self.text("x1", body="改了\n")})
            self.assertEqual(st, 200)
            self.assertEqual(c.call("DELETE", "/v1/settings/memory/x1?scope=user")[0], 204)
            self.assertEqual(c.call("GET", "/v1/settings/memory/x1?scope=user")[0], 404)
        finally:
            server.stop()
            mgr.close(wait=True)


class ConflictNames(unittest.TestCase):
    def test_conflict_is_exported(self):
        self.assertTrue(issubclass(Conflict, Exception))


if __name__ == "__main__":
    unittest.main()
