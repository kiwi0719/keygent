"""“现在是哪个任务在调这个服务器”：服务器在调用中途提问、借模型、问 roots 时，靠它找到发起调用的任务。
见 design/mcp2.md 第二节。

所有任务共用一个连接，所以：
- 每次调用登记一个 Call（谁在调、这次调用借模型用了多少 token、正在等人的次数），调完注销；
- 新协议里提问是调用结果的一部分，SDK 在 call_tool 里调回调——contextvar 直接拿到；
- 旧协议里提问是服务器单独发来的请求，查这个服务器上正在进行的调用：一个就是它，几个就交给最近开始的那个，
  一个都没有就拒绝。
"""
from __future__ import annotations

import contextvars
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Caller:
    """一个任务。ask(kind, payload) 在任务里记一件等待、阻塞到有回答（Runner.ask_during_call）；
    sample(params) 用这个任务的模型生成（返回 (文字, usage)）；root 是任务的工作目录。"""
    ask: Callable[[str, dict], object]
    root: str = ""
    sample: Callable[[dict], tuple[str, dict]] | None = None
    task: str = ""
    allowed: Callable[[str], bool] = lambda key: False     # 这个任务里点过“总是允许”的（借模型用）


@dataclass
class Call:
    caller: Caller
    server: str
    started: float = field(default_factory=time.monotonic)
    waiting: int = 0                     # 正在等人回答：调用超时暂停计时
    usage: dict = field(default_factory=dict)


current: contextvars.ContextVar[Call | None] = contextvars.ContextVar("weaver_mcp_call", default=None)


class Calls:
    def __init__(self):
        self._lock = threading.Lock()
        self._live: dict[str, list[Call]] = {}

    def begin(self, server: str, caller: Caller) -> Call:
        c = Call(caller, server)
        with self._lock:
            self._live.setdefault(server, []).append(c)
        return c

    def end(self, c: Call) -> None:
        with self._lock:
            live = self._live.get(c.server, [])
            if c in live:
                live.remove(c)

    def find(self, server: str) -> Call | None:
        """提问 / 借模型来了：先看 contextvar（新协议），再看这个服务器上正在进行的调用（最近开始的）。"""
        c = current.get()
        if c is not None and c.server == server:
            return c
        with self._lock:
            live = list(self._live.get(server, []))
        return max(live, key=lambda x: x.started) if live else None
