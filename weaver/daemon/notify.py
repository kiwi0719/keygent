"""系统通知兜底：Keygent 没开时，用 macOS 通知提醒“需要你”和“长任务做完了”。见 design/daemon.md 第五节。

- 新的等待：按 id 去重
- 出错：状态变成 error 时
- 完成：状态变成 done，而且这一轮跑了 LONG_TASK 秒以上
- 有连着事件流的 App（不是命令行）就不发，由它自己提醒
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable

log = logging.getLogger("weaverd")
LONG_TASK = 60.0


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def osascript(title: str, body: str, subtitle: str = "") -> None:
    script = f"display notification {_quote(body)} with title {_quote(title)}"
    if subtitle:
        script += f" subtitle {_quote(subtitle)}"

    def run():
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            log.warning("发系统通知失败")
    threading.Thread(target=run, daemon=True).start()


class Notifier:
    def __init__(self, manager, app_clients: Callable[[], int], send=osascript, long_task: float = LONG_TASK,
                 clock=time.time):
        self.app_clients, self.send, self.long_task, self.clock = app_clients, send, long_task, clock
        self._status: dict[str, str] = {}
        self._started: dict[str, float] = {}
        self._waits: set[str] = set()
        self._lock = threading.Lock()
        self._unsub = manager.subscribe(self._on)
        self._mgr = manager

    def close(self) -> None:
        self._unsub()

    def _notify(self, title: str, body: str, subtitle: str = "") -> None:
        if self.app_clients() > 0:
            return
        self.send(title, body, subtitle)

    def _on(self, kind: str, task_id: str, data: dict) -> None:
        if kind == "task":
            self._on_summary(task_id, data)
        elif kind == "archived":
            with self._lock:
                self._status.pop(task_id, None)
                self._started.pop(task_id, None)

    def _on_summary(self, task_id: str, s: dict) -> None:
        now = self.clock()
        with self._lock:
            old = self._status.get(task_id)
            self._status[task_id] = s["status"]
            if s["status"] in ("running", "queued") and old not in ("running", "queued", "waiting"):
                self._started[task_id] = now                 # 这一轮开始（排队也算，等你的时间也算）
            started = self._started.get(task_id)
        if s["status"] == "waiting" and s.get("waiting"):
            for w in self._mgr.detail(task_id, mark_seen=False)["waiting"]:
                with self._lock:
                    if w["id"] in self._waits:
                        continue
                    self._waits.add(w["id"])
                verb = {"approval": "等你放行", "stuck": "好像卡住了", "input": "有输入待确认", "question": "问你"}.get(w["kind"], "等你")
                self._notify("Weaver", w["title"], f"{s['title']} · {verb}")
        elif s["status"] == "error" and old != "error":
            self._notify("Weaver", s["note"] or "出错了", f"{s['title']} · 出错了")
        elif s["status"] == "done" and old != "done" and started is not None and now - started >= self.long_task:
            self._notify("Weaver", s["now"] or "完成", f"{s['title']} · 做完了")
