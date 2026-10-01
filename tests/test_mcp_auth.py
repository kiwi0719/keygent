"""MCP 登录（design/mcp2.md 第一节）：浏览器授权、令牌落盘与刷新、撤销后回到“要登录”、gh、设备码、设置页的选路。

用 tests/fixtures/oauth_mcp_server.py 起一个真的要登录的 MCP 服务器（SDK 自带的授权服务器实现），
测试代替浏览器去访问授权地址（它会直接重定向回本机的回调端口）。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from weaver.errors import BadRequest
from weaver.mcp import sdk_available
from weaver.mcp.config import parse

FIXTURE = str(Path(__file__).parent / "fixtures" / "oauth_mcp_server.py")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def poll(get, ok, timeout=20.0):
    deadline = time.monotonic() + timeout
    while True:
        v = get()
        if ok(v):
            return v
        if time.monotonic() > deadline:
            raise AssertionError(f"等了 {timeout} 秒还没满足：{v!r}")
        time.sleep(0.1)


def follow(url: str) -> None:
    """代替浏览器：打开授权地址，一路跟着重定向回到本机的回调端口。"""
    def go():
        with urllib.request.urlopen(url, timeout=10) as r:
            r.read()
    threading.Thread(target=go, daemon=True).start()


@unittest.skipUnless(sdk_available(), "需要 MCP SDK")
class Login(unittest.TestCase):
    ttl = "3600"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.revoke = Path(cls.tmp.name) / "revoke"
        cls.port = free_port()
        env = {**os.environ, "FAKE_TOKEN_TTL": cls.ttl, "FAKE_REVOKE_FILE": str(cls.revoke)}
        cls.proc = subprocess.Popen([sys.executable, FIXTURE, str(cls.port)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{cls.port}/mcp"
        poll(lambda: _listening(cls.port), bool, 15)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def setUp(self):
        self.revoke.unlink(missing_ok=True)
        self.home = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.cfg = parse("oauth", {"type": "http", "url": self.url}, "user", "t", {})

    def manager(self):
        from weaver.mcp.manager import McpManager
        m = McpManager([self.cfg], self.home / "mcp-logs", connect_timeout=15)
        self.addCleanup(m.close)
        return m

    def login(self, m):
        m.open_url = follow
        start = m.login("oauth", "browser")
        self.assertEqual(start["kind"], "browser")
        self.assertIn("/authorize?", start["url"])
        st = m.servers["oauth"]
        poll(lambda: (st.client, st.login), lambda v: v[0] is not None and v[1] is None)
        return st

    def test_needs_login_then_browser_login_then_call(self):
        m = self.manager()
        m.start()
        st = m.servers["oauth"]
        self.assertTrue(st.needs_login)
        self.assertIn("要登录", st.error)
        from weaver.mcp.tools import McpTools
        self.assertIn("按 ⌘L 登录", McpTools(m).render())
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("weaver-test").debug("开始")
            self.login(m)
        self.assertEqual(m.call("oauth", "whoami", {}), ("已登录的用户", False))
        f = self.home / "mcp-tokens.json"
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        token = json.loads(f.read_text())[self.url]["tokens"]["access_token"]
        self.assertNotIn(token, "\n".join(logs.output))                       # 令牌不进日志

    def test_login_survives_restart_and_revocation_needs_login_again(self):
        self.login(self.manager())
        m2 = self.manager()                                                    # 像 weaverd 重启：读回令牌
        m2.start()
        self.assertIsNotNone(m2.servers["oauth"].client)
        self.revoke.write_text("x")                                            # 授权服务器撤销了
        m3 = self.manager()
        m3.start()
        self.assertTrue(m3.servers["oauth"].needs_login)
        self.assertIsNone(m3.servers["oauth"].client)

    def test_second_login_cancels_first_and_closes_callback(self):
        m = self.manager()
        m.open_url = lambda url: None                                          # 第一次：没人去点
        first = m.login("oauth", "browser")
        port = int(first["url"].split("redirect_uri=")[1].split("%3A")[2].split("%2F")[0])
        self.assertTrue(_listening(port))
        self.login(m)                                                          # 再按一次 ⌘L
        poll(lambda: _listening(port), lambda up: not up, 5)                   # 回调端口都关了

    def test_settings_login_route_and_status(self):
        from weaver.mcp.pool import McpPool
        from weaver.settings.service import Settings
        pool = McpPool(self.home, idle=None)
        self.addCleanup(pool.close)
        pool.manager.open_url = follow
        s = Settings(self.home, pool=lambda: pool, claude_home=self.home / "claude",
                     desktop_json=self.home / "desk.json")
        s.mcp_add({"oauth": {"type": "http", "url": self.url}, "local": {"command": "x"}})
        item = poll(lambda: s.mcp_list()["servers"][0], lambda x: x["status"] == "needs_login")
        self.assertIn("要登录", item["error"])
        with self.assertRaisesRegex(BadRequest, "本地服务器不用登录"):
            s.mcp_login("local")
        self.assertEqual(s.mcp_login("oauth")["kind"], "browser")
        item = poll(lambda: s.mcp_list()["servers"][0], lambda x: x["status"] == "connected")
        self.assertIn("whoami", item["tools"])
        listed = json.dumps(s.mcp_list(), ensure_ascii=False)
        token = json.loads((self.home / "mcp-tokens.json").read_text())[self.url]["tokens"]["access_token"]
        self.assertNotIn(token, listed)                                        # 列表里不带令牌
        s.mcp_remove("oauth")
        self.assertNotIn(self.url, json.loads((self.home / "mcp-tokens.json").read_text()))


@unittest.skipUnless(sdk_available(), "需要 MCP SDK")
class Refresh(Login):
    ttl = "2"                                                                  # 令牌 2 秒就过期

    def test_expired_token_is_refreshed_after_restart(self):
        self.login(self.manager())
        time.sleep(2.5)
        m2 = self.manager()
        m2.start()
        self.assertIsNotNone(m2.servers["oauth"].client)                       # 先刷新，不要求重新授权
        self.assertEqual(m2.call("oauth", "whoami", {}), ("已登录的用户", False))

    # 父类的几个测试在这个服务器上也跑一遍没意义，跳过
    test_needs_login_then_browser_login_then_call = None
    test_login_survives_restart_and_revocation_needs_login_again = None
    test_second_login_cancels_first_and_closes_callback = None
    test_settings_login_route_and_status = None


@unittest.skipUnless(sdk_available(), "需要 MCP SDK")
class GhAndDevice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gh_header_and_missing_gh(self):
        from weaver.mcp import auth
        cfg = parse("gh", {"type": "http", "url": "https://api.githubcopilot.com/mcp/", "auth": "gh"}, "user", "t", {})
        tokens = auth.TokenFile(self.home / "t.json")
        with mock.patch.object(auth, "gh_token", lambda: "gho_fake"):
            headers, how = auth.http_auth(cfg, tokens)
        self.assertEqual((headers["Authorization"], how), ("Bearer gho_fake", None))
        with mock.patch.object(auth, "gh_token", lambda: None):
            with self.assertRaisesRegex(auth.NeedsLogin, "gh auth login"):
                auth.http_auth(cfg, tokens)

    def test_real_gh_token_reads_cli(self):
        from weaver.mcp import auth
        fake = self.home / "gh"
        fake.write_text("#!/bin/sh\n[ \"$1 $2\" = \"auth token\" ] && echo gho_from_cli\n")
        fake.chmod(0o755)
        with mock.patch.object(auth, "gh_path", lambda: str(fake)):
            self.assertEqual(auth.gh_token(), "gho_from_cli")

    def test_settings_github_uses_gh(self):
        from weaver.mcp import auth
        from weaver.mcp.pool import McpPool
        from weaver.settings import mcp as mcpfile
        from weaver.settings.service import Settings
        pool = McpPool(self.home, idle=None)
        self.addCleanup(pool.close)
        s = Settings(self.home, pool=lambda: pool, claude_home=self.home / "c", desktop_json=self.home / "d.json")
        with mock.patch.object(auth, "gh_token", lambda: "gho_fake"):
            self.assertEqual(s.mcp_add_preset("github", {}), "github")              # 常用服务器：有 gh 直接用
            self.assertEqual(mcpfile.load(self.home)["github"]["auth"], "gh")
            s.mcp_add({"gh2": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
                               "headers": {"Authorization": "Bearer ${NOPE}", "X-Keep": "1"}}})
            self.assertEqual(s.mcp_login("gh2"), {"kind": "done", "via": "gh"})
            self.assertEqual(mcpfile.load(self.home)["gh2"]["headers"], {"X-Keep": "1"})
        with mock.patch.object(auth, "gh_token", lambda: None), mock.patch.object(auth, "GITHUB_CLIENT_ID", ""), \
                mock.patch.dict(os.environ, {"WEAVER_GITHUB_CLIENT_ID": ""}):
            with self.assertRaisesRegex(BadRequest, "gh auth login"):
                s.mcp_login("gh2")

    def test_device_flow(self):
        from weaver.mcp import auth
        from weaver.mcp.manager import McpManager
        calls = []

        class FakeGitHub(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if self.path == "/mcp":                  # 登录完重连时打到这里：不是真的 MCP 服务器
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                calls.append((self.path, body))
                if self.path == "/login/device/code":
                    data = {"device_code": "dc1", "user_code": "ABCD-1234", "interval": 0.1, "expires_in": 60,
                            "verification_uri": "https://github.com/login/device"}
                else:
                    polls = sum(p == "/login/oauth/access_token" for p, _ in calls)
                    data = {"error": "authorization_pending"} if polls < 3 else {"access_token": "gho_device"}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        cfg = parse("github", {"type": "http", "url": f"{base}/mcp"}, "user", "t", {})
        m = McpManager([cfg], self.home / "mcp-logs", connect_timeout=5)
        self.addCleanup(m.close)
        opened = []
        m.open_url = opened.append
        with mock.patch.object(auth, "GITHUB_DEVICE_CODE", f"{base}/login/device/code"), \
                mock.patch.object(auth, "GITHUB_DEVICE_TOKEN", f"{base}/login/oauth/access_token"):
            start = m.login("github", "device", client_id="Ov23test")
            self.assertEqual((start["user_code"], opened), ("ABCD-1234", ["https://github.com/login/device"]))
            poll(lambda: m.tokens.bearer(cfg.url), lambda t: t == "gho_device")
        self.assertIn("client_id=Ov23test", calls[0][1])
        headers, how = auth.http_auth(cfg, m.tokens)
        self.assertEqual(headers["Authorization"], "Bearer gho_device")


class CliLogin(unittest.TestCase):
    """weaver mcp / weaver mcp login（不需要 SDK：接口的结果用假的）。"""

    def test_list_and_browser_login_waits_until_connected(self):
        from weaver.daemon.client import cmd_mcp
        states = iter(["logging_in", "logging_in", "connected"])
        listing = lambda: {"problem": None, "servers": [{"name": "notion", "status": next(states, "connected"),
                                                         "error": "", "tools": ["a"]}]}
        out = []
        code = cmd_mcp(["login", "notion"], listing, lambda n: {"kind": "browser", "url": "https://x/authorize"},
                       write=out.append, sleep=lambda s: None)
        self.assertEqual(code, 0)
        self.assertIn("https://x/authorize", out[0])
        self.assertEqual(out[-1], "notion：登录好了")
        out.clear()
        cmd_mcp([], lambda: {"problem": None, "servers": [{"name": "n", "status": "needs_login", "error": "", "tools": []}]},
                None, write=out.append)
        self.assertEqual(out, ["○ n  要登录：weaver mcp login n"])

    def test_device_and_gh_and_failure(self):
        from weaver.daemon.client import cmd_mcp
        out = []
        self.assertEqual(cmd_mcp(["login", "github"], None, lambda n: {"kind": "done", "via": "gh"}, write=out.append), 0)
        self.assertEqual(out, ["github：已用本机 gh 的登录"])
        out.clear()
        listing = lambda: {"servers": [{"name": "gh", "status": "needs_login", "error": "登录没完成：你在 GitHub 上拒绝了授权",
                                         "tools": []}]}
        code = cmd_mcp(["login", "gh"], listing, lambda n: {"kind": "device", "user_code": "ABCD-1234",
                                                             "verification_uri": "https://github.com/login/device"},
                       write=out.append, sleep=lambda s: None)
        self.assertEqual(code, 1)
        self.assertIn("ABCD-1234", out[0])
        self.assertIn("拒绝了授权", out[-1])


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
