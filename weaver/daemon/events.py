"""事件总线：把任务管理器的通知翻译成 api.md 的事件，带全局游标，供 SSE 订阅。见 design/daemon.md 第六节。

- step：账本有新事件 → 重新翻译步骤，新增的和变了的发出去
- wait / wait_closed：任务摘要变了 → 比较“在等的事”的 id 集合
- task / archived 原样转；delta 不带游标、不进缓冲区
- 最近 BUFFER 条在内存里；每个订阅者一个有上限的队列，满了就断开它
"""
from __future__ import annotations

import queue
import threading
from collections import deque

from .humanize import steps as to_steps

BUFFER = 10_000
CLIENT_QUEUE = 1_000


class Subscriber:
    def __init__(self, size: int = CLIENT_QUEUE):
        self.q: queue.Queue = queue.Queue(size)
        self.dropped = False                 # 队列满了：让它断开重连

    def put(self, item) -> None:
        if self.dropped:
            return
        try:
            self.q.put_nowait(item)
        except queue.Full:
            self.dropped = True
            try:                             # 叫醒写线程，让它发现自己被断开了
                self.q.get_nowait()
                self.q.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass


class EventBus:
    def __init__(self, manager, buffer: int = BUFFER):
        self.mgr = manager
        self._lock = threading.Lock()
        self._cursor = 0
        self._buf: deque = deque(maxlen=buffer)         # (cursor, kind, data)
        self._subs: list[Subscriber] = []
        self._steps: dict[str, list[dict]] = {}         # 任务 → 上次发出去的步骤
        self._waits: dict[str, set[str]] = {}           # 任务 → 上次在等的事
        self._task_locks: dict[str, threading.Lock] = {}
        self._unsub = manager.subscribe(self._on)

    def close(self) -> None:
        self._unsub()
        with self._lock:
            for s in self._subs:
                s.put(None)
            self._subs.clear()

    # ------------------------------------------------ 发布

    @property
    def cursor(self) -> int:
        return self._cursor

    def publish(self, kind: str, data: dict) -> None:
        with self._lock:
            if kind == "delta":
                item = (None, kind, data)
            else:
                self._cursor += 1
                item = (self._cursor, kind, data)
                self._buf.append(item)
            for s in self._subs:
                s.put(item)
            self._subs = [s for s in self._subs if not s.dropped]

    def _tlock(self, task_id: str) -> threading.Lock:
        with self._lock:
            return self._task_locks.setdefault(task_id, threading.Lock())

    def _on(self, kind: str, task_id: str, data: dict) -> None:
        if kind == "delta":
            self.publish("delta", {"task": task_id, **data})
        elif kind == "ledger":
            self._diff_steps(task_id)
        elif kind == "task":
            self.publish("task", {"task": task_id, "summary": data})
            self._diff_waits(task_id)
        elif kind == "archived":
            with self._tlock(task_id):
                closed = self._waits.pop(task_id, set())
                self._steps.pop(task_id, None)
            for wid in sorted(closed):
                self.publish("wait_closed", {"task": task_id, "wait_id": wid})
            self.publish("archived", {"task": task_id})

    def _diff_steps(self, task_id: str) -> None:
        with self._tlock(task_id):           # 同一个任务的步骤按顺序比，免得两个线程交错发
            new = to_steps(self.mgr.events(task_id))
            old = self._steps.get(task_id)
            if old is None:                  # 这次运行里第一次见到它：只发最后一个（之前的客户端用详情接口拿）
                changed = new[-1:]
            else:
                changed = [st for i, st in enumerate(new) if i >= len(old) or old[i] != st]
            self._steps[task_id] = new
            for st in changed:
                self.publish("step", {"task": task_id, "step": st})

    def _diff_waits(self, task_id: str) -> None:
        meta = self.mgr.store.get(task_id)
        with self._tlock(task_id):
            events = self.mgr.events(task_id)
            items = {w["id"]: w for w in self.mgr.task_waits(task_id, events)}
            old = self._waits.get(task_id, set())
            self._waits[task_id] = set(items)
            for wid in sorted(set(items) - old, key=lambda x: items[x]["seq"]):
                self.publish("wait", {"task": task_id, "wait": {**items[wid], "task": task_id,
                                                                 "task_title": meta.title if meta else ""}})
            for wid in sorted(old - set(items)):
                self.publish("wait_closed", {"task": task_id, "wait_id": wid})

    # ------------------------------------------------ 订阅

    def subscribe(self, after: int | None) -> tuple[Subscriber, list, bool]:
        """返回 (订阅者, 要补发的事件, 是否需要客户端重新拉全量)。先登记再拷缓冲区，靠游标去重。"""
        sub = Subscriber()
        with self._lock:
            self._subs.append(sub)
            backlog = [it for it in self._buf if after is not None and it[0] > after]
            oldest = self._buf[0][0] if self._buf else self._cursor + 1
            reset = after is not None and (after > self._cursor or after < oldest - 1)
        if reset:
            backlog = []
        return sub, backlog, reset

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
