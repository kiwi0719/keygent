"""从账本推出任务状态（给任务列表、胶囊、等你的事）。见 design/daemon.md 第四节。

纯函数。“排队中”由任务管理器另外标，这里推不出来。
"""
from __future__ import annotations

from .. import kernel as k
from .changes import call_diff
from .humanize import _input_text, _short, steps as to_steps, title

ERROR_NOTES = {
    "budget": "预算用完，已收尾",
    "stuck": "卡住了，已停下",
    "refusal": "模型拒绝了这个请求",
    "truncated": "输出被截断",
}
CHOICES = {
    "approval": ["allow", "deny"],
    "stuck": ["continue", "stop", "hint"],
    "input": ["allow", "deny"],
    "question": ["answer", "skip"],
    "elicit": ["accept", "decline", "cancel"],
}


def _plain(line: str) -> str:
    """列表上只有一行纯文字：去掉 Markdown 的标题井号、加粗、行内代码记号。"""
    return line.lstrip("#>-* ").replace("**", "").replace("`", "").strip()


def waits(state: k.State, events: list[dict], root: str | None = None) -> list[dict]:
    """没解决的等待，变成人能读的：{id, kind, title, body, choices, call?}
    给了 root（任务的工作目录）：改文件的审批多一个 diff（按磁盘现状和参数算）。"""
    inputs = {e["id"]: e for e in events if e["type"] == "InputReceived"}
    started = {e["wait_id"]: e for e in events if e["type"] == "WaitStarted"}
    out = []
    for w in state.open_waits:
        p = w.payload or {}
        ev = started.get(w.id, {})
        item = {"id": w.id, "seq": ev.get("seq", 0), "ts": ev.get("ts", 0)}
        if "elicit" in p:                           # MCP 服务器在调用中途问你（design/mcp2.md 第二节）
            e = p["elicit"]
            url = e.get("mode") == "url"
            item.update(kind="elicit", title=f"{e.get('server', 'MCP 服务器')} 问你：{e.get('message', '')}".strip(),
                        body=(f"打开这个网址，弄完后按“好了”：{e.get('url', '')}" if url else
                              f"{e.get('server', 'MCP 服务器')} 在要信息；不要在这里填密码"),
                        choices=CHOICES["elicit"], server=e.get("server", ""), mode=e.get("mode", "form"),
                        url=e.get("url", ""), fields=e.get("fields") or [])
        elif "sampling" in p:                       # MCP 服务器想借用模型（第三节）：每次问你
            sm = p["sampling"]
            server = sm.get("server", "MCP 服务器")
            item.update(kind="approval", title=f"{server} 想借用模型",
                        body=(sm.get("preview", "") + (f"\n（最多 {sm['max_tokens']} token）" if sm.get("max_tokens") else "")),
                        choices=CHOICES["approval"] + ["always"],
                        call={"id": w.id, "name": "sampling", "args": {"server": server}})
        elif "sub" in p:                              # 子 Agent 升上来的：照样能放行 / 改参数，注明来自哪个子 Agent
            note = f"（来自子 Agent：{p['sub']}）"
            if p.get("sub_kind") == "stuck":
                item.update(kind="stuck", title=f"子 Agent「{p['sub']}」好像卡住了", body=p.get("reason", ""),
                            choices=CHOICES["stuck"], from_agent=p["sub"])
            else:
                call = p.get("call") or {}
                name, args = call.get("name") or "?", call.get("args") or {}
                choices = CHOICES["approval"] + (["always"] if name == "bash" or name.startswith("mcp") else []) + \
                    (["edit"] if args else [])
                item.update(kind="approval", title=title(name, args), body=(p.get("reason", "") + note).strip(),
                            choices=choices, call={"id": call.get("id"), "name": name, "args": args},
                            from_agent=p["sub"])
                _diff(item, name, args, root)
        elif w.kind == "stuck":
            item.update(kind="stuck", title="它好像卡住了", body=p.get("reason", ""), choices=CHOICES["stuck"])
        elif w.kind == "question":                  # ask_user：问题本身就是标题，选项单独给
            args = p.get("args") or {}
            item.update(kind="question", title=str(args.get("question", "")).strip(), body="",
                        options=[str(o) for o in args.get("options") or []], choices=CHOICES["question"])
        elif "call_id" in p:
            name, args = p.get("name", "?"), p.get("args") or {}
            choices = CHOICES["approval"] + (["always"] if name == "bash" or name.startswith("mcp") else []) + \
                (["edit"] if args else [])
            item.update(kind="approval", title=title(name, args), body=p.get("reason", ""), choices=choices,
                        call={"id": p["call_id"], "name": name, "args": args})
            _diff(item, name, args, root)
        else:
            src = inputs.get(p.get("input_id"), {})
            item.update(kind="input", title="有一条外部输入待确认", body=_input_text(src) or p.get("reason", ""),
                        choices=CHOICES["input"])
        out.append(item)
    return sorted(out, key=lambda x: x["seq"])


def _diff(item: dict, name: str, args: dict, root: str | None) -> None:
    if root is None or name not in ("edit_file", "write_file"):
        return
    try:
        d = call_diff(name, args, root)
    except (OSError, ValueError):
        d = ""
    if d:
        item["diff"] = d


def summarize(events: list[dict], state: k.State | None = None) -> dict:
    """{status, note, now, waiting: [...], final}"""
    s = state or k.fold(events)
    pending = waits(s, events)
    r = s.run
    final = ""
    finished = [e for e in events if e["type"] == "RunFinished"]
    if finished:
        final = finished[-1].get("text", "") or ""
    if pending:
        first = pending[0]
        now = ("问你：" + first["title"] if first["kind"] == "question" else
               first["title"] + ("（等你放行）" if first["kind"] == "approval" else ""))
        status, note = "waiting", ""
    elif r is None:
        status, note, now = ("running", "", "准备开始") if s.new_inputs() or s.unjudged else ("idle", "", "")
    elif not r.active and s.unjudged:               # 新输入还没被内核接住（一轮开始前在连 MCP 等）
        status, note, now = "running", "", "准备开始"
    elif r.active or s.new_inputs():
        status, note = "running", ""
        running = [st for st in to_steps(events) if st["kind"] == "step" and st["status"] == "running"]
        now = running[-1]["title"] if running else "思考中"
    elif r.status == "done":
        status, note, now = "done", "", _short(_plain(final.strip().split("\n")[0]) if final else "完成", 60)
    elif r.status == "cancelled":
        status, note, now = "cancelled", "", "已取消"
    else:
        status = "error"
        note = ERROR_NOTES.get(r.status) or _short((final or (r.last_reply.error if r.last_reply else "")
                                                    or "出错了").strip().split("\n")[0], 80)
        now = note
    return {"status": status, "note": note, "now": now, "waiting": pending, "final": final}
