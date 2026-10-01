"""测试用的假模型。真模型在 weaver/providers/ 里。

统一回复：{"content": [Part], "stop": end|tool_calls|truncated|refusal|other, "usage": {...}}
"""
from __future__ import annotations

import json
from typing import Callable


class FakeModel:
    """假模型：按剧本回复，测试时顶替真模型。

    script 里每一项是一个统一回复（dict），或一个函数 f(messages) -> 回复。
    剧本演完后再调用会抛错。所有收到的请求记在 self.calls 里。
    """
    name = "fake"

    def __init__(self, script: list[dict | Callable[[list[dict]], dict]] | Callable[[list[dict]], dict]):
        self.script = script
        self.calls: list[dict] = []

    def create(self, system, tools, messages, tool_choice="auto", on_delta=None, cache_key=None) -> dict:
        self.calls.append({"system": system, "tools": tools, "messages": messages, "tool_choice": tool_choice})
        if callable(self.script):
            out = self.script(messages)
        elif not self.script:
            raise RuntimeError("假模型的剧本已经演完")
        else:
            step = self.script.pop(0)
            out = step(messages) if callable(step) else step
        if on_delta:
            for p in out["content"]:
                if p["type"] == "text":
                    on_delta({"type": "text", "text": p["text"]})
                elif p["type"] == "tool_call":
                    on_delta({"type": "tool_start", "id": p["id"], "name": p["name"]})
        return out


def reply(text: str = "", calls: list[tuple[str, str, dict | None]] = (), stop: str | None = None,
          usage: tuple[int, int] = (10, 5)) -> dict:
    """测试用：快速造一个统一回复。calls 里每项是 (id, name, args)。"""
    parts: list[dict] = [{"type": "text", "text": text}] if text else []
    for cid, name, args in calls:
        parts.append({"type": "tool_call", "id": cid, "name": name, "args": args,
                      "raw_args": json.dumps(args) if args is not None else "{broken"})
    return {"content": parts, "stop": stop or ("tool_calls" if calls else "end"),
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}
