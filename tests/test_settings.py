"""设置页后端（design/settings.md）：.env、模型、mcp.json、skills、/v1/settings 接口。"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from weaver.errors import BadRequest, Conflict, NotFound
from weaver.settings import mcp, model
from weaver.settings import skills as sk
from weaver.settings.envfile import EnvFile
from weaver.settings.service import Settings

SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def poll(get, ok, timeout=10.0):
    """每 0.1 秒取一次，满足就返回；超时让测试失败。"""
    deadline = time.monotonic() + timeout
    while True:
        value = get()
        if ok(value):
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"等了 {timeout} 秒还没满足：{value!r}")
        time.sleep(0.1)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.claude = self.tmp / "claude"
        self.env = self.home / ".env"

    def tearDown(self):
        self._tmp.cleanup()


class EnvAndModel(Tmp):
    def test_set_keeps_other_lines_and_mode(self):
        self.env.write_text("# 注释\nFOO=1\nWEAVER_MODEL=a\nWEAVER_MODEL=b\n")
        e = EnvFile.load(self.env)
        e.set("WEAVER_MODEL", "c")
        e.set("NEW", "x")
        e.set("FOO", None)
        e.write(self.env)
        self.assertEqual(self.env.read_text(), "# 注释\nWEAVER_MODEL=c\nNEW=x\n")
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(EnvFile.load(self.tmp / "none").get("X"), None)

    def test_read_masks_key_and_fills_preset_url(self):
        self.env.write_text("WEAVER_PROVIDER=deepseek\nWEAVER_MODEL=m\nWEAVER_API_KEY=sk-abcdef1234\n"
                            "WEAVER_MAX_RUNNING=6\n")
        r = model.read(self.env)
        self.assertEqual((r["url"], r["key"], r["advanced"]["max_running"], r["advanced"]["compact_at"]),
                         ("https://api.deepseek.com/v1", "••••1234", "6", ""))
        self.assertEqual(r["defaults"]["context_window"], "自动（按模型）")
        self.assertIsNone(model.read(self.tmp / "none")["key"])

    def test_save_rules(self):
        self.env.write_text("# 我的配置\nWEAVER_BASE_URL=https://openrouter.ai/api/v1\nWEAVER_MODEL=a\n"
                            "WEAVER_API_KEY=k1\nWEAVER_PROVIDER=openrouter\nOTHER=keep\n")
        model.save(self.env, {"url": "https://openrouter.ai/api/v1", "model": "b", "advanced": {"max_running": "8"}})
        e = EnvFile.load(self.env)
        self.assertEqual((e.get("WEAVER_API_KEY"), e.get("WEAVER_PROVIDER"), e.get("WEAVER_MAX_RUNNING"),
                          e.get("OTHER")), ("k1", "openrouter", "8", "keep"))       # 同一家沿用 key
        self.assertTrue(self.env.read_text().startswith("# 我的配置\n"))
        with self.assertRaisesRegex(BadRequest, "API Key"):
            model.save(self.env, {"url": "https://api.deepseek.com/v1", "model": "b"})   # 换了家必须填 key
        model.save(self.env, {"url": "http://localhost:11434/v1", "model": "q"})         # 本机可以没 key
        self.assertIsNone(EnvFile.load(self.env).get("WEAVER_PROVIDER"))
        model.save(self.env, {"url": "http://localhost:11434/v1", "model": "q", "advanced": {"max_running": ""}})
        self.assertIsNone(EnvFile.load(self.env).get("WEAVER_MAX_RUNNING"))              # 空 = 用默认
        for bad, msg in [({"url": "ftp://x", "model": "a"}, "http"),
                         ({"url": "https://x.com", "model": " "}, "模型名"),
                         ({"url": "http://localhost:1/v1", "model": "a", "advanced": {"compact_at": "-3"}}, "正整数"),
                         ({"url": "http://localhost:1/v1", "model": "a", "advanced": {"nope": "1"}}, "nope")]:
            with self.assertRaisesRegex(BadRequest, msg):
                model.save(self.env, bad)


class McpSettings(Tmp):
    def setUp(self):
        super().setUp()
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {})          # add_preset 会改 os.environ：测完还原
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file = self.home / "mcp.json"

    def test_add_keeps_unknown_fields_and_mode(self):
        self.file.write_text('{"x": 1, "mcpServers": {"a": {"command": "c", "trust": "readonly", "cwd": "/t", '
                             '"disabled": true}}}')
        self.assertEqual(mcp.add(self.home, {"b": {"url": "https://h/mcp",
                                                   "headers": {"Authorization": "Bearer tok"}}}), ["b"])
        data = json.loads(self.file.read_text())
        self.assertEqual((data["x"], data["mcpServers"]["a"], sorted(data["mcpServers"])),
                         (1, {"command": "c", "trust": "readonly", "cwd": "/t", "disabled": True}, ["a", "b"]))
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_conflict_overwrite_replace_rename_remove(self):
        mcp.add(self.home, {"a": {"command": "c"}})
        with self.assertRaisesRegex(Conflict, "同名.*a"):
            mcp.add(self.home, {"a": {"command": "d"}})
        mcp.add(self.home, {"a": {"command": "d"}}, overwrite=True)
        self.assertEqual(mcp.load(self.home)["a"], {"command": "d"})
        mcp.add(self.home, {"z": {"command": "z"}})
        with self.assertRaisesRegex(Conflict, "z"):
            mcp.replace(self.home, "a", {"command": "e"}, rename="z")
        self.assertEqual(mcp.replace(self.home, "a", {"command": "e"}, rename="b"), "b")
        mcp.remove(self.home, "b")
        self.assertEqual(list(mcp.load(self.home)), ["z"])
        archived = json.loads((self.home / "mcp-archive.json").read_text())
        self.assertEqual((archived[0]["name"], archived[0]["config"]), ("b", {"command": "e"}))
        self.assertEqual((self.home / "mcp-archive.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(NotFound):
            mcp.remove(self.home, "b")
        with self.assertRaises(NotFound):
            mcp.replace(self.home, "nope", {"command": "x"})
        for bad in ({"bad name": {"command": "x"}}, {"ok": "not-a-dict"}, {"ok": {"args": []}}):
            with self.assertRaises(BadRequest):
                mcp.add(self.home, bad)

    def test_broken_file_refuses_writes_and_is_untouched(self):
        self.file.write_text('{"mcpServers": {')
        with self.assertRaisesRegex(mcp.BrokenConfig, "mcp.json 读不了：第 1 行"):
            mcp.add(self.home, {"a": {"command": "c"}})
        with self.assertRaises(mcp.BrokenConfig):
            mcp.load(self.home)
        self.assertEqual(self.file.read_text(), '{"mcpServers": {')
        self.assertTrue(issubclass(mcp.BrokenConfig, Conflict))
        self.file.write_text('[1]')
        with self.assertRaisesRegex(mcp.BrokenConfig, "对象"):
            mcp.load(self.home)

    def test_parse_three_shapes(self):
        self.assertEqual(list(mcp.parse_text('{"mcpServers": {"gh": {"url": "https://x"}}}')), ["gh"])
        self.assertEqual(list(mcp.parse_text('{"a": {"command": "x"}, "b": {"command": "y"}}')), ["a", "b"])
        guess = lambda raw: list(mcp.parse_text(json.dumps(raw)))
        self.assertEqual(guess({"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}), ["playwright"])
        self.assertEqual(guess({"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}),
                         ["filesystem"])
        self.assertEqual(guess({"command": "uvx", "args": ["mcp-server-fetch"]}), ["fetch"])
        self.assertEqual(guess({"type": "http", "url": "https://api.githubcopilot.com/mcp/"}), ["githubcopilot"])
        self.assertEqual(guess({"command": "/usr/local/bin/my-tool"}), ["my-tool"])
        with self.assertRaisesRegex(BadRequest, "合法的 JSON"):
            mcp.parse_text("{nope")
        with self.assertRaisesRegex(BadRequest, "没认出"):
            mcp.parse_text('{"a": 1}')

    def test_import_sources(self):
        cc, desk = self.tmp / "claude.json", self.tmp / "desktop.json"
        cc.write_text('{"mcpServers": {"s": {"type": "stdio", "command": "x"}}, '
                      '"projects": {"/p": {"mcpServers": {"p": {"command": "y"}}}}}')
        self.assertEqual([(s["from"], list(s["servers"])) for s in mcp.import_sources(cc, desk)],
                         [("Claude Code", ["s"])])
        desk.write_text('{"mcpServers": {"d": {"command": "z"}}}')
        self.assertEqual([s["from"] for s in mcp.import_sources(cc, desk)], ["Claude Code", "Claude 桌面版"])
        cc.write_text("{broken")
        self.assertEqual([s["from"] for s in mcp.import_sources(cc, desk)], ["Claude 桌面版"])

    def test_presets_and_needs(self):
        from unittest import mock
        ids = [p["id"] for p in mcp.presets()]
        self.assertIn("github", ids)
        self.assertEqual(next(p for p in mcp.presets() if p["id"] == "github")["needs"], [])   # GitHub 改成登录
        fake = self.tmp / "presets.json"
        fake.write_text(json.dumps(mcp.presets() + [{
            "id": "tok", "name": "tok", "title": "要令牌的", "description": "", "needs": [
                {"id": "TOK_TOKEN", "kind": "env", "label": "某某令牌"}],
            "config": {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer ${TOK_TOKEN}"}}}]))
        with mock.patch.object(mcp, "PRESETS", fake):
            with self.assertRaisesRegex(BadRequest, "要填：某某令牌"):
                mcp.add_preset(self.home, "tok", {})
            self.assertEqual(mcp.add_preset(self.home, "tok", {"TOK_TOKEN": "t_x"}), "tok")
            self.assertIn("${TOK_TOKEN}", json.dumps(mcp.load(self.home)["tok"]))
            self.assertEqual((EnvFile.load(self.env).get("TOK_TOKEN"), os.environ.get("TOK_TOKEN")), ("t_x", "t_x"))
            with self.assertRaises(Conflict):
                mcp.add_preset(self.home, "tok", {"TOK_TOKEN": "t_y"})
        self.assertEqual(mcp.add_preset(self.home, "filesystem", {"dir": str(self.tmp)}), "filesystem")
        self.assertEqual(mcp.load(self.home)["filesystem"]["args"][-1], str(self.tmp))
        with self.assertRaises(NotFound):
            mcp.add_preset(self.home, "nope", {})


class SkillSettings(Tmp):
    T = "---\nname: {n}\ndescription: 说明\n---\n\n正文\n"

    def claude_skill(self, name):
        d = self.claude / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(self.T.format(n=name))

    def test_create_edit_rename_archive(self):
        self.assertEqual(sk.create_skill(self.home, self.claude, self.T.format(n="rel")), "rel")
        self.assertEqual(sk.read_skill(self.home, self.claude, "rel")["text"], self.T.format(n="rel"))
        (self.home / "skills/rel/run.sh").write_text("echo")
        self.assertEqual(sk.read_skill(self.home, self.claude, "rel")["files"], ["run.sh"])
        with self.assertRaisesRegex(Conflict, "已经有叫 rel"):
            sk.create_skill(self.home, self.claude, self.T.format(n="rel"))
        self.assertEqual(sk.save_skill(self.home, self.claude, "rel", self.T.format(n="release")), "release")
        self.assertTrue((self.home / "skills/release/run.sh").exists())          # 目录里的文件跟着走
        self.assertFalse((self.home / "skills/rel").exists())
        sk.save_skill(self.home, self.claude, "release", self.T.format(n="release") + "多一行\n")
        self.assertTrue((self.home / "skills/release/SKILL.md").read_text().endswith("多一行\n"))
        sk.remove_skill(self.home, self.claude, "release")
        self.assertEqual(len(list((self.home / "skills/.archive").iterdir())), 1)
        self.assertEqual([s for s in sk.list_skills(self.home, self.claude)["skills"] if s["source"] != "builtin"], [])
        with self.assertRaises(NotFound):
            sk.read_skill(self.home, self.claude, "release")
        with self.assertRaises(NotFound):
            sk.save_skill(self.home, self.claude, "release", self.T.format(n="release"))

    def test_validation_and_claude_readonly(self):
        self.claude_skill("pdf")
        cases = [("无 frontmatter", "--- 包起来"), (self.T.format(n="Bad Name"), "小写字母"),
                 ("---\nname: ok\ndescription: \n---\n", "description 不能空"),
                 (self.T.format(n="pdf"), "来自 Claude Code"), (self.T.format(n="a" * 65), "最长 64")]
        for text, msg in cases:
            with self.assertRaisesRegex((BadRequest, Conflict), msg):
                sk.create_skill(self.home, self.claude, text)
        sk.create_skill(self.home, self.claude, self.T.format(n="mine"))
        with self.assertRaisesRegex(Conflict, "来自 Claude Code"):              # 改名撞 Claude 的：原目录不动
            sk.save_skill(self.home, self.claude, "mine", self.T.format(n="pdf"))
        self.assertTrue((self.home / "skills/mine/SKILL.md").exists())
        with self.assertRaisesRegex(BadRequest, "只能看"):
            sk.save_skill(self.home, self.claude, "pdf", self.T.format(n="pdf"))
        with self.assertRaisesRegex(BadRequest, "只能看"):
            sk.remove_skill(self.home, self.claude, "pdf")
        listed = sk.list_skills(self.home, self.claude)["skills"]
        listed = [s for s in listed if s["source"] != "builtin"]
        self.assertEqual([(s["name"], s["source"], s["editable"]) for s in listed],
                         [("mine", "weaver", True), ("pdf", "claude", False)])
        self.assertFalse(sk.read_skill(self.home, self.claude, "pdf")["editable"])

    def test_builtin_readonly_but_overridable(self):
        builtin = [s for s in sk.list_skills(self.home, self.claude)["skills"] if s["source"] == "builtin"]
        if not builtin:
            self.skipTest("这个版本没有内置 skill")
        name = builtin[0]["name"]
        with self.assertRaisesRegex(BadRequest, "内置的 skill 只能看"):
            sk.save_skill(self.home, self.claude, name, self.T.format(n=name))
        self.assertEqual(sk.create_skill(self.home, self.claude, self.T.format(n=name)), name)   # 同名盖过内置
        rows = [(s["source"], s.get("shadowed", False)) for s in sk.list_skills(self.home, self.claude)["skills"]
                if s["name"] == name]
        self.assertEqual(rows, [("weaver", False), ("builtin", True)])
        self.assertTrue(sk.read_skill(self.home, self.claude, name)["editable"])

    def test_shadowed_and_broken_frontmatter(self):
        self.claude_skill("dup")
        (self.home / "skills/dup").mkdir(parents=True)
        (self.home / "skills/dup/SKILL.md").write_text(self.T.format(n="dup"))
        (self.home / "skills/broken").mkdir(parents=True)
        (self.home / "skills/broken/SKILL.md").write_text("乱写")
        listed = sk.list_skills(self.home, self.claude)
        rows = [(s["name"], s["source"], s.get("shadowed", False)) for s in listed["skills"] if s["source"] != "builtin"]
        self.assertEqual(rows, [("broken", "weaver", False), ("dup", "weaver", False), ("dup", "claude", True)])
        self.assertEqual(listed["problems"][0]["path"], str(self.home / "skills/broken/SKILL.md"))
        self.assertEqual(sk.read_skill(self.home, self.claude, "broken")["text"], "乱写")   # 坏的也能打开修
        self.assertEqual(sk.save_skill(self.home, self.claude, "broken", self.T.format(n="fixed")), "fixed")


class SettingsHttp(Tmp):
    def setUp(self):
        super().setUp()
        from weaver.daemon.manager import TaskManager
        from weaver.daemon.server import DaemonServer
        from weaver.daemon.tasks import TaskStore
        from .test_daemon_http import Client
        self.mgr = TaskManager(TaskStore(self.tmp / "tasks", scratch=self.tmp / "scratch"), lambda *a: None)
        self.settings = Settings(self.home, pool=lambda: None, claude_home=self.claude,
                                 desktop_json=self.tmp / "desk.json")
        self.srv = DaemonServer(self.mgr, token="secret", settings=self.settings)
        self.srv.start()
        self.c = Client(self.srv.port, "secret")

    def tearDown(self):
        self.srv.stop()
        self.mgr.close(wait=True)
        super().tearDown()

    def test_routes_roundtrip(self):
        c = self.c
        self.assertEqual(c.call("PUT", "/v1/settings/model", {"url": "http://localhost:1/v1", "model": "m"}),
                         (200, {"restart": True}))
        self.assertEqual(c.call("GET", "/v1/settings/model")[1]["model"], "m")
        self.assertEqual(c.call("PUT", "/v1/settings/model", {"url": "x", "model": "m"})[0], 400)
        st, b = c.call("POST", "/v1/settings/mcp/parse", {"text": '{"a": {"command": "x"}}'})
        self.assertEqual((st, list(b["servers"]), b["conflicts"]), (200, ["a"], []))
        self.assertEqual(c.call("POST", "/v1/settings/mcp", {"servers": b["servers"]}), (201, {"added": ["a"]}))
        st, b2 = c.call("POST", "/v1/settings/mcp", {"servers": b["servers"]})
        self.assertEqual((st, b2["error"]["code"]), (409, "conflict"))
        self.assertEqual(c.call("POST", "/v1/settings/mcp/parse", {"text": '{"a": {"command": "y"}}'})[1]["conflicts"],
                         ["a"])
        st, lst = c.call("GET", "/v1/settings/mcp")
        self.assertEqual([(x["name"], x["status"], x["error"]) for x in lst["servers"]],
                         [("a", "idle", "weaverd 没装 MCP SDK")])              # 测试里没给连接池
        self.assertEqual(c.call("PUT", "/v1/settings/mcp/a", {"config": {"command": "z"}, "rename": "b"}),
                         (200, {"name": "b"}))
        self.assertEqual(c.call("POST", "/v1/settings/mcp/b/reconnect")[0], 400)
        self.assertEqual(c.call("DELETE", "/v1/settings/mcp/b"), (204, None))
        self.assertEqual(c.call("DELETE", "/v1/settings/mcp/b")[0], 404)
        self.assertIn("github", [p["id"] for p in c.call("GET", "/v1/settings/mcp/presets")[1]["presets"]])
        self.assertEqual(c.call("POST", "/v1/settings/mcp/presets/filesystem", {"values": {}})[0], 400)
        self.assertEqual(c.call("GET", "/v1/settings/mcp/import"), (200, {"sources": []}))
        st, sk_ = c.call("POST", "/v1/settings/skills", {"text": "---\nname: x\ndescription: d\n---\n"})
        self.assertEqual((st, sk_["name"]), (201, "x"))
        self.assertEqual(c.call("GET", "/v1/settings/skills")[1]["skills"][0]["name"], "x")
        self.assertEqual(c.call("GET", "/v1/settings/skills/x")[1]["editable"], True)
        self.assertEqual(c.call("PUT", "/v1/settings/skills/x", {"text": "---\nname: y\ndescription: d\n---\n"}),
                         (200, {"name": "y"}))
        self.assertEqual(c.call("DELETE", "/v1/settings/skills/y"), (204, None))
        self.assertEqual(c.call("GET", "/v1/settings/skills/nope")[0], 404)
        self.assertEqual(c.call("GET", "/v1/settings/mcp/parse")[0], 405)
        c.call("POST", "/v1/settings/mcp", {"servers": {"presets": {"command": "x"}}})     # 名字和固定路径撞上
        st, b = c.call("POST", "/v1/settings/mcp/presets/reconnect")
        self.assertEqual((st, b["error"]["message"]), (400, "weaverd 没装 MCP SDK，连不了"))
        self.assertEqual(c.call("POST", "/v1/settings/mcp", {"servers": "x"})[0], 400)

    def test_broken_mcp_json(self):
        (self.home / "mcp.json").write_text("{oops")
        st, lst = self.c.call("GET", "/v1/settings/mcp")
        self.assertEqual(lst["servers"], [])
        self.assertIn("mcp.json 读不了", lst["problem"])
        st, b = self.c.call("POST", "/v1/settings/mcp", {"servers": {"a": {"command": "c"}}})
        self.assertEqual((st, b["error"]["code"]), (409, "conflict"))
        self.assertEqual((self.home / "mcp.json").read_text(), "{oops")

    def test_concurrent_adds_all_kept(self):
        threads = [threading.Thread(target=lambda i=i: self.c.call(
            "POST", "/v1/settings/mcp", {"servers": {f"s{i}": {"command": "x"}}})) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(json.loads((self.home / "mcp.json").read_text())["mcpServers"]), 10)

    def test_token_not_logged(self):
        with self.assertLogs("weaverd", "DEBUG") as cm:
            logging.getLogger("weaverd").debug("开始")
            self.c.call("POST", "/v1/settings/mcp",
                        {"servers": {"g": {"url": "https://x", "headers": {"Authorization": "Bearer SECRET123"}}}})
            self.c.call("POST", "/v1/settings/mcp", {"servers": {"g": {"url": "https://x"}}})   # 出错的那条也不打
        self.assertNotIn("SECRET123", "\n".join(cm.output))

    def test_no_settings_404(self):
        from weaver.daemon.server import DaemonServer
        from .test_daemon_http import Client
        srv = DaemonServer(self.mgr, token="t")
        srv.start()
        try:
            st, b = Client(srv.port, "t").call("GET", "/v1/settings/mcp")
            self.assertEqual((st, b["error"]["message"]), (404, "这个服务没开设置接口"))
        finally:
            srv.stop()


class SettingsLive(Tmp):
    def setUp(self):
        from weaver.mcp import sdk_available
        if not sdk_available():
            self.skipTest("需要 MCP SDK")
        super().setUp()

    def test_status_goes_connecting_to_connected(self):
        from weaver.mcp.pool import McpPool
        pool = McpPool(self.home)
        self.addCleanup(pool.close)
        s = Settings(self.home, pool=lambda: pool, claude_home=self.claude, desktop_json=self.tmp / "desk.json")
        s.mcp_add({"fake": {"command": sys.executable, "args": [SERVER]}})
        self.assertIn(s.mcp_list()["servers"][0]["status"], ("connecting", "connected"))
        item = poll(lambda: s.mcp_list()["servers"][0], lambda x: x["status"] == "connected", timeout=30)
        self.assertIn("echo", item["tools"])
        s.mcp_reconnect("fake")
        poll(lambda: s.mcp_list()["servers"][0], lambda x: x["status"] == "connected", timeout=30)
        s.mcp_add({"bad": {"command": "/nonexistent/x"}})
        bad = poll(lambda: s.mcp_list()["servers"][1], lambda x: x["status"] == "failed", timeout=30)
        self.assertIn("找不到命令", bad["error"])
        s.mcp_add({"envless": {"command": "x", "env": {"K": "${NOPE_NOT_SET}"}}})
        self.assertEqual(s.mcp_list()["servers"][2]["status"], "invalid")


class ReviewFixes(Tmp):
    """独立审查找出来的问题（design/settings.md 进度一节）。"""

    def test_max_running_read_after_env_loaded(self):
        from unittest import mock
        from weaver.daemon.__main__ import resolve_max_running
        with mock.patch.dict(os.environ, {"WEAVER_MAX_RUNNING": "7"}):
            self.assertEqual((resolve_max_running(None), resolve_max_running(2)), (7, 2))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEAVER_MAX_RUNNING", None)
            self.assertEqual(resolve_max_running(None), 4)

    def test_pool_key_changes_with_expanded_token(self):
        from weaver.mcp.config import parse
        from weaver.mcp.pool import McpPool
        pool = McpPool(self.home, idle=None)
        self.addCleanup(pool.close)
        raw = {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer ${TOK}"}}
        a = pool.register(parse("gh", raw, "user", "t", {"TOK": "bad"}), None)
        b = pool.register(parse("gh", raw, "user", "t", {"TOK": "good"}), None)
        self.assertNotEqual(a, b)                                      # 换了令牌：新连接，不沿用旧令牌
        self.assertEqual(pool.manager.servers[b].cfg.headers["Authorization"], "Bearer good")
        self.assertEqual(a, pool.register(parse("gh", raw, "user", "t", {"TOK": "bad"}), None))

    def test_create_skill_does_not_overwrite_mismatched_dir(self):
        d = self.home / "skills" / "foo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SkillSettings.T.format(n="foo-helper"))
        with self.assertRaisesRegex(Conflict, "已经有叫 foo 的目录"):
            sk.create_skill(self.home, self.claude, SkillSettings.T.format(n="foo"))
        self.assertIn("foo-helper", (d / "SKILL.md").read_text())

    def test_symlinked_files_stay_symlinks(self):
        dot = self.tmp / "dotfiles"
        dot.mkdir()
        (dot / "env").write_text("A=1\n")
        self.env.symlink_to(dot / "env")
        model.save(self.env, {"url": "http://localhost:1/v1", "model": "m"})
        self.assertTrue(self.env.is_symlink())
        self.assertIn("WEAVER_MODEL=m", (dot / "env").read_text())
        (dot / "mcp.json").write_text("{}")
        (self.home / "mcp.json").symlink_to(dot / "mcp.json")
        mcp.add(self.home, {"a": {"command": "x"}})
        self.assertTrue((self.home / "mcp.json").is_symlink())
        self.assertIn('"a"', (dot / "mcp.json").read_text())


if __name__ == "__main__":
    unittest.main()
