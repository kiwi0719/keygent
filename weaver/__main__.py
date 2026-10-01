"""命令行入口。

  python3 -m weaver "问题"                    新会话
  python3 -m weaver -s <会话id> "接着问"       在已有会话里开新任务
  python3 -m weaver -s <会话id>                崩溃后恢复：不加新输入，接着跑
  python3 -m weaver --list                    列出会话
  python3 -m weaver -s <会话id> --log          打印账本
  python3 -m weaver -s <会话id> --compact      手动压缩一次上下文
  python3 -m weaver -s <会话id> --undo         撤销这个会话最近一次文件改动（可连续执行）
  python3 -m weaver --yes "…"                  不问人（硬拦截和沙箱照样生效）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .providers import load_env, make_model
from .providers.catalog import context_window
from .cache import cache_break
from .environment import Environment
from .agents import AgentTypes, ProjectTrust
from .corpus import Corpus, session_sources
from .extract import summary as summary_extract
from .jobs import Jobs
from .memory import GUIDE as MEMORY_GUIDE, Memory
from .mcp import McpConfig, sdk_available
from .permissions import PermissionPolicy, command_prefix, mcp_key
from .runner import Runner, new_id
from .stores import DirBlobStore, JsonlEventStore
from .sandbox import Sandbox
from .skills import Skills
from .subagent import GUIDE as SUBAGENT_GUIDE, SubAgents
from .todo import tool as todo_tool
from .ask import parse_reply, prompt_lines, tool as ask_tool
from .tools import ToolBox, UndoLog
from .tools.recall import tool as recall_tool

HOME = Path(".weaver")
SYSTEM = ("你在用户的本地终端里工作。"
          "找东西时先用 grep（按内容）或 find_files（按文件名）定位，再用 read_file 只读相关的几行，"
          "不要整个文件读进来。互不依赖的查找可以在一轮里同时发起。"
          "改代码用 edit_file（改之前先读），新建文件用 write_file；改完用 bash 跑测试或运行来验证。"
          "被拒绝或被沙箱拦下的操作不要换个写法硬来，说明情况交给用户。回答简洁、直接。")


def short(text, n=200) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    text = text.replace("\n", " ⏎ ")
    return text if len(text) <= n else text[:n] + "…"


DIM, RESET = "\033[2m", "\033[0m"


class Terminal:
    """终端前端：on_delta 逐字显示流式片段，on_event 显示账本事件。"""

    def __init__(self):
        self.mode = None            # 当前正在输出的是 text / reasoning
        self.last_usage = None      # 上一轮的用量，用来发现缓存被打断

    def _switch(self, mode) -> None:
        if self.mode != mode and self.mode is not None:
            print(RESET if self.mode == "reasoning" else "", flush=True)
        if mode == "reasoning" and self.mode != "reasoning":
            print(f"{DIM}💭 ", end="")
        self.mode = mode

    def on_delta(self, d: dict) -> None:
        t = d["type"]
        if t == "text":
            self._switch("text")
            print(d["text"], end="", flush=True)
        elif t == "reasoning":
            self._switch("reasoning")
            print(d["text"], end="", flush=True)
        elif t == "tool_start":
            self._switch(None)
        elif t == "compact_start":
            self._switch(None)
            print(f"{DIM}  ⇣ 上下文压缩中（{d.get('reason')}）…{RESET}", flush=True)
        elif t == "abort":
            self._switch(None)
            tail = "，重试中…" if d.get("retrying") else ""
            print(f"\n  ✗ [流中断，以上内容作废{tail}] {short(d.get('reason', ''), 120)}", flush=True)

    def on_subagent(self, desc: str, ev: dict) -> None:
        """子 Agent 的事件只显示一行摘要，免得刷屏。"""
        if ev["type"] == "ActionCompleted" and ev["kind"] == "model" and not ev.get("is_error"):
            for p in (ev.get("output") or {}).get("content", []):
                if p["type"] == "tool_call":
                    print(f"{DIM}    ↳ [{desc}] {p['name']}({short(p['raw_args'], 90)}){RESET}", flush=True)
        elif ev["type"] == "RunFinished":
            print(f"{DIM}    ↳ [{desc}] 结束：{ev['status']}{RESET}", flush=True)

    def on_event(self, ev: dict) -> None:
        t = ev["type"]
        if t == "ActionCompleted" and ev["kind"] == "model":
            self._switch(None)
            out = ev.get("output") or {}
            if ev.get("is_error"):
                print(f"  ✗ 模型调用失败：{out.get('error')}", flush=True)
                return
            for p in out.get("content", []):
                if p["type"] == "tool_call":
                    print(f"  → {p['name']}({p['raw_args']})")
            u = out.get("usage") or {}
            write = f" write={u['cache_write_tokens']}" if u.get("cache_write_tokens") else ""
            print(f"{DIM}  · stop={out.get('stop')} in={u.get('input_tokens')} out={u.get('output_tokens')}"
                  f" cached={u.get('cached_tokens')}{write}{RESET}", flush=True)
            warn = cache_break(self.last_usage, u)
            if warn:
                print(f"  ⚠ {warn}", flush=True)
            self.last_usage = u
        elif t == "ActionCompleted" and ev["kind"] == "compact":
            out = ev.get("output") or {}
            if out.get("mode") in ("trim", "summary"):
                extra = f"，摘要失败已退回机械方案：{short(out['fallback'], 80)}" if out.get("fallback") else ""
                print(f"{DIM}  ⇣ 压缩（{out['mode']}）：约 {out.get('tokens_before')} → {out.get('tokens_after')} token"
                      f"{extra}{RESET}", flush=True)
            else:
                print(f"{DIM}  ⇣ 没有压缩：{out.get('note') or out.get('error')}{RESET}", flush=True)
            self.last_usage = None          # 压缩后缓存本来就会失效，不算“被打断”
        elif t == "ActionCompleted" and ev["kind"] == "extract":
            if (ev.get("output") or {}).get("reason") == "compact":      # 一轮结束后的那次由 main 打印
                print(f"{DIM}  ⇣ 压缩前{summary_extract(ev['output'])}{RESET}", flush=True)
            self.last_usage = None
        elif t == "ActionCompleted" and not ev.get("is_error") and str(ev.get("output", "")).startswith("清单已更新"):
            print("  " + str(ev["output"]).split("\n", 1)[1].replace("\n", "\n  "), flush=True)
        elif t == "ActionCompleted":
            mark = "✗" if ev.get("is_error") else "←"
            print(f"  {mark} {short(ev.get('output'))}", flush=True)
        elif t == "InputReceived" and ev["source"] == "context":
            label = {"environment": "环境", "memory": "记忆", "skills": "skills", "mcp": "MCP"}.get(ev.get("context_kind", "memory"), "背景")
            first = ev["content"][0].get("text", "").strip().split("\n", 1)[0]
            print(f"{DIM}  [{label}] {short(first, 60)}（{len(ev['content'][0].get('text', ''))} 字）{RESET}", flush=True)
        elif t == "InputReceived" and ev.get("nudge"):
            print(f"  ⚑ [{ev['nudge']}] {short(ev['content'][0].get('text', ''), 150)}", flush=True)
        elif t == "InputReceived" and ev["source"] != "user":
            print(f"  [{ev['source']}] {short(ev['content'][0].get('text', ''))}", flush=True)
        elif t == "InputJudged" and ev["verdict"] != "accept":
            print(f"  [输入{ev['verdict']}] {ev.get('reason', '')}", flush=True)
        elif t == "RunFinished":
            self._switch(None)
            print(f"[任务结束：{ev['status']}]", flush=True)


def ask(wait) -> dict:
    p = wait.payload
    if wait.kind == "question":
        args = p.get("args") or {}
        *lines, prompt = prompt_lines(args.get("question", ""), args.get("options") or [])
        print("\n  " + "\n  ".join(lines))
        answer = parse_reply(input("  " + prompt), args.get("options") or [])
        return {"allow": True, "answer": answer} if answer else {"allow": True, "skip": True}
    if wait.kind == "stuck":
        print(f"\n  ⚠ 它好像卡住了：{p.get('reason')}")
        ans = input("  [c] 继续  [s] 停下  或直接输入一句提示再继续：").strip()
        if ans.lower() in ("s", "stop", "停", "停下"):
            return {"allow": False}
        return {"allow": True, "note": "" if ans.lower() in ("", "c", "continue", "继续") else ans}
    if "call_id" not in p:
        ans = input(f"\n需要确认这条输入：{p.get('reason', '')} 允许吗？[y/N] ").strip().lower()
        return {"allow": ans in ("y", "yes")}
    args = p.get("args") or {}
    if p.get("name") == "bash":
        prefix = command_prefix(args.get("command", ""))
        print(f"\n  ? {p.get('reason')}：\n      {args.get('command')}")
        ans = input(f"  允许吗？[y] 这次  [a] 本会话里 “{prefix}” 开头的都允许  [N] 拒绝 ").strip().lower()
        if ans in ("a", "always"):
            return {"allow": True, "always": prefix}
    elif str(p.get("name", "")).startswith("mcp"):
        key = mcp_key(p.get("name", ""), args)
        print(f"\n  ? {p.get('reason')}：{p.get('name')}({short(args, 160)})")
        ans = input(f"  允许吗？[y] 这次  [a] 本会话里 {key} 都允许  [N] 拒绝 ").strip().lower()
        if ans in ("a", "always"):
            return {"allow": True, "always": key}
    else:
        print(f"\n  ? {p.get('reason')}：{p.get('name')}({short(args, 160)})")
        ans = input("  允许吗？[y/N] ").strip().lower()
    return {"allow": ans in ("y", "yes"), "note": "" if ans in ("y", "yes") else "用户在终端拒绝"}


def start_mcp(yes: bool):
    """读 MCP 配置、项目级先信任、连接。返回 (manager, McpTools)；没有配置或没装 SDK 时返回 (None, None)。"""
    conf = McpConfig(".")
    servers = conf.servers()
    for p in conf.problems:
        print(f"{DIM}[MCP] {p}{RESET}", flush=True)
    if not servers:
        return None, None
    if not sdk_available():
        print(f"{DIM}[MCP] 配置了 {len(servers)} 个服务器，但没装 MCP SDK，这次不连接。"
              f"请用项目的虚拟环境运行：.venv/bin/python -m weaver{RESET}", flush=True)
        return None, None
    untrusted = conf.untrusted()
    if untrusted:
        names = "、".join(f"{c.name}（{c.command or c.url}）" for c in untrusted)
        if yes:
            print(f"{DIM}[MCP] 项目配置里有未信任的服务器：{names}，这次不启动；不带 --yes 运行一次可以确认信任{RESET}",
                  flush=True)
        elif input(f"项目配置里有 MCP 服务器：{names}。启动它们等于在本机运行这些命令，信任吗？[y/N] "
                   ).strip().lower() in ("y", "yes"):
            conf.trust(untrusted)
    usable = []
    for c in servers.values():
        if c.problem:
            print(f"{DIM}[MCP] {c.name}：{c.problem}{RESET}", flush=True)
        elif conf.is_trusted(c):
            usable.append(c)
    if not usable:
        return None, None
    from .mcp.manager import McpManager
    from .mcp.tools import McpTools
    manager = McpManager(usable, HOME / "mcp-logs",
                         connect_timeout=float(os.environ.get("WEAVER_MCP_TIMEOUT", "10")))   # npx 第一次要下载，可调大
    manager.start()
    mcp = McpTools(manager)
    ok = [n for n, st in manager.servers.items() if st.client is not None]
    bad = [f"{n}（{st.error}）" for n, st in manager.servers.items() if st.client is None]
    print(f"{DIM}[MCP] 已连接：{'、'.join(ok) or '无'}；{len(mcp.entries)} 个工具"
          f"{'（延迟模式）' if mcp.deferred else ''}{'；连不上：' + '、'.join(bad) if bad else ''}{RESET}", flush=True)
    return manager, mcp


def main() -> None:
    from .daemon.client import SUBCOMMANDS, Api, main as client_main, run_prompt
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:     # weaver tasks / attach / daemon start …
        sys.exit(client_main(sys.argv[1:]))
    ap = argparse.ArgumentParser(prog="weaver")
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("-s", "--session")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--budget", type=int, default=1_000_000,
                    help="token 预算（命中缓存的输入按一折算），含子 Agent 的用量")
    ap.add_argument("--compact", action="store_true", help="手动压缩一次上下文")
    ap.add_argument("--undo", action="store_true", help="撤销这个会话最近一次文件改动")
    ap.add_argument("--yes", action="store_true", help="不问人（硬拦截和沙箱照样生效）")
    ap.add_argument("--no-sandbox", action="store_true", help="确认在没有沙箱时也用 --yes")
    ap.add_argument("--local", action="store_true", help="weaverd 在运行也在本地直接跑")
    args = ap.parse_args()

    # weaverd 在运行：交给它（Ctrl+C 只是不看了，任务在后台继续）。要本地跑的情况照旧
    if args.prompt and not (args.local or args.session or args.compact or args.undo or args.log or args.list
                            or args.yes):
        api = Api.connect()
        if api is not None:
            sys.exit(run_prompt(api, args.prompt, str(Path.cwd())))

    store = JsonlEventStore(HOME / "sessions")
    if args.list:
        for p in sorted(store.root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
            print(p.stem)
        return
    if args.log:
        for ev in store.load(args.session):
            print(json.dumps(ev, ensure_ascii=False))
        return
    if not args.prompt and not args.session:
        ap.error("需要一个问题，或用 -s 指定要恢复的会话")
    if args.session and not store.exists(args.session):
        ap.error(f"会话不存在：{args.session}")
    if args.undo:
        try:
            print(UndoLog(Path(".").resolve(), args.session, DirBlobStore(HOME / "blobs")).undo_last())
        except RuntimeError as e:
            sys.exit(str(e))
        return
    sandbox = Sandbox.from_env(".")
    if args.yes and not sandbox.kind and not args.no_sandbox:
        sys.exit("没有可用的沙箱（或被 WEAVER_SANDBOX=0 关掉了），--yes 会让命令不经确认直接在本机执行。"
                 "确定要这样，请再加 --no-sandbox")

    load_env()
    blobs = DirBlobStore(HOME / "blobs")
    try:
        model = make_model(blobs=blobs)
    except ValueError as e:
        sys.exit(str(e))
    term = Terminal()
    window = (int(os.environ["WEAVER_CONTEXT_WINDOW"]) if os.environ.get("WEAVER_CONTEXT_WINDOW")
              else context_window(model.name, model.base_url, HOME) or 128_000)
    compact_at = int(os.environ["WEAVER_COMPACT_AT"]) if os.environ.get("WEAVER_COMPACT_AT") else None
    policy = PermissionPolicy(".", yes=args.yes, max_steps=args.max_steps, token_budget=args.budget,
                              context_window=window, max_output=model.max_tokens, compact_at=compact_at,
                              interactive=not args.yes, ask_user=True)
    session = args.session or new_id()
    mcp_manager, mcp = start_mcp(args.yes)
    policy.mcp = mcp
    tools, memory, skills, agents = ToolBox(), Memory("."), Skills("."), AgentTypes(".")
    pending = ProjectTrust(skills, agents).pending()
    if pending:                                # 项目里的 skill、子 Agent 类型是别人写的指令：先信任一次
        what = "、".join(pending["skills"] + pending["agents"])
        n = len(pending["skills"]) + len(pending["agents"])
        if args.yes:
            print(f"{DIM}[项目里有 {n} 个未信任的 skill / 子 Agent 类型（{what}），这次不加载；"
                  f"不带 --yes 运行一次可以确认信任]{RESET}", flush=True)
        elif input(f"项目里有 {n} 个 skill / 子 Agent 类型：{what}。它们会作为指令给模型看，信任吗？[y/N] "
                   ).strip().lower() in ("y", "yes"):
            ProjectTrust(skills, agents).trust()
    tools.tools.update(memory.tools())
    notify = []                                 # 后台命令结束时往会话里记一条系统输入（runner 造好后接上）
    jobs = Jobs(Path(".").resolve(), HOME / "jobs", sandbox, notify=lambda text: notify and notify[0](text))
    for j in jobs.cleaned:
        print(f"{DIM}[结束了上次残留的后台命令 {j['id']}：{j['command']}]{RESET}", flush=True)
    tools.add_write_tools(session, blobs, sandbox, jobs=jobs)
    tools.tools["recall"] = recall_tool(lambda: store.load(session), Corpus(session_sources(HOME / "sessions", ".")),
                                        session, str(Path(".").resolve()))
    subagents = SubAgents(session=session, store=store, model=model, root=Path(".").resolve(), sandbox=sandbox,
                          blobs=blobs, yes=args.yes, approver=ask, display=term.on_subagent,
                          context_window=window, max_output=model.max_tokens, compact_at=compact_at,
                          skills=skills, mcp=mcp, agent_types=agents, jobs=jobs,
                          model_for=lambda name: make_model({**os.environ, "WEAVER_MODEL": name}, blobs=blobs),
                          fork_source={"model": model, "system": SYSTEM + MEMORY_GUIDE + SUBAGENT_GUIDE,
                                       "tools": tools})
    tools.on_interrupt.append(subagents.cancel_all)
    tools.on_interrupt.append(lambda: jobs.cancel_agents())
    tools.tools["task"] = subagents.tool()
    if mcp is not None:
        tools.tools.update(mcp.tools())
    tools.tools["todo_write"] = todo_tool()
    tools.tools["skill"] = skills.tool()
    tools.tools["ask_user"] = ask_tool()
    runner = Runner(session, store, model, tools, policy, SYSTEM + MEMORY_GUIDE + SUBAGENT_GUIDE,
                    approver=ask, sink=term.on_event, on_delta=term.on_delta,
                    context=[Environment(".", sandbox), memory, skills, agents] + ([mcp] if mcp else []),
                    extract_memory=memory)
    notify.append(lambda text: runner.submit(text, source="system"))
    print(f"[会话 {session} · {model.protocol} · {model.name} · 窗口 {window} · 压缩阈值 {policy.compact_threshold}]",
          flush=True)
    print(f"{DIM}[{sandbox.describe()}{' · --yes：不问人' if args.yes else ''}]{RESET}", flush=True)
    if args.compact:
        runner.compact()
        if not args.prompt:
            if mcp_manager is not None:
                mcp_manager.close()
            return

    if args.prompt:
        runner.submit(args.prompt)
    try:
        runner.run()
        while [j for j in jobs.running() if j.kind == "agent"]:   # 还有后台子 Agent：等它们，结果到了接着处理
            n = len([j for j in jobs.running() if j.kind == "agent"])
            print(f"{DIM}[等待 {n} 个后台子 Agent…（Ctrl+C 不等）]{RESET}", flush=True)
            for j in [j for j in jobs.running() if j.kind == "agent"]:
                j.done.wait()
            runner.run()
        out = runner.maybe_extract()                # 答案已经显示了，再提取记忆（这一轮成功结束才做）
        if out is not None:
            print(f"{DIM}[{summary_extract(out)}]{RESET}", flush=True)
    except KeyboardInterrupt:
        print("\n[取消中…]", flush=True)
        runner.cancel("用户按了 Ctrl+C")
        runner.run()
    finally:
        if jobs.running():
            print(f"{DIM}[Weaver 退出，结束 {len(jobs.running())} 个后台命令]{RESET}", flush=True)
        jobs.close()
        if mcp_manager is not None:
            mcp_manager.close()


if __name__ == "__main__":
    main()
