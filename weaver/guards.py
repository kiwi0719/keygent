"""卡死检测：从本任务的工具结果记录里找四种打转的模式。纯函数，见 design/todo-loop.md 第二部分。

只看记录末尾“连续”的一段：打转一定是最近在发生的事。
“结果一样”比较前先去掉每次都会变的部分（如 bash 的“用时 X 秒”），否则同样的失败每次都“不一样”。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 模式 → (提醒次数, 停下次数)
THRESHOLDS = {
    "repeat": (3, 5),          # 同一调用，结果也一样
    "same_error": (3, 5),      # 同一调用连续报错
    "tool_errors": (4, 8),     # 同一工具换着参数连续报错（bash 除外）
    "alternating": (6, 10),    # A、B、A、B 来回摇摆
}
VOLATILE = [re.compile(r"用时 [\d.]+ 秒"), re.compile(r"\d+ 步，\d+ token")]


@dataclass
class Found:
    kind: str
    count: int
    start_step: int            # 这段打转从第几步开始（提醒冷却用）
    desc: str


def _norm(result: str) -> str:
    for rx in VOLATILE:
        result = rx.sub("", result)
    return result.strip()


def _short(key: str, n: int = 80) -> str:
    name, _, args = key.partition(":")
    args = args if len(args) <= n else args[:n] + "…"
    return f"{name}({args})"


def _tail(history: list[dict], same) -> int:
    """从末尾往前数，连续满足 same(h, last) 的条数。"""
    if not history:
        return 0
    last, n = history[-1], 0
    for h in reversed(history):
        if not same(h, last):
            break
        n += 1
    return n


def detect(history: list[dict]) -> list[Found]:
    found = []
    if not history:
        return found
    last = history[-1]

    n = _tail(history, lambda h, l: h["key"] == l["key"] and _norm(h["result"]) == _norm(l["result"]))
    if n >= 2:
        found.append(Found("repeat", n, history[-n]["step"],
                           f"你已经连续 {n} 次调用 {_short(last['key'])}，结果都一样。重复不会有新结果"))

    n = _tail(history, lambda h, l: h["error"] and h["key"] == l["key"])
    if n >= 2:
        found.append(Found("same_error", n, history[-n]["step"],
                           f"{_short(last['key'])} 已经连续 {n} 次报错。同样的做法不会突然成功"))

    if last["error"] and last["name"] != "bash":
        n = _tail(history, lambda h, l: h["error"] and h["name"] == l["name"])
        if n >= 2 and len({h["key"] for h in history[-n:]}) > 1:
            found.append(Found("tool_errors", n, history[-n]["step"],
                               f"{last['name']} 已经换着参数连续 {n} 次报错。问题可能不在参数上"))

    if len(history) >= 4:
        a, b = history[-1]["key"], history[-2]["key"]
        n = 0
        if a != b:
            for i, h in enumerate(reversed(history)):
                if h["key"] != (a if i % 2 == 0 else b):
                    break
                n += 1
        if n >= 4:
            found.append(Found("alternating", n, history[-n]["step"],
                               f"最近 {n} 次调用在 {_short(b)} 和 {_short(a)} 之间来回摇摆"))
    return found
