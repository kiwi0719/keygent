"""HTTP 接口 + SSE：每个路径、token、错误格式、附件、事件流（补发、去重、reset、慢客户端）、端到端。全部离线。"""
from __future__ import annotations

import http.client
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from weaver.daemon.events import EventBus, Subscriber
from weaver.daemon.manager import TaskManager
from weaver.daemon.server import DaemonServer
from weaver.daemon.tasks import TaskStore
from weaver.daemon.uploads import Uploads
from weaver.models import FakeModel, reply
from weaver.runner import Runner

from .test_daemon_manager import Ask, box

T = 5


class Client:
    def __init__(self, port, token):
        self.port, self.token = port, token

    def call(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=T)
        h = {"Authorization": f"Bearer {self.token}", **(headers or {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and raw is None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read()
        c.close()
        return r.status, (json.loads(text) if text else None)


class Stream:
    """在后台线程里读 SSE，事件存进 self.events：(id, event, data)。"""
    def __init__(self, port, token, after=None):
        self.events, self.closed = [], threading.Event()
        q = f"?after={after}" if after is not None else ""
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=T * 4)
        self.conn.request("GET", "/v1/events" + q, headers={"Authorization": f"Bearer {token}"})
        self.resp = self.conn.getresponse()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        cur = {}
        try:
            for line in self.resp:
                line = line.decode().rstrip("\n")
                if not line:
                    if cur:
                        self.events.append((int(cur["id"]) if "id" in cur else None, cur.get("event"),
                                            json.loads(cur.get("data", "{}"))))
                    cur = {}
                    continue
                k, _, v = line.partition(": ")
                cur[k] = v
        except Exception:
            pass
        self.closed.set()

    def wait_for(self, pred, timeout=T):
        end = time.time() + timeout
        while time.time() < end:
            if any(pred(e) for e in list(self.events)):
                return True
            time.sleep(0.01)
        return False

    def close(self):
        self.conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.scripts, self.next_script = {}, []

        def factory(meta, store, sink, on_delta):
            script = self.scripts.pop(meta.title, None) or self.next_script
            return Runner("ledger", store, FakeModel(script), box(), Ask(), "SYS", sink=sink, on_delta=on_delta)
        self.mgr = TaskManager(TaskStore(root / "tasks", scratch=root / "scratch"), factory,
                               uploads=Uploads(root))
        self.srv = DaemonServer(self.mgr, token="secret")
        self.srv.start()
        self.c = Client(self.srv.port, "secret")
        self.streams = []

    def stream(self, after=None):
        s = Stream(self.srv.port, "secret", after)
        self.streams.append(s)
        return s

    def tearDown(self):
        for s in self.streams:
            s.close()
        self.srv.stop()
        self.mgr.close(wait=True)
        self.tmp.cleanup()

    def idle(self):
        self.assertTrue(self.mgr.wait_idle(timeout=T))


class Routes(Base):
    def test_auth_and_errors(self):
        self.assertEqual(Client(self.srv.port, "wrong").call("GET", "/v1/tasks")[0], 401)
        st, body = Client(self.srv.port, "wrong").call("GET", "/v1/tasks")
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertEqual(self.c.call("GET", "/v1/nope")[0], 404)
        self.assertEqual(self.c.call("PUT", "/v1/tasks")[0], 405)
        self.assertEqual(self.c.call("DELETE", "/v1/status")[0], 405)
        st, body = self.c.call("POST", "/v1/tasks", raw=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual((st, body["error"]["code"]), (400, "bad_request"))
        st, body = self.c.call("POST", "/v1/tasks", {"text": ""})
        self.assertEqual((st, body["error"]["message"]), (400, "text 不能为空"))
        st, body = self.c.call("POST", "/v1/tasks", {"text": "x", "workdir": "/definitely/not/here"})
        self.assertEqual(st, 400)
        self.assertIn("工作目录不存在", body["error"]["message"])
        self.assertEqual(self.c.call("GET", "/v1/tasks/nope")[0], 404)
        self.assertEqual(self.c.call("POST", "/v1/waits/nope", {"decision": "allow"})[0], 404)
        self.assertEqual(self.c.call("POST", "/v1/tasks", {"text": "x", "attachments": "b-1"})[0], 400)

    def test_lifecycle(self):
        self.next_script = [reply(calls=[("c1", "bash", {"command": "npm publish"})]), reply("## 发布完了\n细节")]
        st, t = self.c.call("POST", "/v1/tasks", {"text": "帮我发布"})
        self.assertEqual((st, t["title"]), (201, "帮我发布"))
        self.idle()
        st, body = self.c.call("GET", "/v1/tasks")
        self.assertEqual([x["status"] for x in body["tasks"]], ["waiting"])
        st, s = self.c.call("GET", "/v1/status")
        self.assertEqual((s["waiting"], s["first_wait"]["task_title"], s["version"]), (1, "帮我发布", "0.1"))
        w = self.c.call("GET", "/v1/waits")[1]["waits"][0]
        self.assertEqual(w["choices"], ["allow", "deny", "always", "edit"])
        st, body = self.c.call("POST", f"/v1/waits/{w['id']}", {"decision": "deny", "args": {"command": "x"}})
        self.assertEqual(st, 400)                                            # 改参数只能配 allow
        st, _ = self.c.call("POST", f"/v1/waits/{w['id']}", {"decision": "allow"})
        self.assertEqual(st, 200)
        self.idle()
        self.assertEqual(self.c.call("POST", f"/v1/waits/{w['id']}", {"decision": "allow"})[0], 409)
        st, d = self.c.call("GET", f"/v1/tasks/{t['id']}")
        self.assertEqual((d["task"]["status"], d["task"]["now"], d["final"]), ("done", "发布完了", "## 发布完了\n细节"))
        self.assertEqual([s["kind"] for s in d["steps"]], ["you", "step", "agent"])
        self.assertEqual(self.c.call("PATCH", f"/v1/tasks/{t['id']}", {"title": "发布 v2"})[1]["title"], "发布 v2")
        self.next_script = [reply("又好了")]
        st, s = self.c.call("POST", f"/v1/tasks/{t['id']}/input", {"text": "再来一次"})
        self.assertEqual(st, 202)
        self.idle()
        self.assertEqual(self.c.call("POST", f"/v1/tasks/{t['id']}/cancel")[0], 202)
        self.assertEqual(self.c.call("DELETE", f"/v1/tasks/{t['id']}")[0], 204)
        self.assertEqual(self.c.call("GET", "/v1/tasks")[1], {"tasks": []})

    def test_upload_and_attach(self):
        st, b = self.c.call("POST", "/v1/blobs", raw="客户ID,地区\nC1,华北\n".encode(),
                            headers={"Content-Type": "text/csv", "X-Filename": quote("客户名单.csv")})
        self.assertEqual((st, b["name"], b["mime"]), (201, "客户名单.csv", "text/csv"))
        st, png = self.c.call("POST", "/v1/blobs", raw=b"\x89PNG....", headers={"X-Filename": quote("../../图.png")})
        self.assertEqual(png["name"], "图.png")                               # 路径被去掉
        self.next_script = [reply("看到了")]
        st, t = self.c.call("POST", "/v1/tasks", {"text": "分析一下", "attachments": [b["id"], png["id"]]})
        self.idle()
        wd = Path(t["workdir"])
        self.assertEqual((wd / "附件/客户名单.csv").read_text(), "客户ID,地区\nC1,华北\n")
        first = next(e for e in self.mgr.events(t["id"]) if e["type"] == "InputReceived" and e["source"] == "user")
        self.assertEqual([p["type"] for p in first["content"]], ["text", "file", "image", "text"])
        self.assertIn("附件/客户名单.csv", first["content"][-1]["text"])
        st, body = self.c.call("POST", "/v1/tasks", {"text": "x", "attachments": ["b-0000000000"]})
        self.assertEqual((st, body["error"]["message"]), (400, "没有这个附件：b-0000000000"))

    def test_long_text_goes_to_grep(self):
        log = "".join(f"第 {i} 行 INFO 正常\n" for i in range(3000)) + "ERROR 连接被拒绝\n"
        st, b = self.c.call("POST", "/v1/blobs", raw=log.encode(),
                            headers={"Content-Type": "text/plain", "X-Filename": quote("粘贴的长文本.txt")})
        self.next_script = [reply("看到了")]
        t = self.c.call("POST", "/v1/tasks", {"text": "看看哪里报错", "attachments": [b["id"]]})[1]
        self.idle()
        self.assertEqual((Path(t["workdir"]) / "附件/粘贴的长文本.txt").read_text(), log)
        first = next(e for e in self.mgr.events(t["id"]) if e["type"] == "InputReceived" and e["source"] == "user")
        self.assertEqual([p["type"] for p in first["content"]], ["text", "text", "text"])   # 没有整份的 file 片段
        note = first["content"][1]["text"]
        self.assertIn("附件/粘贴的长文本.txt：3001 行", note)
        self.assertIn("grep", note)
        self.assertIn("第 11 行", note)
        self.assertNotIn("ERROR", note)
        self.assertLess(len(note), 1000)

    def test_error_capsule_only_unseen(self):
        def boom(_):
            raise RuntimeError("HTTP 500")
        self.next_script = [boom]
        t = self.c.call("POST", "/v1/tasks", {"text": "会出错"})[1]
        self.idle()
        self.assertEqual(self.c.call("GET", "/v1/status")[1]["error"]["task"], t["id"])
        self.c.call("GET", f"/v1/tasks/{t['id']}")                           # 看过了
        self.assertIsNone(self.c.call("GET", "/v1/status")[1]["error"])

    def test_info_file_is_private(self):
        p = self.srv.write_info(Path(self.tmp.name) / "daemon.json")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(json.loads(p.read_text())["port"], self.srv.port)
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")


class Events(Base):
    def test_stream_task_wait_step_and_replay(self):
        s = self.stream(after=0)
        self.next_script = [reply("先跑一下", calls=[("c1", "bash", {"command": "make"})]), reply("好了")]
        t = self.c.call("POST", "/v1/tasks", {"text": "构建"})[1]["id"]
        self.assertTrue(s.wait_for(lambda e: e[1] == "wait"))
        w = next(e for e in s.events if e[1] == "wait")[2]["wait"]
        self.assertEqual((w["task"], w["task_title"], w["title"]), (t, "构建", "跑命令 make"))
        self.c.call("POST", f"/v1/waits/{w['id']}", {"decision": "allow"})
        self.assertTrue(s.wait_for(lambda e: e[1] == "task" and e[2]["summary"]["status"] == "done"))
        kinds = [e[1] for e in s.events]
        self.assertIn("wait_closed", kinds)
        self.assertIn("delta", kinds)
        ids = [e[0] for e in s.events if e[0] is not None]
        self.assertEqual(ids, sorted(set(ids)))                              # 游标递增、不重复
        self.assertTrue(all(e[0] is None for e in s.events if e[1] == "delta"))
        step_statuses = [e[2]["step"]["status"] for e in s.events if e[1] == "step" and e[2]["step"]["kind"] == "step"]
        self.assertEqual(step_statuses[0], "running")                        # 同一个步骤先是在跑……
        self.assertIn("waiting", step_statuses)
        self.assertEqual(step_statuses[-1], "ok")                            # ……最后更新成完成

        mid = ids[len(ids) // 2]                                             # 断线重连：从中间补齐
        s2 = self.stream(after=mid)
        persisted = [e for e in s.events if e[0] is not None]
        self.assertTrue(s2.wait_for(lambda e: e[0] == ids[-1]))
        self.assertEqual([e[0] for e in s2.events if e[0] is not None], [e[0] for e in persisted if e[0] > mid])

    def test_reset_when_cursor_unknown(self):
        s = self.stream(after=999)                                           # 比最新的还大：服务重启过
        self.assertTrue(s.wait_for(lambda e: e[1] == "reset"))

    def test_archived_closes_waits(self):
        s = self.stream(after=0)
        self.next_script = [reply(calls=[("c1", "bash", {"command": "rm -rf build"})])]
        t = self.c.call("POST", "/v1/tasks", {"text": "清理"})[1]["id"]
        self.assertTrue(s.wait_for(lambda e: e[1] == "wait"))
        self.c.call("DELETE", f"/v1/tasks/{t}")
        self.assertTrue(s.wait_for(lambda e: e[1] == "archived"))
        self.assertTrue(any(e[1] == "wait_closed" for e in s.events))

    def test_slow_subscriber_is_dropped(self):
        sub = Subscriber(size=3)
        for i in range(5):
            sub.put((i, "task", {}))
        self.assertTrue(sub.dropped)
        bus = EventBus(self.mgr, buffer=3)
        for i in range(5):
            bus.publish("task", {"i": i})
        _, backlog, reset = bus.subscribe(after=0)                           # 太旧，缓冲区已经丢了
        self.assertTrue(reset)
        _, backlog, reset = bus.subscribe(after=3)
        self.assertEqual(([c for c, _, _ in backlog], reset), ([4, 5], False))
        bus.close()


if __name__ == "__main__":
    unittest.main()
