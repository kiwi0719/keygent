"""跨任务搜索：两种布局、同目录优先、归档、增量缓存、内存上限、取别的任务的原文、recall 工具、搜索接口。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from weaver.corpus import Corpus, daemon_sources, session_sources
from weaver.daemon.tasks import LEDGER, TaskStore
from weaver.tools.recall import tool as recall_tool


def ev(seq, text, source="user", ts=None):
    return {"type": "InputReceived", "seq": seq, "ts": ts or seq, "source": source,
            "content": [{"type": "text", "text": text}]}


def tool_ev(seq, out, ts=None):
    return {"type": "ActionCompleted", "kind": "tool", "seq": seq, "ts": ts or seq, "action_id": f"a{seq}", "output": out}


class Daemon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ts = TaskStore(root / "tasks", scratch=root / "scratch")
        self.q3 = self.ts.create("Q3 流失分析", workdir=self.tmp.name)
        self.other = self.ts.create("周报")
        self.write(self.q3.id, [ev(1, "分析华北流失", ts=100), tool_ev(2, "华北 阈值 定为 60 天", ts=101),
                                {"type": "InputReceived", "seq": 3, "source": "context",
                                 "content": [{"type": "text", "text": "华北 阈值 背景信息不搜"}]}])
        self.write(self.other.id, [ev(1, "华北 阈值 改成 90 天？", ts=200)])
        self.corpus = Corpus(daemon_sources(root / "tasks"))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, task_id, events, name=LEDGER):
        with open(self.ts.dir(task_id) / f"{name}.jsonl", "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_search_order_archive_and_get(self):
        hits = self.corpus.search("华北 阈值")
        self.assertEqual([(h["task_title"], h["seq"]) for h in hits], [("周报", 1), ("Q3 流失分析", 2)])   # 新的在前
        hits = self.corpus.search("华北 阈值", workdir=str(Path(self.tmp.name).resolve()))
        self.assertEqual(hits[0]["task_title"], "Q3 流失分析")                                          # 同目录优先
        self.ts.archive(self.other.id)
        self.assertEqual([h["task_title"] for h in self.corpus.search("华北 阈值")], ["Q3 流失分析"])
        self.assertEqual(len(self.corpus.search("华北 阈值", archived=True)), 2)
        src, e = self.corpus.get(self.q3.id[:6], 2)
        self.assertEqual((src.title, e[2], e[3]), ("Q3 流失分析", "工具结果", "华北 阈值 定为 60 天"))

    def test_incremental_and_subagent(self):
        self.corpus.search("x")
        path = self.ts.dir(self.q3.id) / "ledger.jsonl"
        doc = self.corpus._docs[path]
        first = doc.entries[0]
        self.write(self.q3.id, [tool_ev(4, "新加的内容 西南", ts=300)])
        self.write(self.q3.id, [tool_ev(1, "子 Agent 查到 西南 也有", ts=301)], name="ledger--abc")
        hits = self.corpus.search("西南")
        self.assertEqual([(h["sub"], h["seq"]) for h in hits], [("ledger--abc", 1), ("", 4)])
        self.assertIs(self.corpus._docs[path].entries[0], first)                   # 旧的没重读

    def test_memory_cap_evicts(self):
        small = Corpus(daemon_sources(Path(self.tmp.name) / "tasks"), max_chars=20)
        small.search("华北")
        self.assertEqual(len(small._docs), 1)
        self.assertEqual(len(small.search("华北 阈值")), 2)                        # 丢了的下次再读

    def test_recall_tool(self):
        t = recall_tool(lambda: [], self.corpus, current=self.q3.id, workdir=str(Path(self.tmp.name).resolve()))
        out = t.fn(query="华北 阈值", scope="all")
        self.assertIn("[本任务 · ", out)
        self.assertIn(f"[周报 · {self.other.id[:8]}", out)
        self.assertIn("华北 阈值 改成 90 天", t.fn(task=self.other.id, seq=1))
        self.assertIn("没有以 zzz 开头的任务", t.fn(task="zzz", seq=1))
        self.assertIn("scope", t.spec["parameters"]["properties"])


class Sessions(unittest.TestCase):
    def test_cli_layout(self):
        with tempfile.TemporaryDirectory() as d:
            s = Path(d) / "sessions"
            s.mkdir()
            (s / "abc.jsonl").write_text(json.dumps(ev(1, "修一下登录的报错")) + "\n", encoding="utf-8")
            (s / "abc--x1.jsonl").write_text(json.dumps(tool_ev(1, "登录 报错 在 auth.py")) + "\n", encoding="utf-8")
            srcs = session_sources(s, d)(False)
            self.assertEqual([(x.task, x.title, [p.name for p in x.files]) for x in srcs],
                             [("abc", "修一下登录的报错", ["abc.jsonl", "abc--x1.jsonl"])])
            hits = Corpus(session_sources(s, d)).search("登录 报错")
            self.assertEqual([h["sub"] for h in hits], ["", "abc--x1"])


class SearchApi(unittest.TestCase):
    def test_endpoint(self):
        from .test_daemon_http import Base
        from weaver.models import reply

        class T(Base):
            def runTest(inner):
                inner.mgr.corpus = Corpus(daemon_sources(inner.mgr.store.root))
                inner.next_script = [reply("华北的阈值定为 60 天")]
                inner.c.call("POST", "/v1/tasks", {"text": "华北阈值是多少"})
                inner.idle()
                st, body = inner.c.call("GET", "/v1/search?q=" + __import__("urllib.parse").parse.quote("华北 60"))
                inner.assertEqual(st, 200)
                inner.assertEqual([(r["kind"], r["task_title"]) for r in body["results"]], [("模型", "华北阈值是多少")])
                inner.assertEqual(inner.c.call("GET", "/v1/search?q=")[0], 400)
        r = unittest.TestResult()
        T().run(r)
        self.assertTrue(r.wasSuccessful(), r.failures + r.errors)


if __name__ == "__main__":
    unittest.main()
