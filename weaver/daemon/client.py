"""命令行薄客户端：weaverd 在运行时，weaver 命令把任务交给它，在终端里显示、在终端里放行。见 design/daemon.md 第五节。

读事件流的线程把事件放进队列，主线程逐个处理；遇到等待就在主线程里问人，期间来的事件先排着。
Ctrl+C 只是“不看了”，任务在后台继续。
"""
from __future__ import annotations

import http.client
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
FINAL = ("done", "error", "cancelled")
STATUS_TEXT = {"running": "在跑", "waiting": "等你", "queued": "排队中", "done": "完成", "cancelled": "已取消",
               "error": "出错", "idle": "未开始"}


class ClientError(Exception):
    pass


def home() -> Path:
    return Path(os.environ.get("WEAVER_HOME") or "~/.weaver").expanduser()


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def load_info(base: Path | None = None) -> dict | None:
    """daemon.json；进程已经不在了（被强杀、没来得及删）当作没在运行。"""
    try:
        info = json.loads(((base or home()) / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if pid_alive(info.get("pid")) else None


class Api:
    def __init__(self, port: int, token: str, timeout: float = 15):
        self.port, self.token, self.timeout = port, token, timeout

    @classmethod
    def connect(cls, base: Path | None = None) -> "Api | None":
        """weaverd 在运行而且能连上，就返回客户端；否则 None。"""
        info = load_info(base)
        if not info:
            return None
        api = cls(info["port"], info["token"], timeout=3)
        try:
            api.call("GET", "/v1/status")
        except (OSError, ClientError):
            return None
        api.timeout = 15
        return api

    def call(self, method: str, path: str, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            c.request(method, path, body=data, headers=headers)
            r = c.getresponse()
            raw = r.read()
        finally:
            c.close()
        payload = json.loads(raw) if raw else None
        if r.status >= 400:
            msg = (payload or {}).get("error", {}).get("message") if isinstance(payload, dict) else None
            raise ClientError(msg or f"weaverd 返回了 {r.status}")
        return payload

    def stream(self, out: queue.Queue, stop: threading.Event) -> threading.Thread:
        """后台线程读事件流，(event, data) 放进 out；断开时放 ("closed", {})。"""
        def run():
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
                c.request("GET", "/v1/events", headers={"Authorization": f"Bearer {self.token}",
                                                         "X-Weaver-Client": "cli"})
                r = c.getresponse()
                event, data = "message", ""
                for raw in r:
                    if stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if data:
                            out.put((event, json.loads(data)))
                        event, data = "message", ""
                    elif line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        data += line[6:]
                c.close()
            except Exception:
                pass
            out.put(("closed", {}))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def resolve_task(self, prefix: str) -> str:
        ids = [t["id"] for t in self.call("GET", "/v1/tasks")["tasks"] if t["id"].startswith(prefix)]
        if not ids:
            raise ClientError(f"没有以 {prefix} 开头的任务")
        if len(ids) > 1:
            raise ClientError(f"以 {prefix} 开头的任务有 {len(ids)} 个，多写几位")
        return ids[0]


# ------------------------------------------------ 显示

def _first_line(s: str, n: int = 160) -> str:
    line = " ".join((s or "").strip().split("\n")[0].split())
    return line if len(line) <= n else line[:n] + "…"


class View:
    """一个任务在终端里的样子。同一个步骤可能来好几次（在跑 → 完成），按 (seq, 类型, 工具, 理由, 序) 认。"""

    def __init__(self, task_id: str, write=print, skip_first_you: bool = False):
        self.task_id, self.write = task_id, write
        self.shown: dict[tuple, str] = {}
        self.skip_you = skip_first_you

    def _key(self, st: dict) -> tuple:
        return st["seq"], st["kind"], st["tool"], st["why"], st["text"] if st["kind"] != "step" else st["title"].split("（你改过参数）")[0]

    def step(self, st: dict) -> None:
        key = self._key(st)
        before = self.shown.get(key)
        if before == st["status"]:
            return
        self.shown[key] = st["status"]
        if st["kind"] == "you":
            if self.skip_you:
                self.skip_you = False
                return
            self.write(f"{BOLD}你：{RESET}{st['text']}")
        elif st["kind"] == "agent":
            self.write(st["text"].strip())
        elif st["status"] == "note":
            self.write(f"{DIM}  · {st['title']}{RESET}")
        else:
            if before is None:
                self.write(f"  ⏺ {st['title']}")
            if st["status"] in ("ok", "error", "denied", "cancelled", "interrupted"):
                mark = {"ok": "", "error": "出错：", "denied": "被拒绝：", "cancelled": "已取消：",
                        "interrupted": "中断："}[st["status"]]
                self.write(f"{DIM}    ⎿ {mark}{_first_line(st['out']) or '(没有输出)'}{RESET}")

    def finished(self, summary: dict) -> None:
        s = summary["status"]
        if s == "done":
            self.write(f"{DIM}[完成]{RESET}")
        elif s == "cancelled":
            self.write(f"{DIM}[已取消]{RESET}")
        else:
            self.write(f"{DIM}[出错：{summary['note']}]{RESET}")


def ask_wait(api: Api, w: dict, read=input, write=print) -> None:
    """在终端里回答一件等待。"""
    if w["kind"] == "question":
        from ..ask import parse_reply, prompt_lines         # 用到才导入：薄客户端平时不加载工具箱
        *lines, prompt = prompt_lines(w["title"], w.get("options") or [])
        write("\n  " + "\n  ".join(lines))
        answer = parse_reply(read("  " + prompt), w.get("options") or [])
        try:
            api.call("POST", f"/v1/waits/{w['id']}",
                     {"decision": "answer", "note": answer} if answer else {"decision": "skip"})
        except ClientError as e:
            write(f"{DIM}  [{e}]{RESET}")           # 比如在 Keygent 那边已经回答了
        return
    if w["kind"] == "elicit":                               # MCP 服务器问你：逐个字段问（design/mcp2.md 第二节）
        write(f"\n  ? {w['title']}\n  {DIM}{w.get('body', '')}{RESET}")
        try:
            if w.get("mode") == "url":
                ans = read("  弄完了按回车；[n] 不给  [c] 取消这次调用：").strip().lower()
                body = {"decision": {"n": "decline", "c": "cancel"}.get(ans, "accept")}
            else:
                values = {}
                for f in w.get("fields") or []:
                    hint = f"（{' / '.join(f['options'])}）" if f.get("options") else \
                        "（y / n）" if f.get("type") == "boolean" else ""
                    v = read(f"  {f.get('title') or f['name']}{'*' if f.get('required') else ''}{hint}：").strip()
                    if f.get("type") == "boolean" and v:
                        v = v.lower() in ("y", "yes", "true", "是")
                    if v != "":
                        values[f["name"]] = v
                ans = read("  交上去？[y] 交  [n] 不给  [c] 取消这次调用：").strip().lower()
                body = ({"decision": "accept", "values": values} if ans in ("", "y", "yes") else
                        {"decision": "cancel"} if ans == "c" else {"decision": "decline"})
            api.call("POST", f"/v1/waits/{w['id']}", body)
        except ClientError as e:
            write(f"{DIM}  [{e}]{RESET}")
        return
    write(f"\n  ? {w['title']}" + (f"  {DIM}({w['body']}){RESET}" if w.get("body") else ""))
    choices = w["choices"]
    while True:
        try:
            if w["kind"] == "stuck":
                ans = read("  [c] 继续  [s] 停下  或直接输入一句提示再继续：").strip()
                body = ({"decision": "stop"} if ans.lower() in ("s", "stop", "停", "停下") else
                        {"decision": "continue"} if ans.lower() in ("", "c", "continue", "继续") else
                        {"decision": "hint", "note": ans})
            else:
                opts = "  [y] 放行  [n] 拒绝" + ("  [a] 总是允许" if "always" in choices else "") + \
                       ("  [e] 改参数" if "edit" in choices else "") + " "
                ans = read(opts).strip().lower()
                if ans in ("y", "yes"):
                    body = {"decision": "allow"}
                elif ans in ("a", "always") and "always" in choices:
                    body = {"decision": "always"}
                elif ans in ("e", "edit") and "edit" in choices:
                    args = _edit_args(w["call"], read)
                    if args is None:
                        continue
                    body = {"decision": "allow", "args": args}
                elif ans in ("n", "no", ""):
                    note = read("  原因（可以不填）：").strip()
                    body = {"decision": "deny", "note": note or "用户在终端拒绝"}
                else:
                    continue
            api.call("POST", f"/v1/waits/{w['id']}", body)
            return
        except ClientError as e:
            write(f"{DIM}  [{e}]{RESET}")           # 比如在 Keygent 那边已经处理了
            return


def _edit_args(call: dict, read) -> dict | None:
    args = dict(call.get("args") or {})
    if call["name"] == "bash":
        new = _prefilled(read, "  新命令：", args.get("command", ""))
        return {**args, "command": new} if new.strip() else None
    raw = _prefilled(read, "  新参数（JSON）：", json.dumps(args, ensure_ascii=False))
    try:
        data = json.loads(raw)
    except ValueError:
        print("  不是合法的 JSON")
        return None
    return data if isinstance(data, dict) else None


def _prefilled(read, prompt: str, text: str) -> str:
    """有 readline 时把原来的内容预先填好，改几个字就行。"""
    if read is not input:
        return read(prompt)
    try:
        import readline
    except ImportError:
        return read(prompt)
    readline.set_startup_hook(lambda: readline.insert_text(text))
    try:
        return read(prompt)
    finally:
        readline.set_startup_hook(None)


# ------------------------------------------------ 跟着一个任务

def follow(api: Api, task_id: str, events: queue.Queue, view: View, read=input, write=print) -> str:
    """显示已有的步骤，然后接着看事件流，直到这一轮结束。返回最后的状态。"""
    d = api.call("GET", f"/v1/tasks/{task_id}")
    for st in d["steps"]:
        view.step(st)
    asked: set[str] = set()
    pending = list(d["waiting"])
    status = d["task"]["status"]
    if status in FINAL and not pending:
        view.finished(d["task"])
        return status
    while True:
        while pending:
            w = pending.pop(0)
            if w["id"] not in asked:
                asked.add(w["id"])
                ask_wait(api, w, read, write)
        kind, data = events.get()
        if kind == "closed":
            write(f"{DIM}[和 weaverd 的连接断了；任务在后台继续，weaver attach {task_id[:8]} 接着看]{RESET}")
            return "detached"
        if data.get("task") != task_id:
            continue
        if kind == "step":
            view.step(data["step"])
        elif kind == "wait":
            pending.append(data["wait"])
        elif kind == "task":
            s = data["summary"]
            if s["status"] in FINAL and s["waiting"] == 0:
                for st in api.call("GET", f"/v1/tasks/{task_id}")["steps"]:   # 补上可能还没到的最后几步
                    view.step(st)
                view.finished(s)
                return s["status"]
        elif kind == "archived":
            write(f"{DIM}[任务已归档]{RESET}")
            return "archived"


def _watch(api: Api, start, read=input, write=print) -> int:
    """先连事件流再开始（新建 / 取详情），免得漏掉中间的事件。start(events) 返回任务 id 和 View。"""
    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    api.stream(events, stop)
    task_id = None
    try:
        task_id, view = start()
        status = follow(api, task_id, events, view, read, write)
        return 0 if status in ("done", "detached", "archived") else 1
    except KeyboardInterrupt:
        if task_id:
            write(f"\n{DIM}[不看了，任务在后台继续。weaver attach {task_id[:8]} 接着看，weaver cancel {task_id[:8]} 取消]{RESET}")
        return 0
    finally:
        stop.set()


def run_prompt(api: Api, text: str, workdir: str, write=print, read=input) -> int:
    def start():
        t = api.call("POST", "/v1/tasks", {"text": text, "workdir": workdir})
        write(f"{DIM}[交给 weaverd：任务 {t['id'][:8]} · 工作目录 {t['workdir']}]{RESET}")
        return t["id"], View(t["id"], write, skip_first_you=True)
    return _watch(api, start, read, write)


def attach(api: Api, prefix: str, write=print, read=input) -> int:
    task_id = api.resolve_task(prefix)
    return _watch(api, lambda: (task_id, View(task_id, write)), read, write)


# ------------------------------------------------ 子命令

def _ago(ts: float) -> str:
    import time
    d = max(0, time.time() - ts)
    return (f"{int(d)} 秒前" if d < 60 else f"{int(d // 60)} 分钟前" if d < 3600 else
            f"{int(d // 3600)} 小时前" if d < 86400 else f"{int(d // 86400)} 天前")


def cmd_tasks(api: Api, write=print) -> int:
    tasks = api.call("GET", "/v1/tasks")["tasks"]
    if not tasks:
        write("没有任务")
    for t in tasks:
        line = t["note"] if t["status"] == "error" else t["now"]
        write(f"{t['id'][:8]}  {STATUS_TEXT.get(t['status'], t['status']):4}  {_ago(t['updated']):7}  "
              f"{t['title']}{DIM}  {_first_line(line, 60)}{RESET}")
    return 0


def cmd_waits(api: Api, write=print) -> int:
    ws = api.call("GET", "/v1/waits")["waits"]
    if not ws:
        write("没有在等你的事")
    for w in ws:
        write(f"{w['id']}  {w['task_title']} · {w['title']}{DIM}  可选：{'/'.join(w['choices'])}{RESET}")
    return 0


def cmd_mcp(rest: list[str], listing, login, write=print, sleep=time.sleep, timeout: float = 600) -> int:
    """weaver mcp：列出用户级 MCP 服务器和状态；weaver mcp login <名字>：登录（design/mcp2.md 第一节）。
    listing() / login(name) 是 /v1/settings/mcp 和 …/login 的结果（weaverd 在跑时走接口，否则本地做）。"""
    marks = {"connected": "●", "connecting": "◌", "logging_in": "◌", "needs_login": "○", "failed": "✕", "invalid": "✕"}
    if not rest or rest[0] == "list":
        data = listing()
        if data.get("problem"):
            write(data["problem"])
        for x in data["servers"]:
            what = {"connected": f"已连接 · {len(x['tools'])} 个工具", "needs_login": "要登录：weaver mcp login " + x["name"],
                    "logging_in": "登录中"}.get(x["status"], x["error"] or x["status"])
            write(f"{marks.get(x['status'], '○')} {x['name']}  {what}")
        if not data["servers"]:
            write("还没有 MCP 服务器（~/.weaver/mcp.json，或者在 Keygent 的设置 › MCP 里加）")
        return 0
    if rest[0] != "login" or len(rest) < 2:
        write("用法：weaver mcp [list] | weaver mcp login <名字>")
        return 2
    name = rest[1]
    info = login(name)
    if info["kind"] == "done":
        write(f"{name}：已用本机 gh 的登录")
        return 0
    if info.get("error"):
        write(info["error"])
        return 1
    if info["kind"] == "device":
        write(f"在打开的网页（{info['verification_uri']}）里输入这个码：{info['user_code']}")
    else:
        write(f"已在浏览器打开授权页；没打开的话手动打开：\n{info['url']}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sleep(1)
        item = next((x for x in listing()["servers"] if x["name"] == name), None)
        if item is None:
            write(f"{name} 被删掉了")
            return 1
        if item["status"] in ("connected", "connecting"):
            write(f"{name}：登录好了")
            return 0
        if item["status"] not in ("logging_in",):
            write(f"{name}：{item['error'] or item['status']}")
            return 1
    write("等太久了，没完成登录")
    return 1


def _local_settings():
    """weaverd 没在运行时：本地起一个连接池做登录（令牌写进同一个文件，weaverd、命令行都能用）。"""
    from ..mcp import sdk_available
    from ..mcp.pool import McpPool
    from ..settings.service import Settings
    if not sdk_available():
        raise ClientError("没装 MCP SDK：请用项目的 .venv/bin/python -m weaver 运行")
    home = Path(os.environ.get("WEAVER_HOME") or "~/.weaver").expanduser()
    pool = McpPool(home, idle=None)
    return Settings(home, pool=lambda: pool), pool


SUBCOMMANDS = ("tasks", "waits", "attach", "approve", "cancel", "archive", "daemon", "mcp")


def main(argv: list[str]) -> int:
    """weaver <子命令> …；返回退出码。"""
    cmd, rest = argv[0], argv[1:]
    if cmd == "daemon":
        from .control import main as control
        return control(rest)
    api = Api.connect()
    if cmd == "mcp":
        try:
            if api is not None:
                return cmd_mcp(rest, lambda: api.call("GET", "/v1/settings/mcp"),
                               lambda n: api.call("POST", f"/v1/settings/mcp/{quote(n, safe='')}/login"))
            settings, pool = _local_settings()
            try:
                return cmd_mcp(rest, settings.mcp_list, settings.mcp_login)
            finally:
                pool.close()
        except (ClientError, Exception) as e:          # BadRequest 等：说清楚就退出
            print(str(e), file=sys.stderr)
            return 1
    if api is None:
        print("weaverd 没在运行。启动：weaver daemon start（或 weaver daemon install 设成开机自启）", file=sys.stderr)
        return 2
    try:
        if cmd == "tasks":
            return cmd_tasks(api)
        if cmd == "waits":
            return cmd_waits(api)
        if not rest:
            print(f"用法：weaver {cmd} <{'等待 id' if cmd == 'approve' else '任务 id'}>", file=sys.stderr)
            return 2
        if cmd == "attach":
            return attach(api, rest[0])
        if cmd == "approve":
            decision = {"y": "allow", "n": "deny", "a": "always"}.get(rest[1], rest[1]) if len(rest) > 1 else "allow"
            api.call("POST", f"/v1/waits/{rest[0]}", {"decision": decision})
            print("已回答")
            return 0
        task_id = api.resolve_task(rest[0])
        if cmd == "cancel":
            api.call("POST", f"/v1/tasks/{task_id}/cancel")
            print(f"已取消 {task_id[:8]}")
        elif cmd == "archive":
            api.call("DELETE", f"/v1/tasks/{task_id}")
            print(f"已归档 {task_id[:8]}")
        return 0
    except ClientError as e:
        print(str(e), file=sys.stderr)
        return 1
