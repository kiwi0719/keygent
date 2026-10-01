"""把账本翻译成人能读的步骤（对应 Keygent 的 ProcStep：you / agent / step）。见 design/daemon.md 第七节。

纯函数：给一本账本，出一个步骤列表。
- 用户的话 → you；模型的文字 → agent；工具调用 + 结果 → step（标题、工具、输出摘要、理由、状态）
- “理由”取同一条模型回复里、调用前面的那段文字
- 并行时结果在账本里是乱序的，但步骤在模型回复到达时就按调用顺序建好了，结果来了再填，所以顺序天然正确
- 背景信息（环境、记忆快照）、输入审核、等待本身不进步骤
"""
from __future__ import annotations

import json

from ..kernel import ANSWERED

OUT_LEN = 300
ARG_LEN = 60
ANSWERED_PREFIX = ANSWERED.split("{}")[0]       # “用户回答：”


def _short(s, n: int = ARG_LEN) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def title(name: str, args: dict | None) -> str:
    """每种工具一个标题模板；不认识的就“调用 工具名”。"""
    a = args if isinstance(args, dict) else {}
    if name == "read_file":
        off, lim = a.get("offset"), a.get("limit")
        rng = ""
        if off or lim:
            start = int(off or 1)
            rng = f" 第 {start}–{start + int(lim) - 1} 行" if lim else f" 从第 {start} 行起"
        return f"读 {a.get('path', '?')}{rng}"
    if name == "grep":
        where = f" 在 {a['path']}" if a.get("path") not in (None, "", ".") else ""
        glob = f"（{a['glob']}）" if a.get("glob") else ""
        return f"搜索 \"{_short(a.get('pattern'), 40)}\"{where}{glob}"
    if name == "find_files":
        return f"查找文件 {a.get('pattern', '?')}"
    if name == "write_file":
        return f"写入 {a.get('path', '?')}"
    if name == "edit_file":
        return f"修改 {a.get('path', '?')}"
    if name == "bash":
        return f"跑命令 {_short(a.get('command'))}" + ("（后台）" if a.get("background") else "")
    if name == "task" and a.get("background"):
        return f"派后台子 Agent：{_short(a.get('description'), 30)}"
    if name == "task":
        mode = (f"（{a['agent']}）" if a.get("agent") else "（分叉）" if a.get("context") == "fork" else
                {"general": "（可改文件）"}.get(a.get("mode", "explore"), ""))
        return f"派子 Agent：{_short(a.get('description'), 30)}{mode}"
    if name == "todo_write":
        todos = a.get("todos") or []
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") in ("completed", "cancelled"))
        return f"更新任务清单（{done}/{len(todos)}）"
    if name == "remember":
        return f"记住：{_short(a.get('description') or a.get('name'), 40)}"
    if name == "forget":
        return f"忘掉 {a.get('name', '?')}"
    if name == "ask_user":
        return f"问你：{_short(a.get('question'), 60)}"
    if name == "skill":
        return f"加载 skill：{a['name']}" if a.get("name") else f"查找 skill：{_short(a.get('query'), 30)}"
    if name == "recall":
        where = "（所有任务）" if a.get("scope") == "all" else "（别的任务）" if a.get("task") else ""
        return f"查记录 {_short(a.get('query') or ('#' + str(a.get('seq'))), 40)}{where}"
    if name in ("job_output", "job_kill", "job_wait"):
        verb = {"job_output": "看后台任务输出", "job_kill": "结束后台任务", "job_wait": "等后台任务"}[name]
        return f"{verb} {a.get('id', '?')}"
    if name == "jobs":
        return "列出后台任务"
    if name == "mcp_call":
        return f"MCP {a.get('server', '?')}：{a.get('tool', '?')}"
    if name == "mcp_search":
        return f"查 MCP 工具 {_short(a.get('query'), 30)}"
    if name == "mcp_list_resources":
        return "列 MCP 资源"
    if name == "mcp_read_resource":
        return f"读 MCP 资源 {_short(a.get('uri'), 40)}"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        return f"MCP {parts[1]}：{parts[2]}" if len(parts) == 3 else f"调用 {name}"
    return f"调用 {name}"


def _synthetic_status(output: str) -> str:
    if "已取消" in output or "取消" in output[:20]:
        return "cancelled"
    if "拒绝" in output:
        return "denied"
    if "执行中断" in output:
        return "interrupted"
    return "error"


def _input_text(ev: dict) -> str:
    parts = []
    for p in ev.get("content") or []:
        if p.get("type") == "text":
            parts.append(p.get("text", ""))
        else:
            parts.append(f"[{p.get('type')}：{p.get('name') or p.get('mime', '')}]")
    return "\n".join(x for x in parts if x)


def _user_said(ev: dict) -> tuple[str, list[str]]:
    """用户自己说的话 + 附件名。附件片段和给模型的说明（note）不混进正文。"""
    text, files = [], []
    for p in ev.get("content") or []:
        if p.get("files"):
            files.extend(f for f in p["files"] if f not in files)
        elif p.get("note") or p.get("type") != "text":
            if p.get("name") and p["name"] not in files:
                files.append(p["name"])
        elif p.get("text"):
            text.append(p["text"])
    return "\n".join(text), files


def steps(events: list[dict]) -> list[dict]:
    """步骤：{kind: you/agent/step, seq, ts, title, text, tool, out, why, status}"""
    out: list[dict] = []
    inputs: dict[str, dict] = {}
    by_call: dict[str, dict] = {}
    waits: dict[str, str] = {}                      # wait_id -> call_id

    def add(kind, ev, **kw):
        step = {"kind": kind, "seq": ev["seq"], "ts": ev.get("ts", 0), "title": "", "text": "", "tool": "",
                "out": "", "why": "", "status": "ok", **kw}
        out.append(step)
        return step

    for ev in events:
        t = ev["type"]
        if t == "InputReceived":
            inputs[ev["id"]] = ev
            src = ev.get("source")
            if src == "policy" and ev.get("nudge"):
                first = _input_text(ev).split("\n")[0]
                add("step", ev, title=f"Weaver 提醒：{_short(first, 50)}", text=_input_text(ev), status="note")
            elif src == "system":
                add("step", ev, title=_short(_input_text(ev).split("\n")[0], 60), text=_input_text(ev), status="note")
        elif t == "InputJudged":
            src = inputs.get(ev["input_id"], {})
            if ev["verdict"] == "accept" and src.get("source") in ("user", "trigger"):
                text = _input_text(src)
                if src.get("source") == "trigger":
                    add("step", ev, title=f"被触发：{_short(text, 50)}", text=text, status="note")
                else:
                    said, files = _user_said(src)
                    add("you", ev, title="你：" + _short(said or "、".join(files), 50), text=said, files=files)
            elif ev["verdict"] == "reject":
                add("step", ev, title="拒绝了一条外部输入", text=ev.get("reason", ""), status="denied")
        elif t == "ActionCompleted" and ev["kind"] == "model":
            o = ev.get("output") or {}
            if ev.get("is_error"):
                add("step", ev, title="模型调用失败", out=_short(o.get("error"), OUT_LEN), status="error")
                continue
            if ev.get("synthetic") or o.get("stop") == "refusal":
                continue
            content = o.get("content") or []
            why = "".join(p.get("text", "") for p in content if p.get("type") == "text").strip()
            calls = [p for p in content if p.get("type") == "tool_call"]
            if why:
                add("agent", ev, title="Agent：" + _short(why, 50), text=why)
            for c in calls:
                by_call[c["id"]] = add("step", ev, title=title(c.get("name", "?"), c.get("args")),
                                       tool=c.get("name", ""), why=why, status="running",
                                       text=json.dumps(c.get("args"), ensure_ascii=False) if c.get("args") else "")
        elif t == "ActionCompleted" and ev["kind"] == "compact":
            o = ev.get("output") or {}
            if o.get("mode") in ("trim", "summary"):
                kind = "裁剪旧工具结果" if o["mode"] == "trim" else "写摘要"
                add("step", ev, title=f"上下文压缩（{kind}）", status="note",
                    out=f"约 {o.get('tokens_before')} → {o.get('tokens_after')} token")
        elif t == "ActionCompleted" and ev["kind"] == "extract":
            o = ev.get("output") or {}
            saved = o.get("saved") or []
            if o.get("error"):
                add("step", ev, title="记忆提取失败", status="note", out=_short(o["error"], OUT_LEN))
            else:
                add("step", ev, title=f"记住了 {len(saved)} 条" if saved else "没有新的记忆", status="note",
                    out="、".join(saved))
        elif t == "ActionStarted" and ev.get("kind") == "tool":
            call = ev.get("input") or {}
            step = by_call.get(ev["action_id"])
            if step is not None and call.get("edited_from") is not None:     # 用户改过参数：按实际执行的显示
                step["title"] = title(call.get("name", "?"), call.get("args")) + "（你改过参数）"
                step["text"] = json.dumps(call.get("args"), ensure_ascii=False)
        elif t == "ActionCompleted":
            step = by_call.get(ev["action_id"])
            if step is None:
                continue
            text = ev.get("output") if isinstance(ev.get("output"), str) else str(ev.get("output"))
            step["out"] = _short(text, OUT_LEN)
            if step["tool"] == "ask_user" and not ev.get("is_error"):     # 回答不是错误，也不算“拒绝”
                step["out"] = ("你答：" + _short(text[len(ANSWERED_PREFIX):], OUT_LEN)
                               if text.startswith(ANSWERED_PREFIX) else "你让它自己定")
                step["status"] = "ok"
                continue
            step["status"] = (_synthetic_status(text) if ev.get("synthetic") else
                              "cancelled" if "任务被取消，命令被终止" in text[-200:] else
                              "error" if ev.get("is_error") else "ok")
        elif t == "WaitStarted":
            p = ev.get("payload") or {}
            call_id = p.get("call_id") or p.get("parent_call")      # 子 Agent 升上来的等待：标在 task 那一步上
            if call_id in by_call:
                waits[ev["wait_id"]] = call_id
                by_call[call_id]["status"] = "waiting"
        elif t == "WaitResolved":
            call_id = waits.get(ev["wait_id"])
            if call_id and by_call[call_id]["status"] == "waiting":
                by_call[call_id]["status"] = "running"
    return out
