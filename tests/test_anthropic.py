"""Anthropic Messages 协议测试：请求翻译、流式解析、跨协议。全部离线。"""
from __future__ import annotations

import json
import unittest

from weaver.policy import Policy
from weaver.providers import AnthropicModel, AnthropicQuirks, ModelError, make_model
from weaver.providers.anthropic import safe_id
from weaver.runner import Runner
from weaver.stores import MemoryBlobStore, MemoryEventStore
from weaver.tools import ToolBox

from .test_providers import FakeTransport, split


def asse(*events) -> bytes:
    """编成 Anthropic 风格的 SSE：每个事件带 event: 行。"""
    out = b""
    for e in events:
        out += f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode()
    return out


def start(usage=None):
    return {"type": "message_start", "message": {"id": "msg", "role": "assistant", "content": [],
                                                 "usage": usage or {"input_tokens": 10, "output_tokens": 1}}}


def block(i, **b):
    return {"type": "content_block_start", "index": i, "content_block": b}


def delta(i, **d):
    return {"type": "content_block_delta", "index": i, "delta": d}


def stop_block(i):
    return {"type": "content_block_stop", "index": i}


def end(reason="end_turn", output=5):
    return [{"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": output}},
            {"type": "message_stop"}]


def model(transport, **kw):
    return AnthropicModel("claude-x", "https://api.anthropic.com", "k", transport=transport,
                          sleep=lambda s: None, **kw)


OK = asse(start(), block(0, type="text", text=""), delta(0, type="text_delta", text="好"), stop_block(0), *end())

FULL = asse(
    start({"input_tokens": 20, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 80, "output_tokens": 1}),
    {"type": "ping"},
    block(0, type="thinking", thinking=""),
    delta(0, type="thinking_delta", thinking="先想"),
    delta(0, type="thinking_delta", thinking="一想"),
    delta(0, type="signature_delta", signature="SIG=="),
    stop_block(0),
    block(1, type="redacted_thinking", data="ENCRYPTED"),
    stop_block(1),
    block(2, type="text", text=""),
    delta(2, type="text_delta", text="我读一下"),
    stop_block(2),
    block(3, type="tool_use", id="toolu_1", name="read_file", input={}),
    delta(3, type="input_json_delta", partial_json="{\"path\": "),
    delta(3, type="input_json_delta", partial_json="\"中文.md\"}"),
    stop_block(3),
    block(4, type="tool_use", id="toolu_2", name="read_file", input={}),
    delta(4, type="input_json_delta", partial_json="{\"path\": \"b\"}"),
    stop_block(4),
    *end("tool_use", 42),
)


class Stream(unittest.TestCase):
    def check(self, reply):
        types = [p["type"] for p in reply["content"]]
        self.assertEqual(types, ["reasoning", "reasoning", "text", "tool_call", "tool_call"])
        think, redacted, text, a, b = reply["content"]
        self.assertEqual((think["text"], think["format"]), ("先想一想", "anthropic"))
        self.assertEqual(think["opaque"][0]["signature"], "SIG==")
        self.assertEqual(redacted["opaque"][0], {"index": 0, "type": "redacted", "data": "ENCRYPTED"})
        self.assertEqual(text["text"], "我读一下")
        self.assertEqual((a["id"], a["args"]), ("toolu_1", {"path": "中文.md"}))
        self.assertEqual(b["args"], {"path": "b"})
        self.assertEqual(reply["stop"], "tool_calls")
        u = reply["usage"]
        self.assertEqual((u["input_tokens"], u["cached_tokens"], u["cache_write_tokens"], u["output_tokens"]),
                         (1000, 900, 80, 42))       # input 把缓存读写加回去

    def test_full_stream(self):
        deltas = []
        self.check(model(FakeTransport([FULL])).create("s", [], [], on_delta=deltas.append))
        self.assertEqual([d["type"] for d in deltas],
                         ["reasoning", "reasoning", "text", "tool_start", "tool_start"])

    def test_byte_by_byte(self):
        self.check(model(FakeTransport(split(FULL, 1))).create("s", [], []))

    def test_stop_reasons(self):
        for raw, want in (("end_turn", "end"), ("max_tokens", "truncated"), ("refusal", "refusal"),
                          ("model_context_window_exceeded", "context_exceeded"), ("pause_turn", "other")):
            raw_stream = asse(start(), block(0, type="text", text="x"), stop_block(0), *end(raw))
            self.assertEqual(model(FakeTransport([raw_stream])).create("s", [], [])["stop"], want, raw)

    def test_tool_input_given_on_start(self):
        raw = asse(start(), block(0, type="tool_use", id="t", name="f", input={"x": 1}), stop_block(0), *end("tool_use"))
        self.assertEqual(model(FakeTransport([raw])).create("s", [], [])["content"][0]["args"], {"x": 1})

    def test_overloaded_error_retries(self):
        err = asse(start(), block(0, type="text", text=""), delta(0, type="text_delta", text="半"),
                   {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        t, deltas = FakeTransport([err], [OK]), []
        reply = model(t).create("s", [], [], on_delta=deltas.append)
        self.assertEqual(reply["content"][0]["text"], "好")
        self.assertEqual([d["type"] for d in deltas], ["text", "abort", "text"])

    def test_invalid_request_error_not_retried(self):
        err = asse({"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})
        t = FakeTransport([err], [OK])
        with self.assertRaises(ModelError):
            model(t).create("s", [], [])
        self.assertEqual(len(t.requests), 1)


class Translate(unittest.TestCase):
    def body(self, messages, tools=(), tool_choice="auto", **kw):
        t = FakeTransport([OK])
        model(t, **kw).create("SYS", list(tools), messages, tool_choice=tool_choice)
        return t.requests[0]

    def test_headers_url_system_tools(self):
        req = self.body([{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                        tools=[{"name": "f", "description": "d", "parameters": {"type": "object"}}],
                        tool_choice="none")
        self.assertEqual(req["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual((req["headers"]["x-api-key"], req["headers"]["anthropic-version"]), ("k", "2023-06-01"))
        b = req["body"]
        self.assertEqual(b["system"], [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(b["tools"], [{"name": "f", "description": "d", "input_schema": {"type": "object"},
                                       "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(b["tool_choice"], {"type": "none"})
        self.assertTrue(b["stream"])
        self.assertIn("max_tokens", b)
        self.assertNotIn("thinking", b)

    def test_bearer_auth_and_thinking(self):
        req = self.body([{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                        quirks=AnthropicQuirks(auth="bearer", thinking_budget=2048))
        self.assertEqual(req["headers"]["Authorization"], "Bearer k")
        self.assertNotIn("x-api-key", req["headers"])
        self.assertEqual(req["body"]["thinking"], {"type": "enabled", "budget_tokens": 2048})

    def test_tool_results_merged_into_user_and_alternation(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "读两个文件"}]},
            {"role": "assistant", "content": [
                {"type": "reasoning", "text": "想", "opaque": [{"index": 0, "signature": "S"}], "format": "anthropic"},
                {"type": "text", "text": "好"},
                {"type": "tool_call", "id": "a", "name": "read_file", "args": {"path": "x"}, "raw_args": "{}"},
                {"type": "tool_call", "id": "b", "name": "read_file", "args": None, "raw_args": "{bad"}]},
            {"role": "tool", "call_id": "a", "content": "内容", "is_error": False},
            {"role": "tool", "call_id": "b", "content": "参数不合法", "is_error": True},
            {"role": "user", "content": [{"type": "text", "text": "<system-reminder>插话</system-reminder>"}]},
        ]
        m = self.body(msgs)["body"]["messages"]
        self.assertEqual([x["role"] for x in m], ["user", "assistant", "user"])
        self.assertEqual(m[1]["content"][0], {"type": "thinking", "thinking": "想", "signature": "S"})
        self.assertEqual(m[1]["content"][3]["input"], {})            # 参数解析失败的调用回传 {}
        u = m[2]["content"]
        self.assertEqual([x["type"] for x in u], ["tool_result", "tool_result", "text"])   # 工具结果在前
        self.assertEqual(u[1], {"type": "tool_result", "tool_use_id": "b", "content": "参数不合法", "is_error": True})
        self.assertNotIn("is_error", u[0])

    def test_images_and_pdf(self):
        blobs = MemoryBlobStore()
        img, pdf = blobs.put(b"PNG"), blobs.put(b"%PDF")
        m = self.body([{"role": "user", "content": [
            {"type": "image", "ref": img, "mime": "image/png"},
            {"type": "file", "ref": pdf, "name": "a.pdf", "mime": "application/pdf"}]}], blobs=blobs)["body"]["messages"]
        a, b = m[0]["content"]
        self.assertEqual(a["source"]["media_type"], "image/png")
        self.assertEqual((b["type"], b["source"]["media_type"]), ("document", "application/pdf"))

    def test_cross_protocol_history(self):
        """OpenAI 格式留下的历史：id 被替换成合法字符，别家的思考签名被丢掉，不报错。"""
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [
                {"type": "reasoning", "text": "别家思考", "opaque": [{"type": "reasoning.text"}], "format": "openrouter"},
                {"type": "reasoning", "text": "纯文字思考", "opaque": None, "format": "reasoning_content"},
                {"type": "tool_call", "id": "call:abc.1", "name": "f", "args": {}, "raw_args": "{}"}]},
            {"role": "tool", "call_id": "call:abc.1", "content": "ok"},
        ]
        m = self.body(msgs)["body"]["messages"]
        self.assertEqual([b["type"] for b in m[1]["content"]], ["tool_use"])
        self.assertEqual(m[1]["content"][0]["id"], "call_abc_1")
        self.assertEqual(m[2]["content"][0]["tool_use_id"], "call_abc_1")
        self.assertEqual(safe_id("toolu_01A-b"), "toolu_01A-b")

    def test_empty_assistant_not_sent_empty(self):
        m = self.body([{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                       {"role": "assistant", "content": []},
                       {"role": "user", "content": [{"type": "text", "text": "再说"}]}])["body"]["messages"]
        self.assertTrue(m[1]["content"])


class Config(unittest.TestCase):
    def test_presets(self):
        m = make_model({"WEAVER_PROVIDER": "anthropic", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "claude-x"})
        self.assertIsInstance(m, AnthropicModel)
        m = make_model({"WEAVER_PROVIDER": "deepseek-anthropic", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "deepseek-chat",
                        "WEAVER_AUTH": "bearer", "WEAVER_THINKING_BUDGET": "1024"})
        self.assertEqual(m.base_url, "https://api.deepseek.com/anthropic")
        self.assertEqual((m.quirks.auth, m.quirks.thinking_budget), ("bearer", 1024))
        m = make_model({"WEAVER_PROVIDER": "openrouter-anthropic", "OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "a/b"})
        self.assertEqual((m.protocol, m.quirks.auth, m.name), ("anthropic_messages", "bearer", "a/b"))


class EndToEnd(unittest.TestCase):
    def test_runner_with_anthropic_then_switch_protocol(self):
        """同一本账本：先用 Anthropic 跑一个带工具的任务，再换 OpenAI 协议接着问。"""
        first = asse(start(), block(0, type="thinking", thinking=""),
                     delta(0, type="thinking_delta", thinking="读文件"), delta(0, type="signature_delta", signature="S"),
                     stop_block(0), block(1, type="tool_use", id="toolu_1", name="read_file", input={}),
                     delta(1, type="input_json_delta", partial_json="{\"path\": \"/nope\"}"), stop_block(1),
                     *end("tool_use"))
        second = asse(start(), block(0, type="text", text=""), delta(0, type="text_delta", text="不存在"),
                      stop_block(0), *end())
        t = FakeTransport(split(first, 5), [second])
        store = MemoryEventStore()
        r = Runner("s", store, model(t), ToolBox(), Policy(), "SYS")
        r.submit("读 /nope")
        self.assertEqual(r.run().run.status, "done")
        sent = t.requests[1]["body"]["messages"]
        self.assertEqual([x["role"] for x in sent], ["user", "assistant", "user"])
        self.assertEqual(sent[1]["content"][0]["signature"], "S")      # 思考签名原样回传
        self.assertEqual(sent[2]["content"][0]["type"], "tool_result")

        from weaver.providers import OpenAIChatModel
        from .test_providers import Retry
        t2 = FakeTransport([Retry.ok])
        r.model = OpenAIChatModel("m", "https://x/v1", "k", transport=t2, sleep=lambda s: None)
        r.submit("换个模型再问")
        self.assertEqual(r.run().run.status, "done")
        wire = t2.requests[0]["body"]["messages"]
        self.assertEqual([x["role"] for x in wire], ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertNotIn("reasoning_details", wire[2])                 # Anthropic 的签名不传给 OpenAI


if __name__ == "__main__":
    unittest.main()
