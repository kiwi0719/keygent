"""MCP 第二期（design/mcp2.md 第二到第七节）在常驻服务里：服务器提问（表单、校验、拒绝、等人时不算超时、
任务取消）、借用模型（每次问、总是允许、用量记账）、图片给模型看、roots、工具清单中途变化、prompts。
用 tests/fixtures/fake_mcp_server.py（FAKE_MCP_V2=1）当真实的 stdio 服务器；需要 SDK，没装就跳过。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from weaver.errors import BadRequest
from weaver.mcp import sdk_available
from weaver.models import FakeModel, reply

SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
T = 30


def until(pred, timeout=T):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return None


@unittest.skipUnless(sdk_available(), "没装 MCP SDK")
class McpV2(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home, self.scratch, self.project = t / "home", t / "scratch", t / "proj"
        for d in (self.home, self.scratch, self.project):
            d.mkdir()
        fake = {"command": sys.executable, "args": [SERVER], "env": {"FAKE_MCP_V2": "1"}, "trust": "all"}
        (self.home / "mcp.json").write_text(json.dumps({"mcpServers": {"fake": fake}}))
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
        from weaver.daemon.uploads import Uploads
        model = FakeModel(script)
        model.base_url, model.max_tokens = "", 4096
        factory = RunnerFactory(self.home, model=model)
        m = TaskManager(TaskStore(self.home / "tasks", self.scratch), factory, uploads=Uploads(self.home, factory.blobs))
        self.addCleanup(factory.close)
        self.addCleanup(m.close)
        return m, factory, model

    @staticmethod
    def tool_result(call) -> object:
        return [x for x in call["messages"] if x["role"] == "tool"][-1]["content"]

    # ------------------------------------------------ 提问

    def test_elicitation_form_validation_and_pause(self):
        m, factory, model = self.manager([reply(calls=[("c1", "mcp__fake__deploy", {})]), reply("部署好了")])
        factory.mcp_pool().manager.call_timeout = 1                 # 等人回答的时间不算：下面等 2 秒也不超时
        task = m.create("部署", workdir=str(self.project))["id"]
        w = until(lambda: next((x for x in m.waits() if x["kind"] == "elicit"), None))
        self.assertIsNotNone(w)
        self.assertEqual(w["title"], "fake 问你：部署到哪个环境？")
        self.assertEqual(w["choices"], ["accept", "decline", "cancel"])
        fields = {f["name"]: f for f in w["fields"]}
        self.assertEqual((fields["env"]["type"], fields["env"]["options"], fields["env"]["required"]),
                         ("enum", ["staging", "prod"], True))
        self.assertEqual((fields["count"]["type"], fields["force"]["type"]), ("integer", "boolean"))
        self.assertEqual(m.summary(task)["status"], "waiting")
        self.assertEqual(m.status()["first_wait"]["kind"], "elicit")
        with self.assertRaises(BadRequest):                         # 必填的没填
            m.answer(w["id"], "accept", values={"count": "2"})
        with self.assertRaises(BadRequest):                         # 不在选项里
            m.answer(w["id"], "accept", values={"env": "dev"})
        time.sleep(2)
        m.answer(w["id"], "accept", values={"env": "staging", "count": "2", "force": True})
        self.assertTrue(m.wait_idle(task, T))
        self.assertEqual(self.tool_result(model.calls[1]), "deployed to staging x2 force=True")
        # 回答的值不进模型的上下文，只交给服务器（工具结果里服务器自己说了什么另说）
        self.assertNotIn("elicit", json.dumps(model.calls[1]["messages"], ensure_ascii=False))

    def test_elicitation_decline_and_cancel_with_task(self):
        m, factory, model = self.manager([reply(calls=[("c1", "mcp__fake__deploy", {})]), reply("没部署"),
                                          reply(calls=[("c2", "mcp__fake__deploy", {})]), reply("取消了")])
        task = m.create("部署", workdir=str(self.project))["id"]
        w = until(lambda: next((x for x in m.waits() if x["kind"] == "elicit"), None))
        m.answer(w["id"], "decline")
        self.assertTrue(m.wait_idle(task, T))
        self.assertEqual(self.tool_result(model.calls[1]), "not deployed: decline")
        # 等你回答时任务被取消：这件等待作废，服务器收到 cancel，调用正常结束，任务不挂住
        m.input(task, "再部署一次")
        until(lambda: next((x for x in m.waits() if x["kind"] == "elicit"), None))
        m.cancel(task)
        self.assertTrue(m.wait_idle(task, T))
        self.assertEqual(m.summary(task)["status"], "cancelled")
        self.assertEqual([x for x in m.waits() if x["kind"] == "elicit"], [])

    # ------------------------------------------------ 借用模型

    def test_sampling_asks_then_always(self):
        script = [reply(calls=[("c1", "mcp__fake__summarize", {"text": "很长的一段话"})]),
                  reply("短总结", usage=(100, 20)),                  # 借用模型那一次
                  reply("好"),
                  reply(calls=[("c2", "mcp__fake__summarize", {"text": "又一段"})]),
                  reply("第二次总结", usage=(100, 20)),
                  reply("又好")]
        m, factory, model = self.manager(script)
        task = m.create("总结", workdir=str(self.project))["id"]
        w = until(lambda: next((x for x in m.waits() if x.get("call", {}).get("name") == "sampling"), None))
        self.assertEqual(w["kind"], "approval")
        self.assertEqual(w["title"], "fake 想借用模型")
        self.assertIn("总结：很长的一段话", w["body"])
        self.assertIn("always", w["choices"])
        m.answer(w["id"], "always")
        self.assertTrue(m.wait_idle(task, T))
        self.assertEqual(model.calls[1]["tools"], [])                # 借用：不带工具
        self.assertEqual(self.tool_result(model.calls[2]), "summary: 短总结")
        self.assertGreaterEqual(m.detail(task, mark_seen=False)["usage"]["tokens"], 120)   # 用量算进这个任务
        rules = m.always_rules()
        self.assertEqual([r["key"] for r in rules], ["sampling:fake"])
        # 总是允许之后不再问
        m.input(task, "再总结")
        self.assertTrue(m.wait_idle(task, T))
        self.assertEqual(self.tool_result(model.calls[5]), "summary: 第二次总结")

    def test_sampling_denied(self):
        m, factory, model = self.manager([reply(calls=[("c1", "mcp__fake__summarize", {"text": "x"})]), reply("算了")])
        task = m.create("总结", workdir=str(self.project))["id"]
        w = until(lambda: next((x for x in m.waits() if x.get("call", {}).get("name") == "sampling"), None))
        m.answer(w["id"], "deny")
        self.assertTrue(m.wait_idle(task, T))
        self.assertIn("没有同意借用模型", json.dumps(self.tool_result(model.calls[1]), ensure_ascii=False))

    # ------------------------------------------------ 图片、roots、清单变化

    def test_image_result_reaches_model_and_steps(self):
        m, factory, model = self.manager([reply(calls=[("c1", "mcp__fake__picture", {})]), reply("看到了")])
        task = m.create("看图", workdir=str(self.project))["id"]
        self.assertTrue(m.wait_idle(task, T))
        content = self.tool_result(model.calls[1])
        self.assertIsInstance(content, list)
        self.assertEqual([p["type"] for p in content], ["text", "image"])
        self.assertEqual(content[0]["text"], "这是一张图")
        self.assertTrue(factory.blobs.get(content[1]["ref"]).startswith(b"\x89PNG"))
        step = next(s for s in m.detail(task, mark_seen=False)["steps"] if s["tool"] == "mcp__fake__picture")
        self.assertEqual(step["images"][0]["mime"], "image/png")
        self.assertIn("[图片 image/png]", step["out"])

    def test_roots_are_the_calling_tasks_workdir(self):
        m, factory, model = self.manager([reply(calls=[("c1", "mcp__fake__where", {})]), reply("好")])
        task = m.create("在哪", workdir=str(self.project))["id"]
        self.assertTrue(m.wait_idle(task, T))
        self.assertEqual(self.tool_result(model.calls[1]), "roots: " + self.project.resolve().as_uri())

    def test_tool_list_change_applies_next_round(self):
        m, factory, model = self.manager([reply(calls=[("c1", "mcp__fake__grow", {})]), reply("长好了"),
                                          reply(calls=[("c2", "mcp__fake__grown", {})]), reply("用上了")])
        task = m.create("长一个工具", workdir=str(self.project))["id"]
        self.assertTrue(m.wait_idle(task, T))
        names = lambda c: [t["name"] for t in c["tools"]]
        self.assertNotIn("mcp__fake__grown", names(model.calls[0]))
        until(lambda: factory.mcp_pool().manager.servers[next(iter(factory.mcp_pool().manager.servers))].version)
        m.input(task, "用新工具")
        self.assertTrue(m.wait_idle(task, T))
        self.assertIn("mcp__fake__grown", names(model.calls[2]))
        self.assertEqual(self.tool_result(model.calls[3]), "grown!")

    # ------------------------------------------------ prompts

    def test_prompts_list_and_create_task(self):
        m, factory, model = self.manager([reply("审好了")])
        prompts = m.prompts(str(self.project))
        review = next(p for p in prompts if p["name"] == "review")
        self.assertEqual(review["server"], "fake")
        self.assertEqual([(a["name"], a["required"]) for a in review["arguments"]], [("pr", True), ("focus", False)])
        s = m.create_from_prompt({"server": "fake", "name": "review", "arguments": {"pr": "12", "focus": "安全"}},
                                 workdir=str(self.project))
        self.assertEqual(s["title"], "/fake:review")
        self.assertTrue(m.wait_idle(s["id"], T))
        first = [x for x in model.calls[0]["messages"] if x["role"] == "user"][-1]["content"]
        self.assertIn("请审查 PR 12，重点看安全", json.dumps(first, ensure_ascii=False))
        with self.assertRaises(BadRequest):
            m.create_from_prompt({"server": "nope", "name": "review"}, workdir=str(self.project))


if __name__ == "__main__":
    unittest.main()
