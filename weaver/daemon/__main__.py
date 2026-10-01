"""python -m weaver.daemon：启动常驻服务（前台运行；第 6 步再包成 weaver daemon start 和 launchd）。"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..providers import load_env
from ..settings.service import Settings
from .factory import RunnerFactory
from .manager import MAX_RUNNING, TaskManager
from .notify import Notifier
from .server import DaemonServer
from .tasks import TaskStore
from .uploads import Uploads


def resolve_max_running(cli: int | None) -> int:
    """命令行给了就用它；否则看 WEAVER_MAX_RUNNING——要在 load_env 之后调：设置页写进 ~/.weaver/.env 的值，
    launchd 的环境里没有。"""
    return cli or int(os.environ.get("WEAVER_MAX_RUNNING") or MAX_RUNNING)


def main() -> None:
    ap = argparse.ArgumentParser(prog="weaverd")
    ap.add_argument("--port", type=int, default=0, help="默认由系统分配，实际端口写进 daemon.json")
    ap.add_argument("--max-running", type=int, default=None, help=f"默认读 WEAVER_MAX_RUNNING，没有就是 {MAX_RUNNING}")
    ap.add_argument("--env", default=".env", help="模型配置文件")
    ap.add_argument("--quiet", action="store_true", help="不发系统通知")
    args = ap.parse_args()

    home = Path(os.environ.get("WEAVER_HOME") or "~/.weaver").expanduser()
    home.mkdir(parents=True, exist_ok=True)
    # 同一个 home 只能有一个 weaverd（命令行起的、launchd 起的、Keygent 登录项起的可能撞上）。
    # 锁跟着进程走，被强杀也会自动释放；拿不到就以 75（EX_TEMPFAIL）退出，launchd 过一会儿再试。
    lock = open(home / "daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"weaverd 已经在运行（{home}），这次不启动", file=sys.stderr)
        sys.exit(75)
    load_env(args.env if Path(args.env).exists() else home / ".env")
    handlers = [RotatingFileHandler(home / "daemon.log", maxBytes=5 * 1024 * 1024, backupCount=3)]
    if sys.stderr.isatty():
        # 在终端里前台跑才同时打到屏幕；launchd 下 stderr 是 daemon.out，只留启动失败和崩溃，不重复记一份不轮转的日志
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    try:
        factory = RunnerFactory(home)
    except ValueError as e:
        sys.exit(str(e))
    manager = TaskManager(TaskStore(home / "tasks"), factory, max_running=resolve_max_running(args.max_running),
                          uploads=Uploads(home, factory.blobs))
    manager.corpus = factory.corpus
    server = DaemonServer(manager, port=args.port, settings=Settings(home, factory.mcp_pool))
    quiet = args.quiet or os.environ.get("WEAVER_NOTIFY") == "0"
    notifier = None if quiet else Notifier(manager, lambda: server.app_clients)
    info = server.write_info(home / "daemon.json")
    mcp = factory.warm_mcp()
    woken = manager.recover()
    logging.info("weaverd 已启动：127.0.0.1:%s，模型 %s，恢复了 %d 个任务%s", server.port, factory.model.name,
                 len(woken), f"，正在连接 {mcp} 个 MCP 服务器" if mcp else "")

    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    server.start()
    done.wait()
    logging.info("weaverd 正在退出")
    if notifier:
        notifier.close()
    server.stop()
    manager.close()
    factory.close()
    info.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
