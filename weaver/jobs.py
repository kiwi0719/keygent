"""后台命令登记表：bash(background=true)、job_output、job_kill、jobs。见 design/round2.md 第三节、design/daemon.md 第五节。

- 输出直接写文件（不走管道，免得管道满了进程卡住）；日志上限 10 MB，超过只留后一半
- 回收线程每秒看一次：结束了就记下退出码、关句柄、通知（往任务里记一条 system 输入）
- 连续 IDLE_LIMIT 秒没有新输出的自动结束
- PID 和进程组登记在 registry.json；进程被强杀后重启时，按登记清理上次残留的
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import redact as rd
from .tools.shell import MAX_OUTPUT, clean_env

RUNTIME = ("proc", "thread", "cancel_fn", "progress_fn", "done")   # 只在内存里，不进登记表
START_WAIT = 3.0
LOG_LIMIT = 10 * 1024 * 1024
IDLE_LIMIT = 24 * 3600
KEEP = 100                                   # 登记表里最多留多少条已结束的
TAIL_LINES = 20


@dataclass
class Job:
    id: str
    command: str
    started: float
    pid: int = 0
    status: str = "running"                  # running / done / failed / killed
    exit_code: int | None = None
    ended: float | None = None
    reason: str = ""                         # killed 的原因
    ident: str = ""                          # 进程启动时间（ps lstart）：重启后核对“还是不是它”
    read_pos: int = 0                        # job_output 读到哪了
    notify: bool = True
    quiet: bool = False                      # 启动的头几秒：结束了直接写进返回结果，不另外通知
    kill_reason: str = ""                    # 正在被我们结束（回收线程可能先看到它退出）
    kill_notify: bool = False
    kind: str = "shell"                      # shell：后台命令；agent：后台子 Agent
    report: str = ""                         # 后台子 Agent 的汇报
    waited: bool = False                     # 主 Agent 正在 job_wait 等它：结束时不另外通知
    proc: object = field(default=None, repr=False, compare=False)
    thread: object = field(default=None, repr=False, compare=False)
    cancel_fn: object = field(default=None, repr=False, compare=False)
    progress_fn: object = field(default=None, repr=False, compare=False)
    done: object = field(default=None, repr=False, compare=False)

    def public(self) -> dict:
        return {f: getattr(self, f) for f in self.__dataclass_fields__ if f not in RUNTIME}


def _ps(pid: int) -> tuple[str, str]:
    """(进程组, 启动时间)；进程不在了返回空。"""
    try:
        out = subprocess.run(["ps", "-o", "pgid=,lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "", ""
    pgid, _, started = out.partition(" ")
    return pgid.strip(), " ".join(started.split())


def _alive_group_leader(pid: int, ident: str) -> bool:
    """这个 PID 还是不是我们当初启动的那个进程组组长：组长是它自己、启动时间对得上（防止 PID 被复用后误杀）。
    不比命令行：bash -c 只有一条命令时会直接换成那条命令。"""
    pgid, started = _ps(pid)
    return bool(ident) and pgid == str(pid) and started == ident


def _kill_group(pid: int, grace: float = 2.0) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.monotonic() + grace
    while time.monotonic() < end:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Jobs:
    def __init__(self, root: str | Path, log_dir: str | Path, sandbox=None,
                 notify: Callable[[str], None] | None = None, idle_limit: float = IDLE_LIMIT,
                 log_limit: int = LOG_LIMIT, poll: float = 1.0):
        self.root = Path(root)
        self.dir = Path(log_dir)
        self.sandbox = sandbox
        self._notify, self._held = notify, []    # 还没接上通知时（重启清理）先存着，接上后补发
        self.idle_limit, self.log_limit, self.poll = idle_limit, log_limit, poll
        self.jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cleaned = self._clean_leftovers()

    @property
    def notify(self):
        return self._notify

    @notify.setter
    def notify(self, fn) -> None:
        self._notify = fn
        held, self._held = self._held, []
        for msg in held if fn else []:
            fn(msg)

    def _send(self, msg: str) -> None:
        if self._notify:
            self._notify(msg)
        else:
            self._held.append(msg)

    # ------------------------------------------------ 登记

    @property
    def registry(self) -> Path:
        return self.dir / "registry.json"

    def log(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.log"

    def _save(self) -> None:
        with self._lock:
            done = sorted((j for j in self.jobs.values() if j.status != "running"), key=lambda j: j.ended or 0)
            for j in done[:max(0, len(done) - KEEP)]:            # 已结束的只留最近 KEEP 条
                self.jobs.pop(j.id, None)
            data = [j.public() for j in self.jobs.values()]
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.registry.with_name(f"registry.json.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.registry)

    def _clean_leftovers(self) -> list[dict]:
        """上次进程被强杀、没来得及清理的后台命令：确认身份后结束，标成 killed。"""
        try:
            data = json.loads(self.registry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        cleaned = []
        for d in data:
            job = Job(**{k: d[k] for k in Job.__dataclass_fields__ if k in d and k not in RUNTIME})
            if job.status == "running" and job.kind == "agent":     # 线程没了：标成中断，告诉主任务
                job.status, job.ended, job.reason = "killed", time.time(), "weaverd 重启，后台子 Agent 中断了"
                self._send(f"后台子 Agent {job.id}（{job.command}）{job.reason}；做到一半的记录在子账本里。"
                           "需要的话重新派一个。")
                cleaned.append(job.public())
            elif job.status == "running":
                if job.pid and _alive_group_leader(job.pid, job.ident):
                    _kill_group(job.pid)
                job.status, job.ended, job.reason, job.notify = "killed", time.time(), "上次退出时没清理，重启时结束", False
                cleaned.append(job.public())
            self.jobs[job.id] = job
        if cleaned:
            self._save()
        return cleaned

    # ------------------------------------------------ 启动、读、杀

    def start(self, command: str, wait: float = START_WAIT) -> str:
        argv = ["/bin/bash", "-c", command]
        if self.sandbox:
            argv = self.sandbox.wrap(argv)
        job = Job(id="j" + uuid.uuid4().hex[:6], command=command, started=time.time())
        self.dir.mkdir(parents=True, exist_ok=True)
        # 追加模式：日志被截短后，子进程的写入接在新的末尾，不会留下空洞。子进程拿到自己的一份句柄，这里随即关掉
        with open(self.log(job.id), "ab") as out:
            job.proc = subprocess.Popen(argv, cwd=self.root, env=clean_env(), stdout=out, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
        job.pid = job.proc.pid
        job.ident = _ps(job.pid)[1]
        job.quiet = True
        with self._lock:
            self.jobs[job.id] = job
        self._save()
        self._ensure_thread()
        end = time.monotonic() + wait
        while time.monotonic() < end and job.proc.poll() is None:
            time.sleep(0.05)
        with self._lock:                         # 回收线程要通知得先拿这把锁：放开后它看到的已经不是“启动中”
            self._check(job)
            job.quiet = False
        head = self._read(job, MAX_OUTPUT) or "(还没有输出)"
        state = ("已经结束，" + self._ended_text(job)) if job.status != "running" else \
            f"在后台运行中。用 job_output(id=\"{job.id}\") 看后续输出，job_kill 结束它；结束时会通知你"
        return f"后台命令 {job.id}：{command}\n{state}\n\n{head}"

    def _read(self, job: Job, limit: int) -> str:
        try:
            with open(self.log(job.id), "rb") as f:
                size = f.seek(0, 2)
                if size < job.read_pos:                          # 日志被截短过
                    job.read_pos = 0
                start = max(job.read_pos, size - limit)
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return ""
        skipped = start - job.read_pos
        job.read_pos = size
        text = data.decode("utf-8", "replace")
        text, hits = rd.default().redact(text)
        if hits:
            text += rd.note(hits)
        return (f"[前面还有 {skipped} 字节没显示]\n" if skipped > 0 else "") + text

    # ------------------------------------------------ 后台子 Agent

    def start_agent(self, title: str, run: Callable[[], tuple], cancel: Callable[[], None],
                    progress: Callable[[], str], limit: int | None = None) -> str:
        """在线程里跑一个子 Agent。run() 返回 (汇报, {is_error})。立刻返回任务 id。
        limit：同时最多几个后台子 Agent（数和登记在同一把锁里，几个同时派出也不会超）。"""
        job = Job(id="a" + uuid.uuid4().hex[:6], command=title, started=time.time(), kind="agent",
                  cancel_fn=cancel, progress_fn=progress, done=threading.Event())

        def go():
            try:
                text, meta = run()
                status = "failed" if meta.get("is_error") else "done"
            except Exception as e:
                text, status = f"{type(e).__name__}: {e}", "failed"
            job.report = text
            self.dir.mkdir(parents=True, exist_ok=True)
            self.log(job.id).write_text(text, encoding="utf-8")
            if job.kill_reason:
                self._finish(job, "killed", reason=job.kill_reason, notify=job.kill_notify)
            else:
                self._finish(job, status)
            job.done.set()
        job.thread = threading.Thread(target=go, name=f"weaver-agent-{job.id}", daemon=True)
        with self._lock:
            if limit is not None and sum(j.kind == "agent" and j.status == "running" for j in self.jobs.values()) >= limit:
                raise ValueError(f"后台子 Agent 最多同时 {limit} 个，先用 job_wait 等一个结束")
            self.jobs[job.id] = job
        self._save()
        job.thread.start()
        return (f"后台子 Agent {job.id}：{title}\n已派出，在后台运行。做完会通知你；需要结果时用 job_wait(id=\"{job.id}\") 等，"
                f"job_output 看进度，job_kill 结束它。")

    def cancel_agents(self, reason: str = "主任务被取消") -> int:
        """取消全部后台子 Agent，不等它们停下、不通知（主任务被取消时用，不能阻塞接口线程）。"""
        agents = [j for j in self.running() if j.kind == "agent"]
        for j in agents:
            j.kill_reason, j.kill_notify = reason, False
            if j.cancel_fn:
                j.cancel_fn()
        return len(agents)

    def wait(self, job_id: str, timeout: float = 60) -> str:
        job = self._get(job_id)
        if job.kind != "agent":
            raise ValueError("job_wait 只用于后台子 Agent；后台命令用 job_output 看输出")
        if job.status == "running" and job.done is not None:
            job.waited = True                    # 等到了就直接交回结果，不再另外通知
            finished = job.done.wait(max(1, min(float(timeout), 600)))
            if not finished:
                job.waited = False
                return f"[{job.id} 还在运行，{job.progress_fn() if job.progress_fn else ''}]"
        return self.output(job_id)

    def output(self, job_id: str) -> str:
        job = self._get(job_id)
        if job.kind == "agent":
            if job.status == "running":
                return f"[{job.id} 运行中] " + (job.progress_fn() if job.progress_fn else "")
            return f"[{job.id} {self._ended_text(job)}]\n{job.report or '(没有汇报)'}"
        self._check(job)
        text = self._read(job, MAX_OUTPUT)
        state = "运行中" if job.status == "running" else self._ended_text(job)
        return f"[{job.id} {state}]\n" + (text.rstrip() or "(没有新输出)")

    def kill(self, job_id: str, reason: str = "Agent 结束了它", notify: bool = False) -> str:
        job = self._get(job_id)
        if job.status != "running":
            return f"{job.id} 已经结束：{self._ended_text(job)}"
        job.kill_reason, job.kill_notify = reason, notify        # 回收线程先看到它退出时，照样记成“被结束”
        if job.kind == "agent":
            if job.cancel_fn:
                job.cancel_fn()
            if job.done is not None:
                job.done.wait(15)
            self._finish(job, "killed", reason=reason, notify=notify)
            return f"已结束 {job.id}（{job.command}）"
        _kill_group(job.pid)
        if job.proc is not None:
            try:
                job.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._finish(job, "killed", reason=reason, notify=notify)
        return f"已结束 {job.id}（{job.command}）"

    def list(self) -> str:
        with self._lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.started)
        for j in jobs:
            self._check(j)
        if not jobs:
            return "没有后台任务"
        return "\n".join(f"{j.id}  {'子 Agent' if j.kind == 'agent' else '命令'}  "
                         f"{'运行中' if j.status == 'running' else self._ended_text(j)}  {j.command}" for j in jobs)

    def running(self) -> list[Job]:
        with self._lock:
            return [j for j in self.jobs.values() if j.status == "running"]

    def close(self, reason: str = "Weaver 退出") -> None:
        """结束全部后台命令（不通知），停回收线程。"""
        self._stop.set()
        for j in self.running():
            self.kill(j.id, reason=reason)

    def _get(self, job_id: str) -> Job:
        with self._lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise ValueError(f"没有这个后台任务：{job_id}（用 jobs 看全部）")
        return job

    # ------------------------------------------------ 回收

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._loop, name="weaver-jobs", daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll):
            jobs = self.running()
            if not jobs:
                return                           # 没有在跑的就退出，下次启动时再开
            for j in jobs:
                self._check(j)

    def _check(self, job: Job) -> None:
        if job.status != "running" or job.proc is None or job.kind == "agent":
            return
        code = job.proc.poll()                   # 顺带回收，不留僵尸
        if code is not None:
            if job.kill_reason:
                self._finish(job, "killed", reason=job.kill_reason, notify=job.kill_notify)
            else:
                self._finish(job, "done" if code == 0 else "failed", code=code)
            return
        log = self.log(job.id)
        try:
            st = log.stat()
        except OSError:
            return
        if st.st_size > self.log_limit:
            self._shrink(log, st.st_size)
        if time.time() - max(st.st_mtime, job.started) > self.idle_limit:
            self.kill(job.id, reason=f"连续 {int(self.idle_limit // 3600) or 1} 小时没有新输出，自动结束", notify=True)

    def _shrink(self, log: Path, size: int) -> None:
        """超过上限：只留后一半（原地改写；子进程是追加写，之后的输出接在新的末尾）。"""
        with open(log, "r+b") as f:
            f.seek(size - self.log_limit // 2)
            tail = f.read()
            f.seek(0)
            f.write("[...前面的输出超过上限已丢弃...]\n".encode() + tail)
            f.truncate()

    def _finish(self, job: Job, status: str, code: int | None = None, reason: str = "", notify: bool = True) -> None:
        with self._lock:
            if job.status != "running":
                return
            job.status, job.exit_code, job.ended, job.reason = status, code, time.time(), reason
            notify = job.notify = notify and not job.quiet and not job.waited
        self._save()
        if notify and job.kind == "agent":
            report = job.report if len(job.report) <= 8000 else job.report[:8000] + "…"
            self._send(f"后台子 Agent {job.id}（{job.command}）{self._ended_text(job)}，汇报：\n{report}")
        elif notify and self.notify:
            tail = self._tail(job)
            msg = f"后台命令 {job.id}（{job.command}）{self._ended_text(job)}。"
            self.notify(msg + (f"最后几行输出：\n{tail}" if tail else ""))

    def _tail(self, job: Job) -> str:
        try:
            with open(self.log(job.id), "rb") as f:
                size = f.seek(0, 2)
                f.seek(max(0, size - 4000))
                lines = f.read().decode("utf-8", "replace").splitlines()[-TAIL_LINES:]
        except OSError:
            return ""
        return rd.default().redact("\n".join(lines))[0]

    @staticmethod
    def _ended_text(job: Job) -> str:
        if job.status == "killed":
            return f"已被结束（{job.reason}）" if job.reason else "已被结束"
        if job.kind == "agent":
            return "完成了" if job.status == "done" else "失败了"
        return f"结束了，退出码 {job.exit_code}"
