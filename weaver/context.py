"""背景输入（source=context）：环境信息、记忆快照等。见 design/environment.md 第四节。

每个提供者给出当前内容；执行器在每条用户输入前检查，和账本里同类最近一份不一样才在末尾追加。
追加不动前缀，缓存不受影响；压缩时 context 输入原样保留。
"""
from __future__ import annotations

import hashlib


class ContextProvider:
    kind = "context"
    first_head = ""                  # 第一次放进账本时的开头说明
    update_head = ""                 # 内容变了、追加更新时的开头说明

    def render(self) -> str:
        """当前内容；空字符串表示没有可说的。"""
        raise NotImplementedError

    def digest(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]


def last_digests(events: list[dict]) -> dict[str, str]:
    """账本里每种背景输入最近一份的哈希。兼容旧账本里记忆快照的 memory_digest 字段。"""
    found: dict[str, str] = {}
    for ev in events:
        if ev["type"] != "InputReceived" or ev["source"] != "context":
            continue
        if ev.get("context_kind") and ev.get("digest"):
            found[ev["context_kind"]] = ev["digest"]
        elif ev.get("memory_digest"):
            found["memory"] = ev["memory_digest"]
    return found


def pending_context(providers: list[ContextProvider], events: list[dict]) -> list[dict]:
    """该往账本里追加哪些背景输入。返回 [{kind, text, digest}]，按提供者顺序。"""
    seen = last_digests(events)
    out = []
    for p in providers:
        body = p.render()
        digest = p.digest(body)
        last = seen.get(p.kind)
        if last == digest or (last is None and not body):
            continue                 # 没变化；或者从来没有、现在也没有，就不占位置
        head = p.first_head if last is None else p.update_head
        text = "\n\n".join(x for x in (head, body or "（目前没有内容。）") if x)
        out.append({"kind": p.kind, "text": text, "digest": digest})
    return out
