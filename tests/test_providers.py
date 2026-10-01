"""接入层测试：SSE 解析、OpenAI 流式累积、断流重试、请求翻译、配置。全部离线。"""
from __future__ import annotations

import json
import unittest

from weaver.policy import Policy
from weaver.providers import ModelError, OpenAIChatModel, Quirks, guess_provider, make_model
from weaver.providers.openai_chat import ThinkSplitter
from weaver.providers.sse import iter_sse
from weaver.runner import Runner
from weaver.stores import MemoryBlobStore, MemoryEventStore
from weaver.tools import ToolBox


def sse(*objs) -> bytes:
    """把若干 JSON 对象（或原始字符串）编成 OpenAI 风格的 SSE 字节流。"""
    out = b""
    for o in objs:
        data = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)
        out += f"data: {data}\n\n".encode()
    return out


def chunk(delta=None, finish=None, usage=None):
    obj = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        obj = {"choices": [], "usage": usage}
    return obj


def split(data: bytes, size: int) -> list[bytes]:
    return [data[i:i + size] for i in range(0, len(data), size)]


class FakeTransport:
    """按顺序回放若干次“响应”。每次响应是字节块列表，或一个异常，或 (字节块列表, 中途抛的异常)。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append({"url": url, "headers": headers, "body": json.loads(body)})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        chunks, err = (r if isinstance(r, tuple) else (r, None))

        def gen():
            yield from chunks
            if err:
                raise err
        return gen()


def model(transport, **kw) -> OpenAIChatModel:
    return OpenAIChatModel("m", "https://x/v1", "k", transport=transport, sleep=lambda s: None, **kw)


FULL_STREAM = sse(
    chunk({"role": "assistant", "reasoning": "先想"}),
    chunk({"reasoning": "一想", "reasoning_details": [{"type": "reasoning.text", "text": "先想", "index": 0}]}),
    chunk({"reasoning_details": [{"type": "reasoning.text", "text": "一想", "signature": "sig", "index": 0}]}),
    chunk({"content": "好的，"}),
    chunk({"content": "我读一下"}),
    chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                           "function": {"name": "read_file", "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                           "function": {"name": "read_file", "arguments": "{\"pa"}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"path\": \"a.md\"}"}}]}),
    chunk({"tool_calls": [{"index": 1, "function": {"arguments": "th\": \"中文.md\"}"}}]}),
    chunk(finish="tool_calls"),
    ": OPENROUTER PROCESSING",
    chunk(usage={"prompt_tokens": 100, "completion_tokens": 20,
                 "prompt_tokens_details": {"cached_tokens": 80}, "cost": 0.001}),
    "[DONE]",
).replace(b"data: : OPENROUTER PROCESSING\n\n", b": OPENROUTER PROCESSING\n\n")


class SSE(unittest.TestCase):
    def test_chinese_split_across_chunks(self):
        raw = "data: {\"t\": \"中文\"}\n\n".encode()
        for size in (1, 2, 3, 5):
            events = list(iter_sse(split(raw, size)))
            self.assertEqual(events, [("message", '{"t": "中文"}')])

    def test_comments_event_names_multiline_crlf(self):
        raw = b": ping\r\n\r\nevent: foo\r\ndata: a\r\ndata: b\r\n\r\ndata: last"
        self.assertEqual(list(iter_sse([raw])), [("foo", "a\nb"), ("message", "last")])


class OpenAIStream(unittest.TestCase):
    def check_full(self, reply):
        types = [p["type"] for p in reply["content"]]
        self.assertEqual(types, ["reasoning", "text", "tool_call", "tool_call"])
        r, t, a, b = reply["content"]
        self.assertEqual(r["text"], "先想一想")
        self.assertEqual(r["opaque"], [{"type": "reasoning.text", "text": "先想一想", "signature": "sig", "index": 0}])
        self.assertEqual(r["format"], "openrouter")
        self.assertEqual(t["text"], "好的，我读一下")
        self.assertEqual((a["id"], a["name"], a["args"]), ("call_a", "read_file", {"path": "a.md"}))
        self.assertEqual(b["args"], {"path": "中文.md"})
        self.assertEqual(reply["stop"], "tool_calls")
        self.assertEqual(reply["usage"]["input_tokens"], 100)
        self.assertEqual(reply["usage"]["cached_tokens"], 80)

    def test_full_stream(self):
        deltas = []
        reply = model(FakeTransport([FULL_STREAM])).create("sys", [], [], on_delta=deltas.append)
        self.check_full(reply)
        self.assertEqual("".join(d["text"] for d in deltas if d["type"] == "text"), "好的，我读一下")
        self.assertEqual([d["name"] for d in deltas if d["type"] == "tool_start"], ["read_file", "read_file"])

    def test_byte_by_byte(self):
        reply = model(FakeTransport(split(FULL_STREAM, 1))).create("sys", [], [])
        self.check_full(reply)

    def test_finish_without_done_is_ok(self):
        raw = sse(chunk({"content": "hi"}), chunk(finish="stop"))
        reply = model(FakeTransport([raw])).create("s", [], [])
        self.assertEqual((reply["stop"], reply["content"][0]["text"]), ("end", "hi"))

    def test_broken_tool_args(self):
        raw = sse(chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "x", "arguments": "{bad"}}]}),
                  chunk(finish="length"), "[DONE]")
        reply = model(FakeTransport([raw])).create("s", [], [])
        self.assertIsNone(reply["content"][0]["args"])
        self.assertEqual(reply["content"][0]["raw_args"], "{bad")
        self.assertEqual(reply["stop"], "truncated")

    def test_refusal(self):
        raw = sse(chunk({"refusal": "不行"}), chunk(finish="stop"), "[DONE]")
        self.assertEqual(model(FakeTransport([raw])).create("s", [], [])["stop"], "refusal")


class Retry(unittest.TestCase):
    ok = sse(chunk({"content": "好"}), chunk(finish="stop"), "[DONE]")

    def test_mid_stream_break_aborts_and_retries(self):
        half = sse(chunk({"content": "半截"}))
        t = FakeTransport((split(half, 4), ModelError("流中断", retryable=True)), [self.ok])
        deltas = []
        reply = model(t).create("s", [], [], on_delta=deltas.append)
        self.assertEqual(reply["content"][0]["text"], "好")
        kinds = [d["type"] for d in deltas]
        self.assertEqual(kinds, ["text", "abort", "text"])
        self.assertTrue(deltas[1]["retrying"])

    def test_error_event_in_200_stream(self):
        err = sse(chunk({"content": "x"}), {"error": {"code": 502, "message": "upstream overloaded"}})
        t = FakeTransport([err], [self.ok])
        self.assertEqual(model(t).create("s", [], [])["content"][0]["text"], "好")
        self.assertEqual(len(t.requests), 2)

    def test_stream_ends_without_marker(self):
        t = FakeTransport([sse(chunk({"content": "x"}))], [self.ok])
        self.assertEqual(model(t).create("s", [], [])["content"][0]["text"], "好")

    def test_non_retryable_raises_immediately(self):
        t = FakeTransport(ModelError("HTTP 400: bad", retryable=False), [self.ok])
        with self.assertRaises(ModelError):
            model(t).create("s", [], [])
        self.assertEqual(len(t.requests), 1)

    def test_gives_up_after_retries(self):
        t = FakeTransport(*[ModelError("HTTP 503", retryable=True)] * 3)
        deltas = []
        with self.assertRaises(ModelError):
            model(t, retries=2).create("s", [], [], on_delta=deltas.append)
        self.assertEqual(len(t.requests), 3)
        self.assertEqual(deltas, [])                  # 没显示过东西，就不用发 abort


class Translate(unittest.TestCase):
    def test_request_body(self):
        blobs = MemoryBlobStore()
        ref = blobs.put(b"\x89PNG")
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "看图"}, {"type": "image", "ref": ref, "mime": "image/png"}]},
            {"role": "assistant", "content": [
                {"type": "reasoning", "text": "想", "opaque": [{"type": "reasoning.text", "text": "想"}], "format": "openrouter"},
                {"type": "reasoning", "text": "别家", "opaque": [{"sig": "x"}], "format": "anthropic"},
                {"type": "text", "text": "读一下"},
                {"type": "tool_call", "id": "c1", "name": "read_file", "args": {"path": "a"}, "raw_args": "{\"path\":\"a\"}"}]},
            {"role": "tool", "call_id": "c1", "content": "不存在", "is_error": True},
        ]
        t = FakeTransport([Retry.ok])
        m = model(t, blobs=blobs, quirks=Quirks(echo_reasoning={"openrouter"}))
        m.create("SYS", [{"name": "read_file", "description": "", "parameters": {}}], msgs, tool_choice="none")
        req = t.requests[0]
        body = req["body"]
        self.assertEqual(req["url"], "https://x/v1/chat/completions")
        self.assertEqual(req["headers"]["Authorization"], "Bearer k")
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["tool_choice"], "none")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})
        self.assertTrue(body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        a = body["messages"][2]
        self.assertEqual(a["content"], "读一下")
        self.assertEqual(a["tool_calls"][0]["function"]["arguments"], "{\"path\":\"a\"}")
        self.assertEqual(a["reasoning_details"], [{"type": "reasoning.text", "text": "想"}])  # 别家的签名被丢掉
        self.assertEqual(body["messages"][3], {"role": "tool", "tool_call_id": "c1", "content": "[错误] 不存在"})


class Dialects(unittest.TestCase):
    """各家“OpenAI 兼容”服务的方言差异。"""

    def run_stream(self, *objs, quirks=None, size=None):
        raw = sse(*objs)
        deltas = []
        reply = model(FakeTransport(split(raw, size) if size else [raw]), quirks=quirks).create(
            "s", [], [], on_delta=deltas.append)
        return reply, deltas

    def test_deepseek_reasoning_content_and_cache_usage(self):
        reply, deltas = self.run_stream(
            chunk({"reasoning_content": "嗯，"}), chunk({"reasoning_content": "想想"}),
            chunk({"content": "答案"}), chunk(finish="stop"),
            chunk(usage={"prompt_tokens": 100, "completion_tokens": 9,
                         "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36}), "[DONE]")
        r, t = reply["content"]
        self.assertEqual((r["type"], r["text"], r["format"]), ("reasoning", "嗯，想想", "reasoning_content"))
        self.assertEqual(t["text"], "答案")
        self.assertEqual(reply["usage"]["cached_tokens"], 64)
        self.assertEqual([d["type"] for d in deltas], ["reasoning", "reasoning", "text"])

    def test_zero_cached_is_zero_not_none(self):
        reply, _ = self.run_stream(chunk({"content": "x"}), chunk(finish="stop"),
                                   chunk(usage={"prompt_tokens": 10, "completion_tokens": 1,
                                                "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 900}}))
        self.assertEqual(reply["usage"]["cached_tokens"], 0)
        self.assertEqual(reply["usage"]["cache_write_tokens"], 900)

    def test_kimi_top_level_cached_tokens(self):
        reply, _ = self.run_stream(chunk({"content": "x"}), chunk(finish="stop"),
                                   chunk(usage={"prompt_tokens": 10, "completion_tokens": 1, "cached_tokens": 8}))
        self.assertEqual(reply["usage"]["cached_tokens"], 8)

    def test_think_tags_split_across_chunks(self):
        for size in (None, 1, 3):
            with self.subTest(size=size):
                reply, _ = self.run_stream(chunk({"content": "\n<think>先想"}), chunk({"content": "一下</th"}),
                                           chunk({"content": "ink>\n\n正文"}), chunk(finish="stop"), size=size)
                r, t = reply["content"]
                self.assertEqual((r["text"], t["text"]), ("先想一下", "正文"))

    def test_think_splitter_edge_cases(self):
        sp = ThinkSplitter()
        self.assertEqual(sp.feed("<th") + sp.feed("is is text"), [("text", "<this is text")])
        sp = ThinkSplitter()
        self.assertEqual(sp.feed("<think>没想完") + sp.flush(), [("reasoning", "没想完")])
        sp = ThinkSplitter()
        self.assertEqual(sp.feed("正文里的 <think> 不算"), [("text", "正文里的 <think> 不算")])

    def test_think_tags_can_be_disabled(self):
        reply, _ = self.run_stream(chunk({"content": "<think>x</think>y"}), chunk(finish="stop"),
                                   quirks=Quirks(think_tags=False))
        self.assertEqual(reply["content"][0]["text"], "<think>x</think>y")

    def test_tool_calls_without_index_or_id(self):
        reply, _ = self.run_stream(
            chunk({"tool_calls": [{"id": "a", "function": {"name": "f", "arguments": {"x": 1}}}]}),
            chunk({"tool_calls": [{"id": "b", "function": {"name": "g", "arguments": "{\"y\""}}]}),
            chunk({"tool_calls": [{"function": {"arguments": ": 2}"}}]}),
            chunk(finish="tool_calls"))
        a, b = reply["content"]
        self.assertEqual((a["id"], a["name"], a["args"]), ("a", "f", {"x": 1}))
        self.assertEqual((b["id"], b["name"], b["args"]), ("b", "g", {"y": 2}))
        reply, _ = self.run_stream(chunk({"tool_calls": [{"index": 0, "function": {"name": "f", "arguments": "{}"}}]}),
                                   chunk(finish="tool_calls"))
        self.assertEqual(reply["content"][0]["id"], "call_0")      # 没给 id 就补一个

    def test_finish_reason_variants(self):
        for raw, want in (("eos", "end"), ("max_tokens", "truncated"), ("sensitive", "refusal"), ("weird", "other")):
            reply, _ = self.run_stream(chunk({"content": "x"}), chunk(finish=raw))
            self.assertEqual(reply["stop"], want, raw)

    def test_stop_with_tool_calls_means_tool_calls(self):
        reply, _ = self.run_stream(chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "f", "arguments": "{}"}}]}),
                                   chunk(finish="stop"))
        self.assertEqual(reply["stop"], "tool_calls")

    def test_echo_reasoning_current_turn_only(self):
        think = lambda text: {"type": "reasoning", "text": text, "opaque": None, "format": "reasoning_content"}
        msgs = [{"role": "user", "content": [{"type": "text", "text": "旧问题"}]},
                {"role": "assistant", "content": [think("旧的思考"), {"type": "text", "text": "旧回答"}]},
                {"role": "user", "content": [{"type": "text", "text": "新问题"}]},
                {"role": "assistant", "content": [think("新的思考"), {"type": "tool_call", "id": "c", "name": "f",
                                                                      "args": {}, "raw_args": "{}"}]},
                {"role": "tool", "call_id": "c", "content": "ok"}]
        t = FakeTransport([Retry.ok])
        model(t, quirks=Quirks(echo_reasoning={"reasoning_content"}, echo_reasoning_scope="turn")).create("s", [], msgs)
        wire = t.requests[0]["body"]["messages"]
        self.assertNotIn("reasoning_content", wire[2])
        self.assertEqual(wire[4]["reasoning_content"], "新的思考")
        self.assertEqual(wire[1], {"role": "user", "content": "旧问题"})     # 纯文字用字符串

    def test_no_echo_by_default(self):
        msgs = [{"role": "assistant", "content": [{"type": "reasoning", "text": "想", "opaque": [{"a": 1}],
                                                   "format": "openrouter"}, {"type": "text", "text": "hi"}]}]
        t = FakeTransport([Retry.ok])
        model(t).create("s", [], msgs)
        self.assertEqual(t.requests[0]["body"]["messages"][1], {"role": "assistant", "content": "hi"})


class Config(unittest.TestCase):
    def test_legacy_openrouter_env(self):
        m = make_model({"OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "a/b"})
        self.assertEqual((m.protocol, m.name, m.base_url), ("openai_chat", "a/b", "https://openrouter.ai/api/v1"))

    def test_presets_and_custom(self):
        m = make_model({"WEAVER_PROVIDER": "deepseek", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "deepseek-chat"})
        self.assertEqual(m.base_url, "https://api.deepseek.com/v1")
        m = make_model({"WEAVER_PROVIDER": "custom", "WEAVER_BASE_URL": "http://h:1/v1", "WEAVER_MODEL": "x"})
        self.assertIsNone(m.api_key)
        m = make_model({"WEAVER_PROVIDER": "ollama", "WEAVER_MODEL": "qwen3"})
        self.assertEqual(m.base_url, "http://localhost:11434/v1")

    def test_openai_uses_max_completion_tokens(self):
        m = make_model({"WEAVER_PROVIDER": "openai", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "gpt-x"},
                       transport=FakeTransport([Retry.ok]))
        _, _, body = m.request("s", [], [], "auto")
        self.assertIn("max_completion_tokens", body)
        self.assertNotIn("max_tokens", body)

    def test_env_overrides(self):
        m = make_model({"WEAVER_PROVIDER": "custom", "WEAVER_BASE_URL": "http://h/v1", "WEAVER_MODEL": "x",
                        "WEAVER_STREAM_USAGE": "0", "WEAVER_ECHO_REASONING": "reasoning_content",
                        "WEAVER_ECHO_SCOPE": "turn", "WEAVER_THINK_TAGS": "false", "WEAVER_MAX_TOKENS": "1000"})
        q = m.quirks
        self.assertEqual((q.stream_usage, q.echo_reasoning, q.echo_reasoning_scope, q.think_tags),
                         (False, {"reasoning_content"}, "turn", False))
        _, _, body = m.request("s", [], [], "auto")
        self.assertNotIn("stream_options", body)
        self.assertEqual(body["max_tokens"], 1000)

    def test_extra_body(self):
        t = FakeTransport([Retry.ok])
        m = make_model({"OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "moonshotai/kimi-k3",
                        "WEAVER_EXTRA_BODY": '{"provider": {"order": ["Moonshot AI"]}}'}, transport=t)
        m.create("s", [], [])
        self.assertEqual(t.requests[0]["body"]["provider"], {"order": ["Moonshot AI"]})
        with self.assertRaisesRegex(ValueError, "JSON"):
            make_model({"OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "x", "WEAVER_EXTRA_BODY": "{bad"})

    def test_guess_provider_from_url(self):
        g = lambda u: guess_provider(u)[:2]
        self.assertEqual(g("https://openrouter.ai/api/v1/"), ("openrouter", "https://openrouter.ai/api/v1"))
        self.assertEqual(g("https://openrouter.ai/api/v1/chat/completions"), ("openrouter", "https://openrouter.ai/api/v1"))
        self.assertEqual(g("https://api.deepseek.com"), ("deepseek", "https://api.deepseek.com/v1"))
        self.assertEqual(g("https://api.deepseek.com/anthropic"), ("deepseek-anthropic", "https://api.deepseek.com/anthropic"))
        self.assertEqual(g("https://api.anthropic.com/v1/messages"), ("anthropic", "https://api.anthropic.com"))
        self.assertEqual(g("https://api.anthropic.com/v1"), ("anthropic", "https://api.anthropic.com"))
        self.assertEqual(g("http://localhost:11434/v1"), ("ollama", "http://localhost:11434/v1"))
        self.assertEqual(g("http://localhost:1234/v1"), ("lmstudio", "http://localhost:1234/v1"))
        self.assertEqual(guess_provider("https://llm.example.com/v1"), ("custom", "https://llm.example.com/v1", "openai_chat"))
        self.assertEqual(guess_provider("https://gw.example.com/anthropic/"),
                         ("custom", "https://gw.example.com/anthropic", "anthropic_messages"))

    def test_url_only_config(self):
        m = make_model({"WEAVER_BASE_URL": "https://api.deepseek.com", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "deepseek-chat"})
        self.assertEqual((m.base_url, m.quirks.echo_reasoning), ("https://api.deepseek.com/v1", {"reasoning_content"}))
        m = make_model({"WEAVER_BASE_URL": "https://gw.example.com/anthropic", "WEAVER_MODEL": "x"})
        self.assertEqual((m.protocol, m.base_url), ("anthropic_messages", "https://gw.example.com/anthropic"))
        with self.assertRaisesRegex(ValueError, "WEAVER_API_KEY"):
            make_model({"WEAVER_BASE_URL": "https://openrouter.ai/api/v1", "WEAVER_MODEL": "x"})

    def test_deepseek_preset(self):
        m = make_model({"WEAVER_PROVIDER": "deepseek", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "deepseek-chat"})
        self.assertEqual(m.quirks.echo_reasoning, {"reasoning_content"})

    def test_errors(self):
        with self.assertRaises(ValueError):
            make_model({})
        with self.assertRaisesRegex(ValueError, "还没实现"):
            make_model({"WEAVER_PROVIDER": "custom", "WEAVER_PROTOCOL": "gemini_native",
                        "WEAVER_BASE_URL": "http://h", "WEAVER_MODEL": "x"})
        with self.assertRaisesRegex(ValueError, "WEAVER_API_KEY"):
            make_model({"WEAVER_PROVIDER": "openai", "WEAVER_MODEL": "x"})


class EndToEnd(unittest.TestCase):
    def test_runner_with_streaming_openai_model(self):
        first = sse(chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file",
                                                                              "arguments": "{\"path\": \"/nope\"}"}}]}),
                    chunk(finish="tool_calls"), chunk(usage={"prompt_tokens": 10, "completion_tokens": 5}), "[DONE]")
        second = sse(chunk({"content": "文件不存在"}), chunk(finish="stop"), "[DONE]")
        t = FakeTransport(split(first, 7), split(second, 3))
        store, deltas = MemoryEventStore(), []
        r = Runner("s", store, model(t), ToolBox(), Policy(), "SYS", on_delta=deltas.append)
        r.submit("读 /nope")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertEqual(s.run.tokens, 15)
        self.assertEqual(store.load("s")[-1]["text"], "文件不存在")
        self.assertEqual(t.requests[1]["body"]["messages"][-1]["role"], "tool")
        self.assertIn({"type": "text", "text": "文件不存在"}, deltas)
        self.assertFalse(any("delta" in e["type"].lower() for e in store.load("s")))   # 片段不进账本


if __name__ == "__main__":
    unittest.main()
