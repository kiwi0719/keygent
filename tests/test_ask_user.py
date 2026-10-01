"""ask_user：工具和参数校验、权限规则（只有主 Agent 等人）、子 Agent 拿不到。见 design/ask-user.md。全部离线。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from weaver import kernel as k
from weaver.ask import ANSWERED, SKIPPED, parse_reply, tool, validate
from weaver.compaction import Compactor
from weaver.models import reply
from weaver.permissions import PermissionPolicy
from weaver.policy import Policy
from weaver.project import is_trimmed, project_with_seq
from weaver.stores import MemoryBlobStore, MemoryEventStore
from weaver.subagent import FORK_DENIED, SubAgents

from .test_kernel import check_invariants, make


def call(**args):
    return {"id": "c1", "name": "ask_user", "args": args}


class Tool_(unittest.TestCase):
    def test_validate(self):
        for bad in ({"question": ""}, {"question": "  "}, {}, {"question": "x", "options": ["a"]},
                    {"question": "x", "options": ["a"] * 5}, {"question": "x", "options": ["a", ""]},
                    {"question": "x", "options": "a,b"}):
            self.assertTrue(validate(bad), bad)
        for good in ({"question": "x"}, {"question": "x", "options": ["a", "b"]},
                     {"question": "x", "options": ["a", "b", "c", "d"]}, {"question": "x", "options": []}):
            self.assertIsNone(validate(good), good)

    def test_policy_waits(self):
        spec = PermissionPolicy(ask_user=True).wait_for(call(question="x"), k.State())
        self.assertIsInstance(spec, k.WaitSpec)
        self.assertEqual(spec.kind, "question")

    def test_policy_yes_executes(self):
        self.assertIsNone(PermissionPolicy(ask_user=True, yes=True).wait_for(call(question="x"), k.State()))
        self.assertEqual(tool().fn(question="x"), SKIPPED)

    def test_bad_args_no_wait(self):
        self.assertIsNone(PermissionPolicy(ask_user=True).wait_for(call(question="x", options=["a"]), k.State()))
        with self.assertRaises(ValueError):
            tool().fn(question="x", options=["a"])

    def test_only_main_agent_waits(self):
        """子 Agent 的规则（默认）不为 ask_user 等人：交给执行，分叉的在执行时被拒绝。"""
        self.assertIsNone(PermissionPolicy().wait_for(call(question="x"), k.State()))
        self.assertIn("ask_user", FORK_DENIED)

    def test_subagent_lacks_tool(self):
        with tempfile.TemporaryDirectory() as d:
            sub = SubAgents(session="p", store=MemoryEventStore(), model=None, root=Path(d), sandbox=None,
                            blobs=MemoryBlobStore())
            for mode in ("explore", "general"):
                self.assertNotIn("ask_user", sub.toolbox(mode).tools)


class Asks(Policy):
    """主 Agent 的样子：ask_user 等人，别的直接执行。"""
    def wait_for(self, call, state):
        return k.WaitSpec("question", "") if call["name"] == "ask_user" else None


def tool_result(store, call_id, session="s1"):
    return next(e for e in store.load(session) if e["type"] == "ActionCompleted" and e["kind"] == "tool"
                and e["action_id"] == call_id)


class Kernel_(unittest.TestCase):
    def ask(self, *extra, question="先看哪?"):
        return reply(calls=[("q1", "ask_user", {"question": question}), *extra])

    def test_answer_becomes_result(self):
        r, model, tools, store = make([self.ask(), reply("好，看华北")], policy=Asks())
        r.submit("分析一下")
        s = r.run()
        self.assertEqual([w.kind for w in s.open_waits], ["question"])
        r.resolve(s.open_waits[0].id, {"allow": True, "answer": "华北"})
        s = r.run()
        self.assertEqual(s.run.status, "done")
        ev = tool_result(store, "q1")
        self.assertEqual(ev["output"], ANSWERED.format("华北"))
        self.assertFalse(ev["is_error"])
        self.assertEqual(tools.executed, [])                # 工具函数不会被真的执行
        self.assertIn("用户回答：华北", str(model.calls[1]["messages"]))
        check_invariants(self, store.load("s1"))

    def test_skip_becomes_result(self):
        r, model, tools, store = make([self.ask(), reply("我自己定")], policy=Asks(),
                                      approver=lambda w: {"allow": True, "skip": True})
        r.submit("分析一下")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        ev = tool_result(store, "q1")
        self.assertEqual(ev["output"], SKIPPED)
        self.assertFalse(ev["is_error"])

    def test_question_alongside_other_call(self):
        r, model, tools, store = make([self.ask(("e1", "echo", {"x": "ok"})), reply("完")], policy=Asks())
        r.submit("分析一下")
        s = r.run()
        self.assertEqual(tools.executed, ["ok"])             # 别的调用不等问题，照常执行
        self.assertEqual(len(s.open_waits), 1)
        r.resolve(s.open_waits[0].id, {"allow": True, "answer": "华北"})
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertEqual(tool_result(store, "q1")["output"], "用户回答：华北")
        self.assertEqual(tool_result(store, "e1")["output"], "echo:ok")
        check_invariants(self, store.load("s1"))

    def test_two_questions_in_one_reply(self):
        r, model, tools, store = make([self.ask(("q2", "ask_user", {"question": "要图吗?"})), reply("完")],
                                      policy=Asks())
        r.submit("分析一下")
        s = r.run()
        self.assertEqual(sorted(w.payload["call_id"] for w in s.open_waits), ["q1", "q2"])
        for w, ans in zip(sorted(s.open_waits, key=lambda w: w.payload["call_id"]), ("华北", "要")):
            r.resolve(w.id, {"allow": True, "answer": ans})
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertEqual(tool_result(store, "q2")["output"], "用户回答：要")

    def test_cancel_closes_question(self):
        r, model, tools, store = make([self.ask(), reply("完")], policy=Asks())
        r.submit("分析一下")
        s = r.run()
        r.cancel("不做了")
        s = r.run()
        self.assertFalse(s.open_waits)
        self.assertTrue(all(w.resolved and w.closed for w in s.waits.values()))


class Cli(unittest.TestCase):
    """两个命令行：直接跑的 python -m weaver（__main__.ask），连 weaverd 的客户端（client.ask_wait）。"""
    OPTS = ["只看华北", "全国一起看"]

    def test_parse_reply(self):
        self.assertEqual(parse_reply("2", self.OPTS), "全国一起看")
        self.assertEqual(parse_reply(" 1 ", self.OPTS), "只看华北")
        self.assertEqual(parse_reply("自己写的", self.OPTS), "自己写的")
        self.assertEqual(parse_reply("3", self.OPTS), "3")                   # 超出选项：当原文
        self.assertEqual(parse_reply("1", []), "1")                         # 没有选项：数字也是原文
        self.assertIsNone(parse_reply("", self.OPTS))
        self.assertIsNone(parse_reply("   ", self.OPTS))

    def test_main_ask(self):
        from weaver import __main__ as cli
        w = k.Wait("w1", "question", {"call_id": "q1", "name": "ask_user",
                                      "args": {"question": "先看哪?", "options": self.OPTS}})
        printed = []
        with mock.patch("builtins.input", side_effect=["2", ""]), \
                mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(map(str, a)))):
            self.assertEqual(cli.ask(w), {"allow": True, "answer": "全国一起看"})
            self.assertEqual(cli.ask(w), {"allow": True, "skip": True})
        shown = "\n".join(printed)
        self.assertIn("问你：先看哪?", shown)
        self.assertIn("1. 只看华北", shown)

    def test_client_ask_wait(self):
        from weaver.daemon.client import ask_wait
        sent = []

        class Api:
            def call(self, method, path, body=None):
                sent.append((method, path, body))
        w = {"id": "w1", "kind": "question", "title": "先看哪?", "body": "", "options": self.OPTS,
             "choices": ["answer", "skip"]}
        out = []
        for typed in ("1", "华北和华东", ""):
            ask_wait(Api(), w, read=lambda _p, t=typed: t, write=out.append)
        self.assertEqual([b for _, _, b in sent], [{"decision": "answer", "note": "只看华北"},
                                                   {"decision": "answer", "note": "华北和华东"},
                                                   {"decision": "skip"}])
        self.assertTrue(all(p == "/v1/waits/w1" for _, p, _ in sent))
        self.assertIn("问你：先看哪?", "\n".join(out))


class Compaction_(unittest.TestCase):
    """用户的回答是工具结果，但它是用户亲口说的：裁剪不清它，摘要附带的“用户原话”里有它。"""
    def answered(self):
        r, model, tools, store = make([reply(calls=[("q1", "ask_user", {"question": "先看哪?"})]),
                                       reply(calls=[("e1", "echo", {"x": "大段输出"})]), reply("完")],
                                      policy=Asks(), approver=lambda w: {"allow": True, "answer": "只看华北"})
        r.submit("分析一下")
        r.run()
        return store.load("s1")

    def test_trim_keeps_answer(self):
        events = self.answered()
        last = events[-1]["seq"]
        events.append({"type": "ActionCompleted", "kind": "compact", "seq": last + 1, "id": "c",
                       "output": {"mode": "trim", "upto_seq": last}})
        tools = {m["call_id"]: m["content"] for _, m in project_with_seq(events) if m["role"] == "tool"}
        self.assertEqual(tools["q1"], "用户回答：只看华北")
        self.assertTrue(is_trimmed(tools["e1"]))

    def test_user_quotes_include_answers(self):
        quotes = Compactor().user_quotes(self.answered(), 10 ** 9)
        self.assertIn("分析一下", quotes)
        self.assertIn("先看哪?", quotes)
        self.assertIn("只看华北", quotes)


if __name__ == "__main__":
    unittest.main()
