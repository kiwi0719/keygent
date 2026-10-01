"""任务管理器：工作线程、醒来 / 睡着、并发上限、回答等待、启动时恢复。见 design/daemon.md 第五节。

一个容量为 max_running 的线程池 + 每个任务三个标记（排队中、在跑、跑完再看一眼）。
不跑的任务不占线程。每个任务一个常驻的执行者实例，所有写账本的操作都经过它。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .. import kernel as k
from ..errors import BadRequest, Conflict, NotFound  # noqa: F401  （接口层从这里导入）
from ..permissions import command_prefix, mcp_key
from .status import summarize, waits as list_waits
from .tasks import LEDGER, TaskMeta, TaskStore

log = logging.getLogger("weaverd")
MAX_RUNNING = 4                                  # 默认值；weaverd 启动时在读了 .env 之后看 WEAVER_MAX_RUNNING


class _Skip(Exception):
    pass


# 造执行者：factory(meta, store, sink, on_delta) -> Runner。store 是这个任务的账本存储，会话名固定为 LEDGER
Factory = Callable[..., object]
# 对外通知：listener(kind, task_id, data)，kind 是 ledger / delta / task
Listener = Callable[[str, str, dict], None]


class TaskManager:
    def __init__(self, store: TaskStore, factory: Factory, max_running: int = MAX_RUNNING, uploads=None):
        self.store, self.factory, self.uploads = store, factory, uploads
        self.corpus = None                         # 跨任务搜索（weaver/corpus.py），给 /v1/search 用
        self.max_running = max(1, max_running)
        self._pool = ThreadPoolExecutor(self.max_running, thread_name_prefix="weaver-task")
        self._lock = threading.RLock()
        self._runners: dict[str, object] = {}
        self._op_locks: dict[str, threading.RLock] = {}  # 同一个任务的操作（插话、放行、取消、归档）排队做
        self._gone: set[str] = set()                     # 已归档：之后的操作一律“没有这个任务”
        self._trust: dict[str, object] = {}              # 任务 → ProjectTrust（项目级 skill / 子 Agent 类型要先信任）
        self._queued: set[str] = set()
        self._running: set[str] = set()
        self._dirty: set[str] = set()
        self._archive_after: set[str] = set()
        self._idle = threading.Condition(self._lock)
        self._listeners: list[Listener] = []
        self._closed = False

    # ------------------------------------------------ 对外通知

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _emit(self, kind: str, task_id: str, data: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, task_id, data)
            except Exception:                       # 一个监听者出错不影响别的、也不影响任务
                log.exception("listener 出错")

    # 这些账本事件会改变任务摘要（状态、now、在等几件）
    _SUMMARY_EVENTS = {"RunStarted", "RunFinished", "WaitStarted", "WaitResolved", "InputJudged",
                       "ActionStarted", "Cancelled"}

    def _on_ledger(self, task_id: str, ev: dict) -> None:
        self._emit("ledger", task_id, ev)
        if ev["type"] in self._SUMMARY_EVENTS:
            self._emit_summary(task_id)

    def _emit_summary(self, task_id: str) -> None:
        try:
            self._emit("task", task_id, self.summary(task_id))
        except NotFound:
            pass

    # ------------------------------------------------ 执行者

    def runner(self, task_id: str):
        with self._lock:
            if task_id in self._gone:
                raise NotFound(f"没有这个任务：{task_id}")
            r = self._runners.get(task_id)
            if r is None:
                meta = self.store.get(task_id)
                if meta is None:
                    raise NotFound(f"没有这个任务：{task_id}")
                r = self.factory(meta, self.store.store(task_id),
                                 lambda ev: self._on_ledger(task_id, ev),
                                 lambda d: self._emit("delta", task_id, d))
                self._runners[task_id] = r
                self._trust[task_id] = getattr(r, "project_trust", None)
                jobs = getattr(getattr(r, "tools", None), "jobs", None)
                if jobs is not None:                 # 后台命令结束时：往任务里记一条系统输入、叫醒它
                    jobs.notify = lambda text: self._system_input(task_id, text)
            return r

    def _system_input(self, task_id: str, text: str) -> None:
        try:
            with self._op(task_id) as r:
                r.submit(text, source="system")
                self.wake(task_id)
        except NotFound:
            pass

    @staticmethod
    def _jobs(r):
        return getattr(getattr(r, "tools", None), "jobs", None)

    @contextmanager
    def _op(self, task_id: str):
        """对一个任务做一件事：和同一任务的其它操作排队；已归档或正要归档的报“没有这个任务”。"""
        with self._lock:
            lock = self._op_locks.setdefault(task_id, threading.RLock())
        with lock:
            with self._lock:
                if task_id in self._archive_after:
                    raise NotFound(f"任务正在归档：{task_id}")
            yield self.runner(task_id)

    # ------------------------------------------------ 醒来 / 睡着

    def wake(self, task_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            if task_id in self._running or task_id in self._queued:
                self._dirty.add(task_id)            # 跑完再看一眼，不重复排队
                return
            self._queued.add(task_id)
        self._emit_summary(task_id)
        self._pool.submit(self._work, task_id)

    def _work(self, task_id: str) -> None:
        with self._lock:
            self._queued.discard(task_id)
            self._running.add(task_id)
        try:
            try:
                r = self.runner(task_id)
            except NotFound:                        # 排队期间被归档了
                with self._lock:
                    self._running.discard(task_id)
                    self._idle.notify_all()
                return
            while True:
                with self._lock:
                    self._dirty.discard(task_id)
                    skip = task_id in self._archive_after and not self._active(task_id)
                try:
                    if skip:                        # 要归档、又没有进行中的一轮：不必再开始新的
                        raise _Skip
                    r.run()
                except _Skip:
                    pass
                except Exception as e:              # 代码 bug：别让任务永远卡在“在跑”
                    log.exception("任务 %s 出错", task_id)
                    self._crashed(r, e)
                with self._lock:
                    extract = task_id not in self._dirty and task_id not in self._archive_after and not self._closed
                if extract:                         # 一轮成功结束、没有新输入：提取记忆（期间来的插话等它做完）
                    try:
                        getattr(r, "maybe_extract", lambda: None)()
                    except Exception:
                        log.exception("任务 %s 提取记忆出错", task_id)
                with self._lock:
                    if task_id not in self._dirty or self._closed:
                        archive = task_id in self._archive_after
                        break
            if archive:
                self._do_archive(task_id)
            else:
                self._emit_summary(task_id)
            with self._lock:
                self._running.discard(task_id)
                # 上面决定“不用再看了”到这里之间，任务还算“在跑”，这段时间里来的叫醒只记了 dirty：
                # 不接住就丢了（放行了却没人去执行）
                again = task_id in self._dirty and not self._closed and task_id not in self._gone
                if again:
                    self._queued.add(task_id)
                self._idle.notify_all()
            if again:
                self._pool.submit(self._work, task_id)
        except Exception:
            log.exception("任务 %s 的工作线程出错", task_id)
            with self._lock:
                self._running.discard(task_id)
                self._idle.notify_all()

    def _active(self, task_id: str) -> bool:
        s = k.fold(self.events(task_id))
        return bool(s.run and s.run.active)

    def _crashed(self, r, e: Exception) -> None:
        state = k.fold(r.store.load(r.session_id))
        if state.run and state.run.active:
            first = (str(e).strip().split("\n") or [""])[0]
            r._append([r._event("RunFinished", run_id=state.run.id, status="error",
                                text=f"服务内部出错：{type(e).__name__}: {first}")])

    def busy(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._running or task_id in self._queued

    def wait_idle(self, task_id: str | None = None, timeout: float | None = None) -> bool:
        """等某个任务（或全部）睡着。测试和关闭时用。"""
        with self._idle:
            return self._idle.wait_for(
                lambda: not (self._running | self._queued if task_id is None else
                             {task_id} & (self._running | self._queued)), timeout)

    # ------------------------------------------------ 任务操作（给接口用）

    def _content(self, text: str, attachments: list[str] | None, workdir: str) -> list[dict] | str:
        if not attachments:
            return text
        if self.uploads is None:
            raise BadRequest("这个服务不支持附件")
        try:
            return self.uploads.content(text or "", list(attachments), workdir)
        except KeyError as e:
            raise BadRequest(f"没有这个附件：{e.args[0]}") from None

    def _check_attachments(self, attachments) -> None:
        for i in attachments or []:
            if self.uploads is None or self.uploads.get(i) is None:
                raise BadRequest(f"没有这个附件：{i}")

    def create(self, text: str, workdir: str | None = None, attachments: list[str] | None = None,
               source: str = "user", **opts) -> dict:
        if not (text or "").strip() and not attachments:
            raise BadRequest("text 不能为空")
        self._check_attachments(attachments)
        try:
            meta = self.store.create(text, workdir=workdir, source=source, **opts)
        except ValueError as e:
            raise BadRequest(str(e)) from None
        try:
            self.runner(meta.id).submit(self._content(text, attachments, meta.workdir), source=source)
        except Exception:
            self.store.archive(meta.id)             # 造不出执行者（比如没配模型）：别留下一个空任务
            with self._lock:
                self._runners.pop(meta.id, None)
            raise
        self.wake(meta.id)
        return self.summary(meta.id)

    def input(self, task_id: str, text: str, attachments: list[str] | None = None) -> dict:
        if not (text or "").strip() and not attachments:
            raise BadRequest("text 不能为空")
        self._check_attachments(attachments)
        with self._op(task_id) as r:
            r.submit(self._content(text, attachments, self.store.get(task_id).workdir))
            self.wake(task_id)
        return self.summary(task_id)

    def cancel(self, task_id: str, reason: str = "用户取消") -> dict:
        with self._op(task_id) as r:
            self._cancel(r, task_id, reason)
        return self.summary(task_id)

    def _cancel(self, r, task_id: str, reason: str) -> None:
        s = k.fold(self.events(task_id))
        if s.run and s.run.active and not s.run.cancelled:
            r.cancel(reason)
            self.wake(task_id)

    def archive(self, task_id: str) -> None:
        with self._op(task_id) as r:
            with self._lock:
                later = task_id in self._running or task_id in self._queued
                if later:
                    self._archive_after.add(task_id)
            if later:                               # 在跑：先取消，停下后由工作线程移走
                self._cancel(r, task_id, "任务被归档")
            else:
                self._do_archive(task_id)

    def _do_archive(self, task_id: str) -> None:
        with self._lock:
            lock = self._op_locks.setdefault(task_id, threading.RLock())
        with lock:
            with self._lock:
                self._gone.add(task_id)
                self._archive_after.discard(task_id)
                r = self._runners.pop(task_id, None)
            if r is not None and self._jobs(r) is not None:
                self._jobs(r).close("任务被归档")    # 它启动的后台命令一起结束
            self.store.archive(task_id)
        self._emit("archived", task_id, {})

    def rename(self, task_id: str, title: str) -> dict:
        if not (title or "").strip():
            raise BadRequest("title 不能为空")
        with self._op(task_id):
            self.store.set_title(task_id, title)
        self._emit_summary(task_id)
        return self.summary(task_id)

    # ------------------------------------------------ 等你的事

    # “信任这个项目吗？”不在账本里（不和哪一轮绑定，也不挡着任务往下跑）：由任务管理器按需算出来
    def _trust_item(self, task_id: str) -> dict | None:
        pt = self._trust.get(task_id)
        p = pt.pending() if pt else None
        meta = self.store.get(task_id) if p else None
        if not p or meta is None or self._denied().get(p["root"]) == p["digest"]:
            return None
        with self._lock:                            # 信任按项目算：同一个项目只问一次，挂在最早的那个任务上
            owners = [t for t, other in self._trust.items() if other is not None and t != task_id
                      and str(other.agents.root) == p["root"] and self.store.get(t) is not None]
        if any(self.store.get(t).created < meta.created for t in owners):
            return None
        servers = p.get("mcp") or []
        parts = ([f"skill：{'、'.join(p['skills'])}"] if p["skills"] else []) + \
                ([f"子 Agent 类型：{'、'.join(p['agents'])}"] if p["agents"] else []) + \
                ([f"MCP 服务器：{'、'.join(servers)}"] if servers else [])
        what = "、".join((["skill"] if p["skills"] else []) + (["子 Agent 类型"] if p["agents"] else []) +
                        (["MCP 服务器"] if servers else []))
        why = ((["skill 和子 Agent 类型是项目里别人写的指令，信任后会给模型看"] if p["skills"] or p["agents"] else []) +
               (["MCP 服务器会在你的电脑上运行上面的命令（或连接那个地址）"] if servers else []))
        return {"id": f"trust-{task_id}", "seq": 0, "ts": meta.created, "kind": "trust",
                "title": f"信任项目 {Path(p['root']).name} 里的{' ' if what[0].isascii() else ''}{what}吗？",
                "root": p["root"],
                "body": "；".join(parts) + "。" + "；".join(why) + "；没信任之前先不加载。",
                "choices": ["allow", "deny"]}

    def _denied(self) -> dict:
        try:
            return json.loads((self.store.root / ".trust-denied.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def task_waits(self, task_id: str, events: list[dict] | None = None, state=None) -> list[dict]:
        """这个任务里在等你的事：账本里的等待 + 信任确认。"""
        events = self.events(task_id) if events is None else events
        items = list_waits(state or k.fold(events), events)
        trust = self._trust_item(task_id)
        return items + ([trust] if trust else [])

    def _answer_trust(self, task_id: str, decision: str) -> dict:
        with self._op(task_id):
            item = self._trust_item(task_id)
            if item is None:
                raise Conflict("这件等待已经被回答了")
            if decision not in item["choices"]:
                raise BadRequest(f"这件等待不能选 {decision}，可选：allow、deny")
            pt = self._trust[task_id]
            if decision == "allow":
                pt.trust()                          # 和命令行共用同一份信任记录；下一轮开始时出现在背景信息里
            else:                                   # 这个项目不再问（内容变了会再问）
                p = pt.pending()
                denied = self._denied()
                denied[p["root"]] = p["digest"]
                path = self.store.root / ".trust-denied.json"
                path.write_text(json.dumps(denied, ensure_ascii=False, indent=1), encoding="utf-8")
        self._emit_summary(task_id)
        return self.summary(task_id)

    def answer(self, wait_id: str, decision: str, args: dict | None = None, note: str = "") -> dict:
        if wait_id.startswith("trust-"):
            task_id = wait_id[len("trust-"):]
            if self.store.get(task_id) is None:
                raise NotFound(f"没有这件等待：{wait_id}")
            self.runner(task_id)
            return self._answer_trust(task_id, decision)
        task_id, w = self._find_wait(wait_id)
        if w is None:
            raise NotFound(f"没有这件等待：{wait_id}")
        if args is not None and (not isinstance(args, dict) or decision != "allow"):
            raise BadRequest("改参数只能配 allow，args 是改过的完整参数（一个对象）")
        with self._op(task_id) as r:
            events = self.events(task_id)
            state = k.fold(events)
            item = next((x for x in list_waits(state, events) if x["id"] == wait_id), None)   # 账本里的等待
            if item is None:
                raise Conflict("这件等待已经被回答或作废了")
            if decision not in item["choices"]:
                raise BadRequest(f"这件等待不能选 {decision}，可选：{'、'.join(item['choices'])}")
            if decision == "answer" and not note.strip():
                raise BadRequest("回答不能为空；不想回答就选 skip，让它自己定")
            value = self._value(item, decision, note)
            if args is not None:
                if "edit" not in item["choices"]:
                    raise BadRequest("这件等待不能改参数")
                value["args"] = args
            r.resolve(wait_id, value)
            self.wake(task_id)
        return self.summary(task_id)

    @staticmethod
    def _value(item: dict, decision: str, note: str) -> dict:
        if decision == "answer":
            return {"allow": True, "answer": note.strip()}
        if decision == "skip":
            return {"allow": True, "skip": True}
        if decision in ("allow", "continue"):
            return {"allow": True}
        if decision == "hint":
            return {"allow": True, "note": note}
        if decision == "always":
            call = item["call"]
            key = (command_prefix(call["args"].get("command", "")) if call["name"] == "bash"
                   else mcp_key(call["name"], call["args"]))
            return {"allow": True, "always": key}
        return {"allow": False, "note": note or ("用户选择停下" if decision == "stop" else "用户拒绝")}

    def _find_wait(self, wait_id: str) -> tuple[str | None, k.Wait | None]:
        for meta in self.store.list():
            for e in reversed(self.events(meta.id)):
                if e["type"] == "WaitStarted" and e["wait_id"] == wait_id:
                    return meta.id, k.Wait(wait_id, e["kind"], e.get("payload") or {})
        return None, None

    def waits(self) -> list[dict]:
        out = []
        for meta in self.store.list():
            for w in self.task_waits(meta.id):
                out.append({**w, "task": meta.id, "task_title": meta.title})
        return sorted(out, key=lambda w: w["ts"])

    # ------------------------------------------------ 查询

    def events(self, task_id: str) -> list[dict]:
        return self.store.events(task_id)

    def summary(self, task_id: str, meta: TaskMeta | None = None) -> dict:
        meta = meta or self.store.get(task_id)
        if meta is None:
            raise NotFound(f"没有这个任务：{task_id}")
        events = self.events(task_id)
        s = summarize(events)
        status = s["status"]
        waiting = len(s["waiting"]) + (1 if self._trust_item(task_id) else 0)
        with self._lock:
            if task_id in self._queued and task_id not in self._running and status in ("running", "idle"):
                status = "queued"
        return {"id": meta.id, "title": meta.title, "status": status, "note": s["note"],
                "now": "排队中" if status == "queued" else s["now"],
                "created": meta.created, "updated": self.store.updated(task_id),
                "waiting": waiting, "workdir": meta.workdir}

    def list(self) -> list[dict]:
        return [self.summary(m.id, m) for m in self.store.list()]

    def detail(self, task_id: str, mark_seen: bool = True) -> dict:
        from .humanize import steps
        meta = self.store.get(task_id)
        if meta is None:
            raise NotFound(f"没有这个任务：{task_id}")
        if mark_seen:                               # 打开看过：胶囊不再因为它的出错变红
            with self._op(task_id):                 # 和归档排队，免得往移走的目录里写
                meta = self.store.get(task_id) or meta
                meta.extra["seen"] = time.time()
                self.store.save(meta)
        events = self.events(task_id)
        state = k.fold(events)
        s = summarize(events, state)
        r = state.run
        return {"task": self.summary(task_id, meta), "steps": steps(events),
                "waiting": [{**w, "task": meta.id, "task_title": meta.title}
                            for w in self.task_waits(task_id, events, state)],
                "final": s["final"],
                "usage": {"tokens": r.tokens if r else 0, "budget": meta.token_budget,
                          "steps": r.steps if r else 0, "max_steps": meta.max_steps,
                          "extract_tokens": sum(k.billable((e.get("output") or {}).get("usage") or {})
                                                for e in events if e["type"] == "ActionCompleted"
                                                and e.get("kind") == "extract")},
                "jobs": self._jobs_list(task_id)}

    def _jobs_list(self, task_id: str) -> list[dict]:
        """这个任务的后台命令和后台子 Agent（给 Keygent 显示“后台还在跑什么”）。"""
        with self._lock:
            r = self._runners.get(task_id)
        jobs = self._jobs(r) if r is not None else None
        if jobs is not None:
            with jobs._lock:
                items = [j.public() for j in jobs.jobs.values()]
        else:                                       # 这次启动还没碰过这个任务：读磁盘上的登记表
            try:
                items = json.loads((self.store.dir(task_id) / "jobs" / "registry.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
        return [{"id": j["id"], "kind": j.get("kind", "shell"), "title": j.get("command", ""),
                 "status": j.get("status", "?"), "started": j.get("started", 0), "ended": j.get("ended")}
                for j in sorted(items, key=lambda j: j.get("started", 0))]

    def status(self) -> dict:
        metas = {m.id: m for m in self.store.list()}
        tasks = [self.summary(i, m) for i, m in metas.items()]
        ws = self.waits()
        ws = [w for w in ws if w["kind"] == "question"] + [w for w in ws if w["kind"] != "question"]  # 问你的先提醒
        err = next((t for t in tasks if t["status"] == "error"
                    and metas[t["id"]].extra.get("seen", 0) < t["updated"]), None)   # 出错之后还没看过的
        return {"running": sum(t["status"] == "running" for t in tasks),
                "queued": sum(t["status"] == "queued" for t in tasks),
                "waiting": len(ws),
                "first_wait": ({"task": ws[0]["task"], "task_title": ws[0]["task_title"], "title": ws[0]["title"],
                                "kind": ws[0]["kind"]}
                               if ws else None),
                "error": {"task": err["id"], "task_title": err["title"], "note": err["note"]} if err else None}

    # ------------------------------------------------ 启动 / 关闭

    def recover(self) -> list[str]:
        """启动时：有进行中的一轮或没处理的输入就接着跑；在等的继续等。返回被唤醒的任务。"""
        woken = []
        for meta in self.store.list():
            reg = self.store.dir(meta.id) / "jobs" / "registry.json"
            if reg.exists() and '"running"' in reg.read_text(encoding="utf-8"):
                try:
                    self.runner(meta.id)             # 造执行者时会清理上次残留的后台命令
                except Exception:
                    log.exception("清理任务 %s 的后台命令失败", meta.id)
            s = k.fold(self.events(meta.id))
            active = bool(s.run and s.run.active)
            if (active and not s.open_waits) or s.unjudged or (not active and s.new_inputs()):
                try:
                    self.runner(meta.id)
                except Exception:
                    log.exception("恢复任务 %s 失败", meta.id)
                    continue
                self.wake(meta.id)
                woken.append(meta.id)
        return woken

    def close(self, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
            runners = list(self._runners.values())
        for r in runners:                            # 先结束命令（前台、后台），工作线程才能尽快停下
            interrupt = getattr(getattr(r, "tools", None), "interrupt", None)
            if interrupt:
                interrupt()
            if self._jobs(r) is not None:
                self._jobs(r).close("weaverd 停止")
        self._pool.shutdown(wait=wait, cancel_futures=True)
