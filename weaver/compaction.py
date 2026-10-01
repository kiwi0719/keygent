"""上下文压缩：怎么压（Runtime 层）。见 design/compaction.md。

内核决定“什么时候压”，这里决定“怎么压”：先试裁剪（不调模型），不够再写摘要。
结果作为 ActionCompleted(kind=compact) 的 output 记进账本，由投影照着执行，原文不删。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .kernel import ANSWERED
from .project import compactions, is_trimmed, project_with_seq, trimmed

ANSWERED_PREFIX = ANSWERED.split("{}")[0]       # “用户回答：”

SUMMARY_HEADER = "此前的对话已压缩成下面的摘要，完整记录仍保存在账本里。请直接接着工作，不要复述摘要。"
SUMMARY_PROMPT = """<system-reminder>
上下文快满了。请把到目前为止的对话写成一份交接摘要，交给接手的你自己继续工作。不要调用任何工具，只输出摘要。

如果对话开头已经有一份之前的摘要，把它合并进来，不要丢掉其中仍然有用的信息。

注意：
- 这份摘要只覆盖到这里。后面还有一段最近的对话会原样保留，接手时能看到，所以“当前状态”写到这里为止即可。
- 有些旧的工具结果可能已经被清理成“[旧工具结果已清理…]”。看不到的内容一律写“内容已清理，需要时重新读取”，绝对不要猜测或编造它的内容。

按下面的小节写，用 Markdown：
## 目标
## 约束和偏好
## 已完成
## 关键文件和代码位置
## 错误和修复
## 当前状态
## 下一步

要求：路径、函数名、命令、报错原文、ID 一律原样保留；不确定的地方如实写“不确定”；不要编造没做过的事。
用户的原话会由系统另外原样附上，这里不用逐字抄写。
</system-reminder>"""
FALLBACK_NOTE = "（自动摘要没有成功，这里只保留了用户的原话。需要细节时请重新读取相关文件。）"


def est(obj) -> int:
    """粗略估算 token 数：字节数 ÷ 3，图片按 1500 算。"""
    if isinstance(obj, dict) and obj.get("type") in ("image", "audio"):
        return 1500
    if isinstance(obj, dict) and obj.get("role"):
        content = obj.get("content")
        if isinstance(content, list):
            return 4 + sum(est(p) for p in content)
        return 4 + len(str(content or "").encode()) // 3
    return len(json.dumps(obj, ensure_ascii=False).encode()) // 3


@dataclass
class Compactor:
    keep_recent: int = 20_000          # 最近这么多 token 原样保留
    min_gain: int = 20_000             # 裁剪至少腾出这么多才值得（每次压缩都会让缓存失效一次）
    target_ratio: float = 0.6          # 压完后的目标 = 阈值 × 这个比例
    quote_chars: int = 20_000          # 用户原话最多附多少字
    quote_each: int = 4_000            # 每条原话最多多少字

    # ------------------------------------------------ 找切点

    @staticmethod
    def _cuts(pairs: list[tuple[int, dict]]) -> list[int]:
        """合法切点：切在 assistant 或 user 消息之前，且切点之后的消息都比之前的新（不拆开调用和结果）。"""
        cuts, max_before = [], 0
        min_after = [0] * (len(pairs) + 1)
        min_after[len(pairs)] = 10 ** 18
        for i in range(len(pairs) - 1, -1, -1):
            min_after[i] = min(pairs[i][0], min_after[i + 1])
        for i, (seq, msg) in enumerate(pairs):
            if i > 0 and msg["role"] in ("assistant", "user") and min_after[i] > max_before:
                cuts.append(i)
            max_before = max(max_before, seq)
        return cuts

    def _tail_start(self, pairs: list[tuple[int, dict]], keep: int) -> int:
        """从末尾往前数，凑够 keep 的位置。"""
        total = 0
        for i in range(len(pairs) - 1, -1, -1):
            total += est(pairs[i][1])
            if total >= keep:
                return i
        return 0

    # ------------------------------------------------ 主流程

    def plan_trim(self, pairs: list[tuple[int, dict]], keep: int) -> tuple[int, int]:
        """裁剪能腾出多少，以及裁到哪个位置。只动“最近一段”之前的工具结果。"""
        tail = self._tail_start(pairs, keep)
        cuts = [c for c in self._cuts(pairs) if c <= tail]
        if not cuts:
            return 0, 0
        head = pairs[:cuts[-1]]
        gain = sum(max(0, est(m) - est({"role": "tool", "content": trimmed(seq)}))
                   for seq, m in head if m["role"] == "tool" and not is_trimmed(m["content"]))
        return gain, max(seq for seq, _ in head)

    def compact(self, events: list[dict], model, system: str, tools: list[dict], current: int,
                threshold: int, reason: str = "threshold", before_summary=None) -> dict:
        """before_summary：确定要写摘要（会把旧对话移出去）时先调用一次，用来提取记忆。"""
        pairs = project_with_seq(events)
        target = int(threshold * self.target_ratio)
        need = max(current - target, 1)
        if reason == "overflow":                 # 已经放不下了，估算偏小，多腾一些
            need = max(need, current * 3 // 10)
        base = {"reason": reason, "tokens_before": current}
        # 小窗口时按阈值缩小：“原样保留”不超过阈值的 1/4，“至少腾出”不超过 1/10
        keep = min(self.keep_recent, threshold // 4)
        min_gain = min(self.min_gain, threshold // 10)

        gain, trim_upto = self.plan_trim(pairs, keep)
        if gain >= max(need, min_gain):
            return {**base, "mode": "trim", "upto_seq": trim_upto, "tokens_after": max(current - gain, 0)}

        tail = self._tail_start(pairs, keep)
        prev, _ = compactions(events)
        prev_upto = prev["upto_seq"] if prev else 0
        # 切点之前必须有上次摘要之后的新内容，否则就是在“摘要摘要”，白花钱
        cuts = [c for c in self._cuts(pairs) if c <= tail and max(s for s, _ in pairs[:c]) > prev_upto]
        if not cuts:
            if gain > 0:                         # 没法写摘要，能裁多少裁多少
                return {**base, "mode": "trim", "upto_seq": trim_upto, "tokens_after": max(current - gain, 0)}
            return {**base, "mode": "none", "note": "没有可以压缩的旧内容"}
        cut = cuts[-1]
        if before_summary:
            before_summary()
        head, rest = pairs[:cut], pairs[cut:]
        upto = max(seq for seq, _ in head)
        # 写摘要用原文（账本里都有），放得下就不给模型看占位符；放不下才退回裁剪后的版本
        raw_head = project_with_seq(events, trims=False)[:cut]
        source = raw_head if sum(est(m) for _, m in raw_head) <= threshold else head

        usage, text, error = {}, "", ""
        try:
            reply = model.create(system, tools, [m for _, m in source] +
                                 [{"role": "user", "content": [{"type": "text", "text": SUMMARY_PROMPT}]}],
                                 tool_choice="none")
            usage = reply.get("usage") or {}
            text = "".join(p["text"] for p in reply.get("content", []) if p["type"] == "text").strip()
            if not text:
                error = f"摘要为空（stop={reply.get('stop')}）"
        except Exception as e:                   # 摘要失败：退回机械方案
            error = f"{type(e).__name__}: {e}"

        body = f"{SUMMARY_HEADER}\n\n{text or FALLBACK_NOTE}"
        quotes = self.user_quotes(events, upto)
        if quotes:
            body += "\n\n## 用户原话（原样，从新到旧）\n" + quotes
        after = est({"role": "user", "content": body}) + sum(est(m) for _, m in rest)
        out = {**base, "mode": "summary", "upto_seq": upto, "summary": body, "tokens_after": after,
               "usage": usage}
        if error:
            out["fallback"] = error
        return out

    def user_quotes(self, events: list[dict], upto: int) -> str:
        """账本里 upto 之前、用户亲口说的话（包括对 ask_user 的回答），从新到旧原样列出。"""
        inputs = {e["id"]: e for e in events if e["type"] == "InputReceived"}
        questions = {p["id"]: (p.get("args") or {}).get("question", "")
                     for e in events if e["type"] == "ActionCompleted" and e["kind"] == "model"
                     for p in (e.get("output") or {}).get("content") or []
                     if p.get("type") == "tool_call" and p.get("name") == "ask_user"}
        said: list[str] = []
        for e in events:
            if e["seq"] > upto:
                break
            if e["type"] == "InputJudged" and e["verdict"] == "accept" \
                    and inputs.get(e["input_id"], {}).get("source") == "user":
                said.append("".join(p.get("text", "") for p in inputs[e["input_id"]]["content"]
                                    if p["type"] == "text").strip())
            elif e["type"] == "ActionCompleted" and e["kind"] == "tool" and e["action_id"] in questions \
                    and not e.get("is_error") and str(e.get("output", "")).startswith(ANSWERED_PREFIX):
                said.append(f"（回答 Weaver 的问题「{questions[e['action_id']]}」）"
                            + str(e["output"])[len(ANSWERED_PREFIX):])
        lines, used = [], 0
        for text in reversed(said):
            if not text:
                continue
            if len(text) > self.quote_each:
                text = text[:self.quote_each] + "…[过长已截断]"
            if used + len(text) > self.quote_chars:
                lines.append("- …[更早的原话见账本]")
                break
            lines.append("- " + text.replace("\n", "\n  "))
            used += len(text)
        return "\n".join(lines)
