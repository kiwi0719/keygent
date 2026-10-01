"""recall：查账本原文，包括被压缩掉的部分。见 design/round2.md 第一节、design/round3.md 第三节。

账本只追加、不删，而且进账本前已经脱敏，压缩只影响发给模型的投影。所以找回原文只要读账本。
scope="task" 查当前任务；scope="all" 查所有任务（weaver/corpus.py）；task + seq 取别的任务某一条的原文。
"""
from __future__ import annotations

import time

from ..corpus import entry_text as _text, excerpt

MAX_HITS = 10
EXCERPT = 500
MAX_FULL = 30_000


def _full(seq: int, kind: str, text: str, where: str = "") -> str:
    more = f"\n…[原文 {len(text)} 字，只显示前 {MAX_FULL} 字]" if len(text) > MAX_FULL else ""
    return f"{where}#{seq} {kind}\n{text[:MAX_FULL]}{more}"


def recall(events: list[dict], query: str = "", seq: int = 0) -> str:
    """只查当前任务。"""
    if seq:
        ev = next((e for e in events if e.get("seq") == seq), None)
        if ev is None:
            return f"没有第 {seq} 条记录（这个会话共 {len(events)} 条）"
        kind, text = _text(ev)
        if not kind:
            return f"第 {seq} 条是 {ev['type']}，没有可读的内容"
        return _full(seq, kind, text)
    words = [w.lower() for w in query.split() if w]
    if not words:
        return "请给 query（关键字，空格分开，都要出现），或者给 seq 取某一条的原文"
    hits = []
    for ev in reversed(events):                  # 新的排前面
        kind, text = _text(ev)
        if kind and all(w in text.lower() for w in words):
            hits.append(f"#{ev['seq']} {kind}：{excerpt(text, words, EXCERPT)}")
            if len(hits) >= MAX_HITS:
                break
    if not hits:
        return f"没有同时包含 {'、'.join(words)} 的记录"
    return f"找到 {len(hits)} 条（新的在前；用 recall(seq=N) 看某一条的全文）：\n\n" + "\n\n".join(hits)


def _date(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def recall_all(corpus, query: str, current: str = "", workdir: str | None = None, archived: bool = False) -> str:
    hits = corpus.search(query, archived=archived, workdir=workdir, limit=MAX_HITS)
    if not query.split():
        return "请给 query（关键字，空格分开，都要出现）"
    if not hits:
        return f"所有任务里都没有同时包含 {'、'.join(query.split())} 的记录" + ("" if archived else "（归档的没搜，要搜加 archived=true）")
    rows = []
    for h in hits:
        tag = "本任务" if h["task"] == current else f"{h['task_title']} · {h['task'][:8]}"
        sub = f" · 子 Agent {h['sub']}" if h["sub"] else ""
        rows.append(f"[{tag}{sub} · {_date(h['ts'])}] #{h['seq']} {h['kind']}：{h['excerpt']}")
    return (f"找到 {len(hits)} 条（同一工作目录的在前，其余新的在前；用 recall(task=\"任务 id\", seq=N) 看全文）：\n\n"
            + "\n\n".join(rows))


def recall_other(corpus, task: str, seq: int) -> str:
    got = corpus.get(task, seq)
    if got is None:
        return f"没有以 {task} 开头的任务，或者不止一个（多写几位）"
    src, e = got
    if not e:
        return f"任务「{src.title}」里没有第 {seq} 条可读的记录"
    return _full(e[0], e[2], e[3], f"[{src.title} · {src.task[:8]}] ")


def tool(load, corpus=None, current: str = "", workdir: str | None = None):
    """load() 返回当前会话的账本；给了 corpus 就能跨任务查。"""
    from . import Tool, _spec
    props = {"query": {"type": "string", "description": "关键字，空格分开，都要出现"},
             "seq": {"type": "integer", "description": "记录序号，取这一条的全文"}}
    desc = ("查以前的记录原文，包括已经被压缩掉的部分（被清理的工具结果、被写成摘要的对话）。"
            "query：关键字，空格分开，都要出现；seq：直接取某一条的全文（占位符里写着序号）。")
    if corpus is not None:
        props.update({
            "scope": {"type": "string", "enum": ["task", "all"],
                      "description": "task（默认）：只查这个任务；all：查以前所有的任务（“上次那个……”）"},
            "task": {"type": "string", "description": "别的任务的 id（搜索结果里有），配合 seq 取那条的全文"},
            "archived": {"type": "boolean", "description": "scope=all 时也搜已归档的任务"}})
        desc += "scope=all 查以前所有的任务；task + seq 取别的任务里某一条的全文。"

    def run(query="", seq=0, scope="task", task="", archived=False):
        if corpus is not None and task and seq and task != current:
            return recall_other(corpus, task, seq)
        if corpus is not None and scope == "all":
            return recall_all(corpus, query, current, workdir, archived)
        return recall(load(), query, seq)
    return Tool(_spec("recall", desc, props, []), run)
