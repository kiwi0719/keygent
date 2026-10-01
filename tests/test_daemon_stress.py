"""并发压测：20 个任务、6 个客户端线程经 HTTP 随机地新建、插话、放行 / 拒绝、取消、归档、查询，
外加一条 SSE 连接。结束后检查：没有 500、没有没接住的异常、账本连续、内核不变量成立、游标递增、全部停下。

默认跑 STRESS_ROUNDS=300 轮；想跑更狠：WEAVER_STRESS=3000 python3 -m unittest tests.test_daemon_stress
"""
from __future__ import annotations

import itertools
import logging
import os
import random
import threading
import time
import unittest

from weaver import kernel as k
from weaver.models import reply
from weaver.stores import JsonlEventStore

from .test_daemon_http import Base
from .test_kernel import check_invariants

ROUNDS = int(os.environ.get("WEAVER_STRESS") or 300)
CLIENTS = 6
TASKS = 20
OK = {200, 201, 202, 204, 400, 404, 409}


class Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


class Stress(Base):
    def test_random_clients(self):
        ids = itertools.count()
        rng_lock = threading.Lock()
        seed = int(os.environ.get("WEAVER_STRESS_SEED") or time.time())
        rng = random.Random(seed)

        def rand(fn):
            with rng_lock:
                return fn(rng)

        def model(_messages):                       # 一半概率调 bash（要审批），一半直接答完；偶尔慢一点
            time.sleep(rand(lambda r: r.choice([0, 0, 0, 0.002, 0.01])))
            if rand(lambda r: r.random()) < 0.5:
                n = next(ids)
                calls = [(f"c{n}-{j}", "bash", {"command": f"echo {n}-{j}"})
                         for j in range(rand(lambda r: r.choice([1, 1, 2, 3])))]
                return reply("要跑命令", calls=calls)
            return reply("做完了")
        self.next_script = model

        errors = Errors()
        logging.getLogger("weaverd").addHandler(errors)
        self.addCleanup(logging.getLogger("weaverd").removeHandler, errors)

        call = self.c.call

        def checked(*a, **kw):                      # 500 时把服务端给的原因一起记下
            st, body = call(*a, **kw)
            if st >= 500:
                bad.append((a[0], a[1], st, body))
            return st, body
        bad: list = []
        self.c.call = checked
        stream = self.stream(after=0)
        created = [self.c.call("POST", "/v1/tasks", {"text": f"任务{i}"})[1]["id"] for i in range(TASKS)]
        counts: dict[str, int] = {}

        def client(n):
            for _ in range(ROUNDS // CLIENTS):
                op = rand(lambda r: r.choices(
                    ["answer", "input", "cancel", "create", "list", "detail", "status", "archive", "waits"],
                    [30, 15, 8, 4, 10, 10, 10, 2, 11])[0])
                tid = rand(lambda r: r.choice(created))
                try:
                    if op == "answer":
                        st, body = self.c.call("GET", "/v1/waits")
                        if st != 200:
                            bad.append(("waits", st, body))
                            continue
                        ws = body["waits"]
                        if not ws:
                            continue
                        w = rand(lambda r: r.choice(ws))
                        dec = rand(lambda r: r.choice([c for c in w["choices"] if c != "edit"]))
                        st, _ = self.c.call("POST", f"/v1/waits/{w['id']}", {"decision": dec, "note": "压测"})
                    elif op == "input":
                        st, _ = self.c.call("POST", f"/v1/tasks/{tid}/input", {"text": f"插话 {n}"})
                    elif op == "cancel":
                        st, _ = self.c.call("POST", f"/v1/tasks/{tid}/cancel")
                    elif op == "create":
                        st, body = self.c.call("POST", "/v1/tasks", {"text": f"新任务 {n}"})
                    elif op == "list":
                        st, _ = self.c.call("GET", "/v1/tasks")
                    elif op == "detail":
                        st, _ = self.c.call("GET", f"/v1/tasks/{tid}")
                    elif op == "status":
                        st, _ = self.c.call("GET", "/v1/status")
                    elif op == "waits":
                        st, _ = self.c.call("GET", "/v1/waits")
                    else:
                        st, _ = self.c.call("DELETE", f"/v1/tasks/{tid}")
                except Exception as e:              # 连接级的错误也算失败
                    bad.append((op, repr(e)))
                    continue
                with rng_lock:
                    counts[f"{op} {st}"] = counts.get(f"{op} {st}", 0) + 1
                if st not in OK:
                    bad.append((op, st))

        threads = [threading.Thread(target=client, args=(i,)) for i in range(CLIENTS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        # 收尾：把剩下的等待都拒绝掉，让所有任务停下
        for _ in range(200):
            ws = self.c.call("GET", "/v1/waits")[1]["waits"]
            for w in ws:
                self.c.call("POST", f"/v1/waits/{w['id']}", {"decision": "stop" if w["kind"] == "stuck" else "deny"})
            self.assertTrue(self.mgr.wait_idle(timeout=10), "收尾时任务没停下")
            if not ws and not self.c.call("GET", "/v1/waits")[1]["waits"]:
                break

        msg = f"seed={seed} counts={counts}"
        if os.environ.get("WEAVER_STRESS_VERBOSE"):
            print("\n" + msg, "cursor", self.srv.bus.cursor)
        self.assertEqual(bad, [], msg)
        self.assertEqual(errors.records, [], msg)
        tasks = self.c.call("GET", "/v1/tasks")[1]["tasks"]
        stuck = [(t["id"], t["status"], self.mgr.busy(t["id"]),
                  [(e["type"], e.get("kind") or e.get("status") or e.get("source")) for e in self.mgr.events(t["id"])[-6:]])
                 for t in tasks if t["status"] in ("running", "queued", "waiting")]
        self.assertEqual(stuck, [], msg)
        for t in tasks:
            events = JsonlEventStore(self.mgr.store.dir(t["id"])).load("ledger")     # 从文件重读
            self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)), msg)
            check_invariants(self, events)
            self.assertFalse(k.fold(events).open_waits, msg)
        self.assertTrue(stream.wait_for(lambda e: e[0] == self.srv.bus.cursor, timeout=10), "事件流没追上")
        cursors = [e[0] for e in stream.events if e[0] is not None]
        self.assertEqual(cursors, list(range(1, len(cursors) + 1)), "游标不连续")


if __name__ == "__main__":
    unittest.main()
