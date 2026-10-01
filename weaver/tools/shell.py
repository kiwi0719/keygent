"""bash：在工作目录执行一条命令。见 design/write-tools.md 第二节。

- 每条命令一个新 shell，工作目录固定为项目根目录。
- 环境变量里名字像密钥的都去掉，免得一条 env 就把 API key 打进上下文。
- 输出保留结尾（报错通常在最后），超长的全文存到 .weaver/outputs/。
- 在沙箱里执行；被沙箱拦下时提示模型别反复重试。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .. import redact as rd
from ..sandbox import DENIED_HINT, STARTUP_FAILED

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_OUTPUT = 30_000
SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)", re.I)


def clean_env(env: dict | None = None) -> dict:
    env = dict(os.environ if env is None else env)
    return {k: v for k, v in env.items() if not SECRET_ENV.search(k)}


class Running:
    """正在跑的前台命令（进程组）。取消时整组结束：线程没法从外面杀，就杀它在等的子进程。"""

    def __init__(self):
        self._procs: set = set()
        self._lock = threading.Lock()
        self.interrupted: set[int] = set()

    def add(self, proc) -> None:
        with self._lock:
            self._procs.add(proc)

    def remove(self, proc) -> None:
        with self._lock:
            self._procs.discard(proc)

    def interrupt(self) -> int:
        with self._lock:
            procs = list(self._procs)
            self.interrupted.update(p.pid for p in procs)
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        if procs:
            threading.Timer(2.0, self._force, args=(procs,)).start()
        return len(procs)

    @staticmethod
    def _force(procs) -> None:
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


def run_bash(command: str, root: Path, sandbox=None, timeout: int = DEFAULT_TIMEOUT,
             outputs_dir: Path | None = None, running: Running | None = None) -> str:
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    argv = ["/bin/bash", "-c", command]
    if sandbox:
        argv = sandbox.wrap(argv)
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=root, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    if running:
        running.add(proc)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)        # 连同子进程一起杀
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
    finally:
        if running:
            running.remove(proc)
    cancelled = bool(running) and proc.pid in running.interrupted
    text = out.decode("utf-8", "replace")
    elapsed = time.monotonic() - started

    notes = []
    if len(text.encode()) > MAX_OUTPUT:
        saved = ""
        if outputs_dir:
            outputs_dir.mkdir(parents=True, exist_ok=True)
            f = outputs_dir / f"bash-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.log"
            f.write_text(rd.default().redact(text)[0], encoding="utf-8")   # 存盘前也脱敏
            try:
                saved = f"，完整输出在 {f.relative_to(root)}（可以用 read_file 或 grep 看）"
            except ValueError:
                saved = f"，完整输出在 {f}"
        tail = text.encode()[-MAX_OUTPUT:].decode("utf-8", "ignore")
        text = tail[tail.find("\n") + 1:] if "\n" in tail else tail
        notes.append(f"输出太长，只保留最后 {MAX_OUTPUT // 1000}KB{saved}")
    if timed_out:
        notes.append(f"超过 {timeout} 秒被终止")
    if cancelled:
        notes.append("任务被取消，命令被终止")
    body = text.rstrip() or "(命令执行完毕，没有输出)"
    status = "超时" if timed_out else f"退出码 {proc.returncode}"
    result = f"{body}\n\n[{status}，用时 {elapsed:.1f} 秒" + ("；" + "；".join(notes) if notes else "") + "]"
    if sandbox and sandbox.kind == "bwrap" and text.startswith("bwrap:"):
        result += STARTUP_FAILED                 # bwrap 自己没起来，不是命令被拦
    elif sandbox and sandbox.kind and ("Operation not permitted" in text or "Read-only file system" in text):
        result += DENIED_HINT                    # macOS 报前者，Linux 的只读挂载报后者
    return result
