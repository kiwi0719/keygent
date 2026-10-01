"""子 Agent 进阶：自定义类型、后台子 Agent、分叉、信任确认。全部离线。"""
from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

from weaver.agents import AgentTypes, ProjectTrust
from weaver.jobs import Jobs
from weaver.models import reply
from weaver.permissions import PermissionPolicy
from weaver.runner import Runner
from weaver.skills import Skills
from weaver.stores import MemoryBlobStore, MemoryEventStore
from weaver.subagent import SubAgents
from weaver.tools import ToolBox

T = 10


def until(pred, timeout=T):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Router:
    """假模型：按“是谁在问”分剧本。who(system, messages) -> 剧本名。记下每次请求（消息拷贝一份）。"""

    def __init__(self, scripts: dict, who):
        self.scripts, self.who, self.calls, self.lock = scripts, who, [], threading.Lock()
        self.name = "fake"

    def create(self, system, tools, messages, tool_choice="auto", on_delta=None, cache_key=None):
        key = self.who(system, messages)
        with self.lock:
            self.calls.append({"who": key, "system": system, "tools": tools, "messages": json.loads(json.dumps(messages))})
            step = self.scripts[key].pop(0)
        return step() if callable(step) else step


def write_agent(d: Path, name: str, tools: str = "", model: str = "", body: str = "你是测试员。"):
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {name} 的说明\n" + (f"tools: {tools}\n" if tools else "") + \
         (f"model: {model}\n" if model else "") + "---\n"
    (d / f"{name}.md").write_text(fm + body, encoding="utf-8")


class Types(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        (self.root / "proj").mkdir()
        self.proj = self.root / "proj"

    def tearDown(self):
        self.tmp.cleanup()

    def types(self, **kw):
        return AgentTypes(self.proj, home=self.home, claude_home=self.root / "claude", **kw)

    def test_discover_map_trust_render(self):
        write_agent(self.proj / ".claude/agents", "reviewer", tools="Read, Grep, Glob")
        write_agent(self.proj / ".weaver/agents", "runner", tools="Read, Bash, Task", model="cheap-model")
        write_agent(self.home / "agents", "writer")
        write_agent(self.home / "agents", "explore")                   # 保留名字，跳过
        (self.home / "agents/bad.md").write_text("没有 frontmatter", encoding="utf-8")
        t = self.types()
        found = t.discover()
        self.assertEqual(sorted(found), ["reviewer", "runner", "writer"])
        self.assertEqual(found["reviewer"].tools, ["read_file", "grep", "find_files"])
        self.assertTrue(found["reviewer"].readonly)
        self.assertEqual((found["runner"].tools, found["runner"].model, found["runner"].readonly),
                         (["read_file", "bash"], "cheap-model", False))                 # task 被去掉
        self.assertIsNone(found["writer"].tools)
        self.assertEqual(sorted(t.available()), ["writer"])                             # 项目级没信任
        pt = ProjectTrust(Skills(self.proj, home=self.home, claude_home=self.root / "claude"), t)
        self.assertEqual(sorted(pt.pending()["agents"]), ["reviewer", "runner"])
        pt.trust()
        self.assertIsNone(pt.pending())
        self.assertIn("- reviewer：reviewer 的说明（只读，可以并行、可以放后台）", t.render())
        self.assertEqual(self.types(project=False).discover().keys(), {"writer"})       # 临时目录：只有用户级


class SubBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "a.py").write_text("print(1)\n")
        self.store, self.blobs = MemoryEventStore(), MemoryBlobStore()
        self.notes: list[str] = []

    def tearDown(self):
        self.jobs.close() if hasattr(self, "jobs") else None
        self.tmp.cleanup()

    def make(self, model, types=None, model_for=None, jobs=True):
        self.jobs = Jobs(self.root, self.root / "jobs", notify=self.notes.append) if jobs else None
        box = ToolBox(self.root).add_write_tools("p", self.blobs, None, jobs=self.jobs)
        sub = SubAgents(session="p", store=self.store, model=model, root=self.root, sandbox=None, blobs=self.blobs,
                        agent_types=types, model_for=model_for, jobs=self.jobs,
                        fork_source={"model": model, "system": "父 SYS", "tools": box})
        box.tools["task"] = sub.tool()
        box.on_interrupt.append(sub.cancel_all)
        box.on_interrupt.append(lambda: self.jobs.cancel_agents())
        return Runner("p", self.store, model, box, PermissionPolicy(self.root), "父 SYS"), sub


class CustomTypes(SubBase):
    def test_type_tools_prompt_and_model(self):
        write_agent(self.root / "home/agents", "reader", tools="Read", model="cheap", body="你只读文件。")
        types = AgentTypes(self.root, home=self.root / "home", claude_home=self.root / "none")
        cheap = Router({"child": [reply(calls=[("k1", "read_file", {"path": "a.py"})]), reply("读完了：print(1)")]},
                       lambda s, m: "child")
        main = Router({"parent": [reply(calls=[("t1", "task", {"description": "读", "prompt": "读 a.py", "agent": "reader"})]),
                                  reply("好")]}, lambda s, m: "parent")
        made = []
        r, _ = self.make(main, types, model_for=lambda name: made.append(name) or cheap)
        r.submit("go")
        self.assertEqual(r.run().run.status, "done")
        self.assertEqual(made, ["cheap"])
        self.assertTrue(cheap.calls[0]["system"].startswith("你只读文件。"))
        self.assertEqual([t["name"] for t in cheap.calls[0]["tools"]], ["read_file"])
        tool_out = [m for m in main.calls[1]["messages"] if m["role"] == "tool"][0]["content"]
        self.assertIn("读完了：print(1)", tool_out)
        self.assertIn("子 Agent「读」（reader）：done", tool_out)

    def test_unknown_type(self):
        main = Router({"parent": [reply(calls=[("t1", "task", {"description": "x", "prompt": "x", "agent": "nope"})]),
                                  reply("好")]}, lambda s, m: "parent")
        r, _ = self.make(main, AgentTypes(self.root, home=self.root / "home", claude_home=self.root / "none"))
        r.submit("go")
        r.run()
        out = [e for e in self.store.load("p") if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]
        self.assertIn("没有名为 nope 的子 Agent 类型", out["output"])


class Background(SubBase):
    def who(self, system, messages):
        return "parent" if system == "父 SYS" else "child"

    def test_background_notify_and_wait(self):
        gate = threading.Event()
        model = Router({"parent": [reply(calls=[("t1", "task", {"description": "慢查", "prompt": "查",
                                                                 "background": True})]),
                                   reply("已经派到后台了")],
                        "child": [lambda: (gate.wait(T), reply("查到了：在 a.py"))[1]]}, self.who)
        r, _ = self.make(model)
        r.submit("go")
        t0 = time.monotonic()
        self.assertEqual(r.run().run.status, "done")
        self.assertLess(time.monotonic() - t0, 2)                     # 主任务不等它
        out = [e for e in self.store.load("p") if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]["output"]
        self.assertIn("已派出，在后台运行", out)
        jid = re.search(r"后台子 Agent (a[0-9a-f]+)", out).group(1)
        self.assertIn("运行中", self.jobs.output(jid))
        gate.set()
        self.assertTrue(until(lambda: self.notes))
        self.assertIn(f"后台子 Agent {jid}（慢查）完成了，汇报：\n查到了：在 a.py", self.notes[0])

        self.notes.clear()                                             # job_wait 等到了：不再另外通知
        model.scripts["child"] = [lambda: (time.sleep(0.3), reply("第二次的结论"))[1]]
        model.scripts["parent"] = [reply(calls=[("t2", "task", {"description": "再查", "prompt": "查",
                                                                 "background": True})]), reply("好")]
        r.submit("再来")
        r.run()
        out = [e for e in self.store.load("p") if e["type"] == "ActionCompleted" and e["kind"] == "tool"][-1]["output"]
        jid2 = re.search(r"后台子 Agent (a[0-9a-f]+)", out).group(1)
        self.assertIn("第二次的结论", self.jobs.wait(jid2, 5))
        time.sleep(0.2)
        self.assertEqual(self.notes, [])

    def test_rules_limit_and_cancel(self):
        gate = threading.Event()
        model = Router({"parent": [reply(calls=[(f"t{i}", "task", {"description": f"查{i}", "prompt": "查",
                                                                    "background": True}) for i in range(4)] +
                                         [("g", "task", {"description": "改", "prompt": "改", "mode": "general",
                                                         "background": True})]),
                                   reply("好")],
                        "child": [lambda: (gate.wait(T), reply("x"))[1] for _ in range(3)]}, self.who)
        r, _ = self.make(model)
        r.submit("go")
        r.run()
        outs = [e["output"] for e in self.store.load("p") if e["type"] == "ActionCompleted" and e["kind"] == "tool"]
        self.assertEqual(sum("已派出" in o for o in outs), 3)
        self.assertTrue(any("最多同时 3 个" in o for o in outs))
        self.assertTrue(any("后台只能派只读的子 Agent" in o for o in outs))
        self.assertEqual(self.jobs.cancel_agents(), 3)                # 主任务被取消：一起取消，不通知
        gate.set()
        self.assertTrue(until(lambda: not self.jobs.running()))
        time.sleep(0.2)
        self.assertEqual(self.notes, [])
        self.assertTrue(all(j.status == "killed" for j in self.jobs.jobs.values()))

    def test_restart_marks_interrupted_and_notifies_later(self):
        (self.root / "jobs").mkdir()
        (self.root / "jobs/registry.json").write_text(json.dumps([{"id": "aold01", "command": "查资料", "started": 1,
                                                                    "kind": "agent", "status": "running"}]))
        held = []
        jobs = Jobs(self.root, self.root / "jobs")
        self.assertEqual(jobs.cleaned[0]["status"], "killed")
        jobs.notify = held.append                                      # 任务管理器接上通知后才补发
        self.assertIn("后台子 Agent aold01（查资料）weaverd 重启，后台子 Agent 中断了", held[0])


class Fork(SubBase):
    def who(self, system, messages):
        last = messages[-1]
        text = "".join(p.get("text", "") for p in last.get("content") or []) if isinstance(last.get("content"), list) else ""
        return "fork" if "分叉出来的子 Agent" in text or any(
            "分叉出来的子 Agent" in "".join(p.get("text", "") for p in (m.get("content") or []) if isinstance(p, dict))
            for m in messages if m["role"] == "user" and isinstance(m.get("content"), list)) else "parent"

    def test_fork_prefix_matches_parent(self):
        model = Router({
            "parent": [reply("先看看", calls=[("r1", "read_file", {"path": "a.py"})]),
                       reply("派个分叉", calls=[("t1", "task", {"description": "改", "prompt": "把 1 改成 2",
                                                              "mode": "general", "context": "fork"})]),
                       reply("好了")],
            "fork": [reply(calls=[("k1", "remember", {"name": "x", "type": "user", "description": "d", "content": "c"}),
                                  ("k2", "edit_file", {"path": "a.py", "old_string": "1", "new_string": "2"})]),
                     reply("改好了")]}, self.who)
        r, _ = self.make(model)
        r.submit("把 a.py 里的 1 改成 2，注意别动别的")
        self.assertEqual(r.run().run.status, "done")
        parent_req = [c for c in model.calls if c["who"] == "parent"][1]       # 发出 task 调用的那次请求
        fork_req = [c for c in model.calls if c["who"] == "fork"][0]
        self.assertEqual((fork_req["system"], fork_req["tools"]), (parent_req["system"], parent_req["tools"]))
        n = len(parent_req["messages"])
        self.assertEqual(json.dumps(fork_req["messages"][:n - 1], ensure_ascii=False),
                         json.dumps(parent_req["messages"][:n - 1], ensure_ascii=False))
        self.assertIn("把 1 改成 2", json.dumps(fork_req["messages"][n:], ensure_ascii=False))
        self.assertEqual((self.root / "a.py").read_text(), "print(2)\n")
        child = [s for s in self.store.sessions if s.startswith("p--")][0]
        outs = {e["action_id"]: e["output"] for e in self.store.load(child) if e["type"] == "ActionCompleted" and e["kind"] == "tool"}
        self.assertIn("分叉的子 Agent 不能用 remember", outs["k1"])
        first = self.store.load(child)[0]
        self.assertEqual((first["context_kind"], first["parent_session"]), ("fork", "p"))

    def test_fork_rules(self):
        model = Router({"parent": [reply(calls=[("t1", "task", {"description": "x", "prompt": "x", "context": "fork"}),
                                                ("t2", "task", {"description": "y", "prompt": "y", "mode": "general",
                                                                "context": "fork", "background": True})]),
                                   reply("好")]}, self.who)
        r, _ = self.make(model)
        r.submit("go")
        r.run()
        outs = [e["output"] for e in self.store.load("p") if e["type"] == "ActionCompleted" and e["kind"] == "tool"]
        self.assertIn("分叉（context=fork）只用于 mode=general", outs[0])
        self.assertIn("不能放后台", outs[1])


class TrustWait(unittest.TestCase):
    def test_daemon_trust_wait(self):
        from weaver.daemon.manager import TaskManager
        from weaver.daemon.tasks import TaskStore
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            proj = root / "proj"
            write_agent(proj / ".weaver/agents", "reviewer", tools="Read")
            home, claude = root / "home", root / "claude"

            def factory(meta, store, sink, on_delta):
                from weaver.models import FakeModel
                from weaver.policy import Policy
                r = Runner("ledger", store, FakeModel([reply("好")] * 5), ToolBox(meta.workdir), Policy(), "SYS",
                           sink=sink, on_delta=on_delta)
                r.project_trust = ProjectTrust(Skills(meta.workdir, home=home, claude_home=claude),
                                               AgentTypes(meta.workdir, home=home, claude_home=claude))
                return r
            mgr = TaskManager(TaskStore(root / "tasks", scratch=root / "s"), factory)
            t = mgr.create("x", workdir=str(proj))["id"]
            self.assertTrue(mgr.wait_idle(timeout=5))
            w = [x for x in mgr.waits() if x["kind"] == "trust"]
            self.assertEqual(len(w), 1)
            self.assertIn("子 Agent 类型：reviewer", w[0]["body"])
            self.assertEqual((mgr.summary(t)["status"], mgr.summary(t)["waiting"]), ("done", 1))   # 不挡着任务
            t2 = mgr.create("y", workdir=str(proj))["id"]
            self.assertTrue(mgr.wait_idle(timeout=5))
            self.assertEqual([x["task"] for x in mgr.waits()], [t])        # 同一个项目只问一次
            mgr.answer(w[0]["id"], "deny")
            self.assertEqual(mgr.waits(), [])                              # 拒绝对整个项目生效
            self.assertEqual(mgr.summary(t2)["waiting"], 0)
            write_agent(proj / ".weaver/agents", "tester")                  # 内容变了：再问
            w = mgr.waits()
            self.assertEqual(len(w), 1)
            mgr.answer(w[0]["id"], "allow")
            self.assertEqual(mgr.waits(), [])
            self.assertTrue((home / "trusted-agents.json").exists())
            mgr.close(wait=True)


if __name__ == "__main__":
    unittest.main()
