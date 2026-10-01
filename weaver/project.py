"""投影：把账本翻译成发给模型的对话。

输出的是和厂商无关的中性格式，由模型适配器再转成具体接口的格式：
  {"role": "user",      "content": [Part]}
  {"role": "assistant", "content": [Part]}          # 含 reasoning、tool_call
  {"role": "tool",      "call_id", "content": str, "is_error": bool}

压缩（design/compaction.md）也在这一层生效：账本里的压缩记录说“某个位置之前缩小”，
投影照着做。原文一直留在账本里。
"""
from __future__ import annotations

from .kernel import AUTO_ACCEPT_SOURCES, CONTEXT_SOURCES

TRIMMED_TOOL = "[旧工具结果已清理"                 # 占位符的固定开头；完整的见 trimmed()


def trimmed(seq: int) -> str:
    """裁剪后的占位符：带上序号，模型可以用 recall 取回原文。只依赖序号，同一位置每次都一样（缓存不受影响）。"""
    return f"{TRIMMED_TOOL}（#{seq}），需要时重新读取，或用 recall(seq={seq}) 取回原文]"


def is_trimmed(content) -> bool:
    return isinstance(content, str) and content.startswith(TRIMMED_TOOL)
TRIMMED_IMAGE = "[旧图片已清理]"


def _reminder(text: str) -> dict:
    return {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}


def _render_input(ev: dict, trimmed: bool) -> dict:
    parts = ev["content"]
    if trimmed:
        parts = [{"type": "text", "text": TRIMMED_IMAGE} if p["type"] != "text" else p for p in parts]
    if ev["source"] != "user":        # 不是人说的话，包起来，免得模型当成用户的要求
        parts = [_reminder(p["text"]) if p["type"] == "text" else p for p in parts]
    return {"role": "user", "content": parts}


def compactions(events: list[dict]) -> tuple[dict | None, int]:
    """账本里生效的压缩：最后一次摘要，以及裁剪覆盖到的最大位置。"""
    summary, trim_upto = None, 0
    for ev in events:
        if ev["type"] == "ActionCompleted" and ev["kind"] == "compact":
            out = ev.get("output") or {}
            if out.get("mode") == "summary":
                summary = out
            if out.get("mode") in ("trim", "summary"):
                trim_upto = max(trim_upto, out.get("upto_seq", 0))
    return summary, trim_upto


def project_with_seq(events: list[dict], trims: bool = True) -> list[tuple[int, dict]]:
    """投影，并给每条消息标上产生它的事件位置（压缩时用来找切点）。

    trims=False 时不做裁剪，工具结果用原文（写摘要时用，免得模型对着占位符瞎编）。
    """
    events = [e for e in events if e.get("context_kind") != "fork"]     # 分叉标记：前缀由执行者另外接上
    summary, trim_upto = compactions(events)
    if not trims:
        trim_upto = 0
    start = summary["upto_seq"] if summary else 0
    out: list[tuple[int, dict]] = []
    if summary:
        # 背景输入（记忆快照等）永远不被压缩：原样排在摘要前面，这段前缀在压缩后仍能命中缓存
        for ev in events:
            if ev["seq"] <= start and ev["type"] == "InputReceived" and ev["source"] in CONTEXT_SOURCES:
                out.append((ev["seq"], _render_input(ev, trimmed=False)))
        out.append((start, {"role": "user", "content": [_reminder(
            summary["summary"] + f"\n\n（以上是第 1–{start} 条记录的摘要，原文还在账本里，需要细节可以用 recall 查）")]}))
    inputs: dict[str, dict] = {}
    order: list[str] = []             # 最近一条 assistant 里的调用顺序
    asks: set[str] = set()            # 其中的 ask_user 调用
    pending: set[str] = set()         # 其中还没有结果的调用
    results: dict[str, tuple[int, dict]] = {}   # 已到的结果；补齐后按调用顺序写回（并行时完成顺序是乱的）
    queued: list[tuple[int, dict]] = []   # 结果补齐前到达的输入，排在结果之后

    def add_input(ev: dict, seq: int) -> None:
        item = (seq, _render_input(ev, trimmed=seq <= trim_upto))
        (queued if pending else out).append(item)

    for ev in events:
        t, seq = ev["type"], ev["seq"]
        if t == "InputReceived":
            inputs[ev["id"]] = ev
        if seq <= start:
            continue
        if t == "InputReceived" and ev["source"] in AUTO_ACCEPT_SOURCES:
            add_input(ev, seq)
        elif t == "InputJudged" and ev["verdict"] == "accept":
            add_input(inputs[ev["input_id"]], seq)
        elif t == "ActionCompleted" and ev["kind"] == "model":
            o = ev.get("output") or {}
            if ev.get("is_error") or ev.get("synthetic") or o.get("stop") in ("refusal", "context_exceeded"):
                continue                  # 失败、被拒、超长的回复不进对话
            out.append((seq, {"role": "assistant", "content": o.get("content") or []}))
            order = [p["id"] for p in o.get("content") or [] if p["type"] == "tool_call"]
            asks = {p["id"] for p in o.get("content") or [] if p["type"] == "tool_call" and p.get("name") == "ask_user"}
            pending, results = set(order), {}
        elif t == "ActionCompleted" and ev["kind"] == "tool":
            if ev["action_id"] not in pending:
                continue                  # 不属于当前 assistant 的结果（比如被拒回复里的调用）
            # 用户对 ask_user 的回答是他亲口说的，很短，不裁
            keep = ev["action_id"] in asks
            content = trimmed(seq) if seq <= trim_upto and not keep else (ev.get("output") or "")
            results[ev["action_id"]] = (seq, {"role": "tool", "call_id": ev["action_id"], "content": content,
                                              "is_error": ev.get("is_error", False)})
            pending.discard(ev["action_id"])
            if not pending:
                out += [results[c] for c in order if c in results] + queued
                queued, results = [], {}
    out += [results[c] for c in order if c in results]      # 还没补齐（只在中途投影时出现）
    if not pending:
        out += queued
    return out


def project(events: list[dict]) -> list[dict]:
    return [m for _, m in project_with_seq(events)]
