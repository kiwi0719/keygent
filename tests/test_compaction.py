"""上下文压缩测试：阈值、裁剪、摘要、切点、前缀、超长重试、崩溃恢复、退回方案。全部离线。"""
from __future__ import annotations

import tempfile
import unittest

from weaver import kernel as k
from weaver.compaction import SUMMARY_PROMPT, Compactor, est
from weaver.models import FakeModel, reply
from weaver.policy import Policy, compact_threshold
from weaver.project import is_trimmed, project, project_with_seq
from weaver.providers import ModelError
from weaver.providers.base import is_context_error
from weaver.providers.catalog import context_window
from weaver.runner import Runner
from weaver.stores import MemoryEventStore

from .test_kernel import check_invariants


def is_summary_request(messages) -> bool:
    last = messages[-1]
    return last["role"] == "user" and any(SUMMARY_PROMPT in p.get("text", "") for p in last["content"])


def usage_of(messages) -> tuple[int, int]:
    return sum(est(m) for m in messages) + 50, 30


class Brain:
    """由对话决定回复的假模型：读 n 次文件后回答；遇到写摘要的请求就写摘要。"""
    count = 0                                    # 每个实例一个编号，保证调用 id 不重复

    def __init__(self, reads: int, talk: int = 0, fail_summary: bool = False, overflow_first: int = 0):
        Brain.count += 1
        self.tag = Brain.count
        self.reads, self.talk, self.fail_summary = reads, talk, fail_summary
        self.overflow_left = overflow_first
        self.calls: list[dict] = []

    def create(self, system, tools, messages, tool_choice="auto", on_delta=None, cache_key=None):
        self.calls.append({"messages": messages, "tool_choice": tool_choice})
        if is_summary_request(messages):
            if self.fail_summary:
                raise RuntimeError("摘要模型挂了")
            return reply("## 目标\n读文件\n## 下一步\n继续", usage=usage_of(messages))
        if self.overflow_left:
            self.overflow_left -= 1
            raise ModelError("HTTP 400: prompt is too long: 250000 tokens > 200000 maximum")
        done = sum(1 for m in messages if m["role"] == "tool")
        done += sum(1 for m in messages for p in m.get("content", [])
                    if isinstance(p, dict) and "此前的对话已压缩" in p.get("text", "")) * 1000
        n = sum(1 for c in self.calls if not is_summary_request(c["messages"]))
        if n > self.reads:
            return reply("都读完了", usage=usage_of(messages))
        return reply("说" * self.talk, calls=[(f"c{self.tag}_{n}", "read", {"n": n})],
                     usage=usage_of(messages))

    @property
    def main_calls(self):
        return [c for c in self.calls if not is_summary_request(c["messages"])]


class BigTools:
    specs = [{"name": "read", "description": "", "parameters": {"type": "object", "properties": {}}}]

    def __init__(self, size=3000):
        self.size = size

    def execute(self, name, args, scope=None):
        return f"[{args['n']}]" + "x" * self.size, False


def make(brain, window=20_000, tool_size=3000, compactor=None, store=None, **policy):
    store = store or MemoryEventStore()
    pol = Policy(max_steps=100, token_budget=10 ** 9, context_window=window, max_output=1000,
                 tool_reserve=2000, **policy)
    r = Runner("s", store, brain, BigTools(tool_size), pol, "SYS",
               compactor=compactor or Compactor(keep_recent=3000, min_gain=1000))
    return r, store


def compacts(store):
    return [e["output"] for e in store.load("s") if e["type"] == "ActionCompleted" and e["kind"] == "compact"]


def strip(messages):
    return [{k: v for k, v in m.items() if k != "cache"} for m in messages]


class Threshold(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(compact_threshold(32_000), 16_000)                 # 预留减过头，退到一半
        self.assertEqual(compact_threshold(128_000), 128_000 - 8192 - 30_000)   # 预留起作用
        self.assertEqual(compact_threshold(204_800), 204_800 - 8192 - 30_000)
        self.assertEqual(compact_threshold(1_000_000), 850_000)             # 百分比起作用
        self.assertEqual(compact_threshold(1_000_000, soft_cap=200_000), 200_000)
        self.assertEqual(compact_threshold(32_000, soft_cap=10_000), 10_000)     # 软上限优先

    def test_context_estimate_in_state(self):
        r, store = make(Brain(reads=1))
        r.submit("go")
        r.run()
        s = k.fold(store.load("s"))
        self.assertGreater(s.context_tokens, 0)
        self.assertEqual(s.ctx_growth_bytes, 0)                # 最后一次模型回复之后没有新内容


class Trim(unittest.TestCase):
    def test_trim_when_tool_results_dominate(self):
        brain = Brain(reads=25)
        r, store = make(brain)
        r.submit("把 25 个文件都读一遍")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        cs = compacts(store)
        self.assertTrue(cs)
        self.assertEqual(cs[0]["mode"], "trim")
        msgs = project(store.load("s"))
        tools = [m["content"] for m in msgs if m["role"] == "tool"]
        self.assertTrue(is_trimmed(tools[0]))                  # 旧的被清理
        self.assertRegex(tools[0], r"（#\d+），需要时重新读取，或用 recall\(seq=\d+\) 取回原文\]$")
        self.assertFalse(is_trimmed(tools[-1]))                # 最近的原样
        self.assertEqual(sum(1 for m in msgs if m["role"] == "assistant"), 26)   # 调用本身都在
        # 每次调模型时，估算都没超过窗口
        for c in brain.main_calls:
            self.assertLess(sum(est(m) for m in c["messages"]), 20_000)
        check_invariants(self, store.load("s"))

    def test_prefix_stable_between_compactions(self):
        brain = Brain(reads=25)
        r, store = make(brain)
        r.submit("go")
        r.run()
        calls = [strip(c["messages"]) for c in brain.main_calls]
        changes = 0
        for a, b in zip(calls, calls[1:]):
            if a != b[:len(a)]:
                changes += 1
        self.assertEqual(changes, len(compacts(store)))        # 前缀只在压缩时变


class Summary(unittest.TestCase):
    def run_talky(self, **kw):
        brain = Brain(reads=12, talk=1500, **kw)                # 回复本身很长，裁工具结果不够
        r, store = make(brain, tool_size=100)
        r.submit("第一句原话：请把所有文件读一遍")
        r.run()
        return brain, r, store

    def test_summary_with_user_quotes(self):
        brain, r, store = self.run_talky()
        cs = compacts(store)
        self.assertEqual(cs[0]["mode"], "summary")
        self.assertIn("## 目标", cs[0]["summary"])
        self.assertIn("第一句原话：请把所有文件读一遍", cs[0]["summary"])     # 用户原话原样保留
        first = project(store.load("s"))[0]["content"][0]["text"]
        self.assertIn("此前的对话已压缩", first)
        self.assertEqual(k.fold(store.load("s")).run.status, "done")
        check_invariants(self, store.load("s"))

    def test_summary_request_reuses_prefix_and_disables_tools(self):
        brain, r, store = self.run_talky()
        i = next(i for i, c in enumerate(brain.calls) if is_summary_request(c["messages"]))
        summary_call, previous = brain.calls[i], brain.calls[i - 1]
        self.assertEqual(summary_call["tool_choice"], "none")
        head = strip(summary_call["messages"][:-1])
        self.assertEqual(head, strip(previous["messages"])[:len(head)])   # 开头和主对话一致，能命中缓存

    def test_second_summary_merges_previous(self):
        brain = Brain(reads=30, talk=1500)
        r, store = make(brain, tool_size=100)
        r.submit("go")
        r.run()
        cs = [c for c in compacts(store) if c["mode"] == "summary"]
        self.assertGreaterEqual(len(cs), 2)
        reqs = [c for c in brain.calls if is_summary_request(c["messages"])]
        self.assertIn("此前的对话已压缩", reqs[1]["messages"][0]["content"][0]["text"])   # 第二次摘要看得到第一次
        self.assertEqual(k.fold(store.load("s")).run.status, "done")

    def test_summary_sees_original_tool_results_not_placeholders(self):
        """先裁剪、再写摘要时，摘要请求里要是原文，不能是占位符（否则模型会编）。"""
        brain = Brain(reads=25)
        r, store = make(brain)
        r.submit("go")
        r.run()
        self.assertEqual(compacts(store)[0]["mode"], "trim")
        r.policy.compact_threshold = 100_000                  # 原文放得下
        for _ in range(3):                                    # 手动压缩，直到走到写摘要
            if r.compact()["mode"] == "summary":
                break
        req = [c for c in brain.calls if is_summary_request(c["messages"])][-1]
        tools = [m["content"] for m in req["messages"] if m["role"] == "tool"]
        self.assertTrue(tools)
        self.assertFalse(any(is_trimmed(t) for t in tools))
        self.assertIn("绝对不要猜测", req["messages"][-1]["content"][0]["text"])

    def test_no_summary_of_summary(self):
        brain = Brain(reads=25)
        r, store = make(brain)
        r.submit("go")
        r.run()
        modes = [r.compact()["mode"] for _ in range(4)]
        self.assertEqual(modes.count("summary"), 1)            # 没有新内容就不再写摘要
        self.assertEqual(modes[-1], "none")

    def test_summary_falls_back_to_trimmed_view_when_raw_too_big(self):
        brain = Brain(reads=25)
        r, store = make(brain)
        r.submit("go")
        r.run()
        r.policy.compact_threshold = 3000                     # 原文放不下了
        r.compact()
        req = [c for c in brain.calls if is_summary_request(c["messages"])][-1]
        self.assertTrue(any(is_trimmed(m["content"]) for m in req["messages"] if m["role"] == "tool"))

    def test_summary_failure_falls_back(self):
        brain, r, store = self.run_talky(fail_summary=True)
        c = compacts(store)[0]
        self.assertEqual(c["mode"], "summary")
        self.assertIn("摘要模型挂了", c["fallback"])
        self.assertIn("自动摘要没有成功", c["summary"])
        self.assertIn("第一句原话", c["summary"])
        self.assertEqual(k.fold(store.load("s")).run.status, "done")


class Cuts(unittest.TestCase):
    def test_cut_never_splits_calls_and_skips_queued_inputs(self):
        pairs = [(1, {"role": "user"}), (5, {"role": "assistant"}), (6, {"role": "tool"}), (8, {"role": "tool"}),
                 (7, {"role": "user"}),                    # 插话：比前面的工具结果早，排在后面
                 (9, {"role": "assistant"}), (10, {"role": "tool"}), (11, {"role": "user"})]
        cuts = Compactor._cuts(pairs)
        self.assertEqual(cuts, [1, 5, 7])
        for c in cuts:
            self.assertIn(pairs[c][1]["role"], ("assistant", "user"))
            self.assertLess(max(s for s, _ in pairs[:c]), min(s for s, _ in pairs[c:]))

    def test_user_quotes_newest_first_and_capped(self):
        store = MemoryEventStore()
        r = Runner("s", store, FakeModel([reply("a"), reply("b"), reply("c")]), BigTools(), Policy(), "SYS")
        for text in ("一", "二", "三"):
            r.submit(text)
            r.run()
        quotes = Compactor(quote_chars=2).user_quotes(store.load("s"), 10 ** 9)
        self.assertEqual(quotes.splitlines()[:2], ["- 三", "- 二"])
        self.assertIn("更早的原话见账本", quotes)


class Overflow(unittest.TestCase):
    def test_overflow_compacts_then_retries(self):
        brain = Brain(reads=6, overflow_first=1)
        r, store = make(brain, window=200_000)                 # 阈值很高，只靠“超长报错”触发
        r.submit("go")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        cs = compacts(store)
        self.assertEqual([c["reason"] for c in cs], ["overflow"])
        check_invariants(self, store.load("s"))

    def test_overflow_again_stops(self):
        brain = Brain(reads=3, overflow_first=5)
        r, store = make(brain, window=200_000)
        r.submit("go")
        s = r.run()
        self.assertEqual(s.run.status, "error")
        self.assertEqual(len(compacts(store)), 1)

    def test_context_exceeded_stop_reason(self):
        brain = FakeModel([reply("半截", stop="context_exceeded"), reply("好了")])
        store = MemoryEventStore()
        r = Runner("s", store, brain, BigTools(), Policy(), "SYS")
        r.submit("go")
        self.assertEqual(r.run().run.status, "done")
        self.assertEqual(len(compacts(store)), 1)
        self.assertNotIn("半截", str(project(store.load("s"))))   # 超长的半截回复不进对话

    def test_error_detection(self):
        for text in ("This model's maximum context length is 128000 tokens", "prompt is too long",
                     '{"code": "context_length_exceeded"}', "Input is too long for requested model"):
            self.assertTrue(is_context_error(text), text)
            e = ModelError(text, retryable=True)
            self.assertEqual((e.kind, e.retryable), ("context", False))
        self.assertFalse(is_context_error("rate limit exceeded"))


class Recovery(unittest.TestCase):
    def test_nothing_to_compact_does_not_loop(self):
        brain = Brain(reads=2)
        r, store = make(brain, window=1000)                    # 阈值极低：每次都想压，但没东西可压
        r.submit("go")
        s = r.run()
        self.assertEqual(s.run.status, "done")
        self.assertTrue(all(c["mode"] in ("none", "trim", "summary") for c in compacts(store)))

    def test_crash_during_compaction(self):
        brain = Brain(reads=25)
        r, store = make(brain)
        r.submit("go")
        r.run()
        events = store.load("s")
        cut = next(i for i, e in enumerate(events) if e["type"] == "ActionStarted" and e["kind"] == "compact") + 1
        store2 = MemoryEventStore()
        store2.sessions["s"] = events[:cut]
        r2, _ = make(Brain(reads=25), store=store2)
        s = r2.run()
        self.assertEqual(s.run.status, "done")
        started = [e for e in store2.load("s") if e["type"] == "ActionStarted" and e["kind"] == "compact"]
        self.assertEqual(started[0]["action_id"], started[1]["action_id"])   # 用同一个 id 重做
        check_invariants(self, store2.load("s"))

    def test_manual_compact(self):
        brain = Brain(reads=12, talk=1500)
        r, store = make(brain, window=10 ** 6, tool_size=100)
        r.submit("go")
        r.run()
        self.assertEqual(compacts(store), [])
        out = r.compact()
        self.assertEqual((out["reason"], out["mode"]), ("manual", "summary"))
        self.assertIn("此前的对话已压缩", project(store.load("s"))[0]["content"][0]["text"])

    def test_projection_seq_is_monotonic_after_cut(self):
        brain = Brain(reads=30, talk=1500)
        r, store = make(brain, tool_size=100)
        r.submit("go")
        r.run()
        pairs = project_with_seq(store.load("s"))
        self.assertEqual(pairs[0][1]["role"], "user")


class Catalog(unittest.TestCase):
    def test_openrouter_lookup_and_cache(self):
        calls = []

        def fetch():
            calls.append(1)
            return {"data": [{"id": "a/b", "context_length": 1_000_000}]}
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(context_window("a/b", "https://openrouter.ai/api/v1", d, fetch), 1_000_000)
            self.assertEqual(context_window("a/b", "https://openrouter.ai/api", d, fetch), 1_000_000)
            self.assertEqual(len(calls), 1)                    # 第二次读缓存
            self.assertIsNone(context_window("x/y", "https://openrouter.ai/api/v1", d, fetch))
        self.assertIsNone(context_window("a/b", "https://api.deepseek.com/v1", None, fetch))

        def broken():
            raise OSError("no network")
        self.assertIsNone(context_window("a/b", "https://openrouter.ai/api/v1", None, broken))


if __name__ == "__main__":
    unittest.main()
