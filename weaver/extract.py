"""记忆自动提取。见 design/round2.md 第二节、design/round3.md 第一节。

- 发给模型的前缀和最近一次请求逐字节相同（同样的 system、工具清单、对话），再加一条提取要求：几乎全部命中缓存
- 只允许 remember / forget：工具清单不能改（改了缓存就失效），别的工具在执行时拦下。最多 MAX_ROUNDS 轮
- 两个时机：压缩写摘要之前、一轮成功结束后（由执行者决定什么时候调）
"""
from __future__ import annotations

import os

from .cache import mark
from .project import project

MAX_ROUNDS = 3
ALLOWED = ("remember", "forget")

PROMPT = """<system-reminder>
现在暂停手上的事，只做一件事：回顾上面的对话，把**以后的任务也用得上、代码和文件里查不到**的东西存成记忆。

值得记的（按记忆的四类）：
- user：用户是谁、做什么的、习惯和偏好（“用 pnpm 不用 npm”“回答用中文、简短”）
- feedback：用户纠正过你或明确认可过的做法，写成“规则 + Why + How to apply”
- project：这个项目的背景、约束、决定（代码里看不出来的）
- reference：外部资源在哪（文档地址、看板、负责人）

不要记：这次任务的过程和中间结果、代码里本来就有的东西、密钥和密码、只对这一次有用的细节。

先看对话开头的记忆索引：已经有相近的，就用同一个 name 覆盖更新，不要重复建。{scope_note}
用 remember 写，过时的用 forget 删。没有值得记的，就直接回答“没有”，不要调用任何工具。
这一步只能用 remember 和 forget。
</system-reminder>"""
NO_PROJECT = "这个任务没有项目（临时目录），只记跟用户本人有关的（user、feedback 类）。"


def enabled() -> bool:
    return os.environ.get("WEAVER_MEMORY_EXTRACT", "1").lower() not in ("0", "false", "no")


def extract(model, system: str, tools: list[dict], events: list[dict], memory, cache_key: str | None = None) -> dict:
    """跑一次提取。返回 {saved: [名字], forgot: [名字], usage, rounds, error?}。"""
    note = "" if "project" in memory.dirs else NO_PROJECT
    messages = mark(project(events)) + [{"role": "user", "content": [{"type": "text",
                                                                        "text": PROMPT.format(scope_note=note)}]}]
    fns = memory.tools()
    saved, forgot, usage, rounds = [], [], {}, 0
    for rounds in range(1, MAX_ROUNDS + 1):
        try:
            reply = model.create(system, tools, list(messages), tool_choice="auto", cache_key=cache_key)
        except Exception as e:
            return {"saved": saved, "forgot": forgot, "usage": usage, "rounds": rounds,
                    "error": f"{type(e).__name__}: {e}"}
        for k, v in (reply.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
        calls = [p for p in reply.get("content") or [] if p.get("type") == "tool_call"]
        if not calls:
            break
        messages.append({"role": "assistant", "content": reply.get("content") or []})
        for c in calls:
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            if c.get("name") not in ALLOWED:
                out, err = "提取记忆这一步只能用 remember 和 forget。", True
            else:
                try:
                    out, err = fns[c["name"]].fn(**args), False
                    (saved if c["name"] == "remember" else forgot).append(args.get("name", "?"))
                except Exception as e:
                    out, err = f"{type(e).__name__}: {e}", True
            messages.append({"role": "tool", "call_id": c["id"], "content": out, "is_error": err})
    return {"saved": saved, "forgot": forgot, "usage": usage, "rounds": rounds}


def remembered_in_run(events: list[dict], run_id: str) -> bool:
    """这一轮里模型自己调用过 remember（Claude Code 的“写过就跳过”）。"""
    for e in events:
        if e.get("run_id") == run_id and e["type"] == "ActionCompleted" and e.get("kind") == "model":
            for p in (e.get("output") or {}).get("content") or []:
                if p.get("type") == "tool_call" and p.get("name") == "remember":
                    return True
    return False


def summary(out: dict) -> str:
    if out.get("error"):
        return f"记忆提取失败：{out['error']}"
    parts = []
    if out.get("saved"):
        parts.append(f"存了 {len(out['saved'])} 条（{'、'.join(out['saved'])}）")
    if out.get("forgot"):
        parts.append(f"删了 {len(out['forgot'])} 条（{'、'.join(out['forgot'])}）")
    return "记忆提取：" + ("，".join(parts) if parts else "没有新的记忆")

