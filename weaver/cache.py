"""提示缓存策略（Runtime 层）。见 design/cache.md。

内核和账本都不知道缓存：这里只在发给模型的消息上加一个 `cache: true` 标记，
由各协议的适配器翻译成自己的写法（Anthropic 的 cache_control、OpenAI 的 prompt_cache_key 等）。
"""
from __future__ import annotations


def mark(messages: list[dict]) -> list[dict]:
    """在最后一条消息上打缓存断点。断点每轮往后挪，前面的内容由服务端按前缀命中。"""
    if not messages:
        return messages
    return messages[:-1] + [{**messages[-1], "cache": True}]


def cache_break(prev: dict | None, cur: dict, min_drop: int = 1024) -> str | None:
    """上一轮已经有缓存、这一轮命中数明显下降：前缀可能被改了，或缓存过期了。"""
    if not prev:
        return None
    before = (prev.get("cached_tokens") or 0) + (prev.get("cache_write_tokens") or 0)
    now = cur.get("cached_tokens") or 0
    if before >= min_drop and now < before - min_drop:
        return f"缓存命中从 {before} 降到 {now}，前缀可能被改动，或缓存已过期"
    return None
