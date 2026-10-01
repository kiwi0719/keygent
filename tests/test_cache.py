"""提示缓存测试：前缀不变（铁律）、断点位置、各家写法、观测。全部离线。"""
from __future__ import annotations

import json
import unittest

from weaver.cache import cache_break, mark
from weaver.models import reply
from weaver.policy import Policy
from weaver.providers import AnthropicModel, AnthropicQuirks, OpenAIChatModel, Quirks, make_model
from weaver.runner import Runner
from weaver.stores import MemoryEventStore
from weaver.tools import ToolBox

from .test_anthropic import OK as ANTHROPIC_OK
from .test_providers import Retry

TOOLS = [{"name": "echo", "description": "回显", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}},
         {"name": "read_file", "description": "读文件", "parameters": {"type": "object", "properties": {}}}]


class Recorder:
    """顶替模型：记下每次发出去的请求体，按剧本回复。"""

    def __init__(self, adapter, script):
        self.adapter, self.script, self.bodies = adapter, list(script), []
        self.name = adapter.name

    def create(self, system, tools, messages, tool_choice="auto", on_delta=None, cache_key=None):
        _, _, body = self.adapter.request(system, tools, messages, tool_choice, cache_key)
        self.bodies.append(json.loads(json.dumps(body, ensure_ascii=False)))
        return self.script.pop(0)


class EchoTools:
    specs = TOOLS

    def execute(self, name, args, scope=None):
        return f"结果 {args}", False


def strip_cache(obj):
    """去掉缓存标记：它每轮往后挪，不算前缀的一部分。"""
    if isinstance(obj, dict):
        return {k: strip_cache(v) for k, v in obj.items() if k not in ("cache_control",)}
    if isinstance(obj, list):
        return [strip_cache(x) for x in obj]
    return obj


def run_session(adapter) -> list[dict]:
    """两个任务、多轮工具调用、中途插话、并行调用。返回每次调模型时的请求体。"""
    script = [
        reply("先看看", calls=[("c1", "echo", {"x": "a"})]),
        reply(calls=[("c2", "echo", {"x": "b"}), ("c3", "read_file", {"x": "中文"})]),
        reply("第一个任务完成"),
        reply(calls=[("c4", "echo", {"x": "c"})]),
        reply("第二个任务完成"),
    ]
    rec = Recorder(adapter, script)
    r = Runner("sess", MemoryEventStore(), rec, EchoTools(), Policy(), "你是测试助手。" * 200)
    r.submit("第一个任务")
    r.run()
    r.submit("第二个任务")
    r.run()
    return rec.bodies


class PrefixStable(unittest.TestCase):
    """铁律：第 N 次请求（去掉缓存标记）是第 N+1 次请求的逐字节前缀。"""

    def check(self, bodies, messages_key="messages"):
        self.assertEqual(len(bodies), 5)
        for n in range(len(bodies) - 1):
            a, b = strip_cache(bodies[n]), strip_cache(bodies[n + 1])
            for key in a:
                if key == messages_key:
                    continue
                self.assertEqual(a[key], b[key], f"第 {n + 1} 次和第 {n + 2} 次的 {key} 不一致")
            ma, mb = a[messages_key], b[messages_key]
            # Anthropic 会把相邻的 user 内容合并，所以比较时允许最后一条被“追加内容”
            self.assertEqual(ma[:-1], mb[:len(ma) - 1], f"第 {n + 1} 次请求不是下一次的前缀")
            last_a, same_pos = ma[-1], mb[len(ma) - 1]
            self.assertEqual(last_a["role"], same_pos["role"])
            ca, cb = last_a["content"], same_pos["content"]
            if isinstance(ca, list):
                self.assertEqual(ca, cb[:len(ca)])
            else:
                self.assertEqual(ca, cb)
            sa = json.dumps(ma[:-1], ensure_ascii=False)
            sb = json.dumps(mb[:len(ma) - 1], ensure_ascii=False)
            self.assertTrue(sb.startswith(sa[:-1]))          # 序列化后也逐字节一致

    def test_openai_plain(self):
        self.check(run_session(OpenAIChatModel("gpt-x", "https://x/v1", "k")))

    def test_openai_with_claude_marks(self):
        self.check(run_session(OpenAIChatModel("anthropic/claude-x", "https://x/v1", "k",
                                               quirks=Quirks(cache_marks="claude"))))

    def test_anthropic(self):
        self.check(run_session(AnthropicModel("claude-x", "https://a", "k")))


class Breakpoints(unittest.TestCase):
    def test_mark_only_last(self):
        msgs = mark([{"role": "user", "content": []}, {"role": "tool", "call_id": "c", "content": "x"}])
        self.assertEqual([m.get("cache") for m in msgs], [None, True])
        self.assertEqual(mark([]), [])

    def count(self, obj) -> int:
        return json.dumps(obj).count("cache_control")

    def test_anthropic_positions(self):
        for body in run_session(AnthropicModel("claude-x", "https://a", "k")):
            self.assertIn("cache_control", body["tools"][-1])
            self.assertNotIn("cache_control", body["tools"][0])
            self.assertIn("cache_control", body["system"][-1])
            self.assertIn("cache_control", body["messages"][-1]["content"][-1])
            self.assertEqual(self.count(body), 3)                 # Anthropic 上限 4 个

    def test_anthropic_cache_off(self):
        body = run_session(AnthropicModel("claude-x", "https://a", "k", quirks=AnthropicQuirks(cache=False)))[0]
        self.assertEqual(self.count(body), 0)

    def test_openai_marks_only_for_claude(self):
        claude = run_session(OpenAIChatModel("anthropic/claude-x", "https://x/v1", "k",
                                             quirks=Quirks(cache_marks="claude")))
        gpt = run_session(OpenAIChatModel("openai/gpt-x", "https://x/v1", "k", quirks=Quirks(cache_marks="claude")))
        for body in claude:
            self.assertEqual(self.count(body), 2)                 # system + 最后一条消息
            self.assertIn("cache_control", body["messages"][-1]["content"][-1])
        for body in gpt:
            self.assertEqual(self.count(body), 0)
            self.assertIsInstance(body["messages"][0]["content"], str)

    def test_openai_prompt_cache_key(self):
        body = run_session(OpenAIChatModel("gpt-x", "https://x/v1", "k", quirks=Quirks(prompt_cache_key=True)))[0]
        self.assertEqual(body["prompt_cache_key"], "sess")
        body = run_session(OpenAIChatModel("gpt-x", "https://x/v1", "k"))[0]
        self.assertNotIn("prompt_cache_key", body)

    def test_presets(self):
        m = make_model({"OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "anthropic/claude-sonnet-4.5"})
        self.assertTrue(m.cache_marks)
        m = make_model({"WEAVER_PROVIDER": "openai", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "gpt-x"})
        self.assertTrue(m.quirks.prompt_cache_key)
        m = make_model({"WEAVER_PROVIDER": "deepseek", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "d",
                        "WEAVER_CACHE_MARKS": "always"})
        self.assertTrue(m.cache_marks)
        m = make_model({"WEAVER_PROVIDER": "anthropic", "WEAVER_API_KEY": "k", "WEAVER_MODEL": "c", "WEAVER_CACHE": "0"})
        self.assertFalse(m.quirks.cache)


class Observe(unittest.TestCase):
    def test_cache_break(self):
        self.assertIsNone(cache_break(None, {"cached_tokens": 0}))
        self.assertIsNone(cache_break({"cached_tokens": 5000}, {"cached_tokens": 5200}))
        self.assertIsNone(cache_break({"cached_tokens": 100}, {"cached_tokens": 0}))       # 太小，不算
        self.assertIn("降到", cache_break({"cached_tokens": 5000}, {"cached_tokens": 0}))
        self.assertIn("降到", cache_break({"cache_write_tokens": 4000}, {"cached_tokens": 0}))


class EndToEndWire(unittest.TestCase):
    def test_real_adapters_accept_cache_key(self):
        from .test_providers import FakeTransport
        OpenAIChatModel("m", "https://x/v1", "k", transport=FakeTransport([Retry.ok])).create(
            "s", [], [{"role": "user", "content": [{"type": "text", "text": "hi"}], "cache": True}], cache_key="x")
        AnthropicModel("m", "https://a", "k", transport=FakeTransport([ANTHROPIC_OK])).create(
            "s", [], [{"role": "user", "content": [{"type": "text", "text": "hi"}], "cache": True}], cache_key="x")


if __name__ == "__main__":
    unittest.main()
