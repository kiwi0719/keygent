"""weaver daemon install / uninstall / start / stop / status / logs。见 design/daemon.md 第五节。

launchd：登录后启动、异常退出自动拉起（KeepAlive.SuccessfulExit=false：正常退出不拉起，stop 才停得住）。
没装 launchd 时 start 就在后台起一个独立进程。
"""
from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

from .client import Api, home, load_info, pid_alive

LABEL = "ai.weaver.daemon"
PROJECT = Path(__file__).resolve().parents[2]          # weaver 包所在的目录


def plist_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def env_file(cwd: Path | None = None) -> Path:
    """模型配置：当前目录的 .env，没有就用 ~/.weaver/.env。"""
    p = (cwd or Path.cwd()) / ".env"
    return p.resolve() if p.exists() else home() / ".env"


def daemon_argv(env: Path) -> list[str]:
    return [sys.executable, "-m", "weaver.daemon", "--env", str(env)]


def plist(env: Path, python_path: Path = PROJECT, weaver_home: Path | None = None) -> dict:
    environment = {"PYTHONPATH": str(python_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if weaver_home:
        environment["WEAVER_HOME"] = str(weaver_home)
    out = str((weaver_home or home()) / "daemon.out")
    return {
        "Label": LABEL,
        "ProgramArguments": daemon_argv(env),
        "WorkingDirectory": str(python_path),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": out,
        "StandardErrorPath": out,
        "ProcessType": "Background",
    }


def _launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def installed() -> bool:
    return plist_path().exists()


def wait_ready(timeout: float = 15) -> Api | None:
    end = time.time() + timeout
    while time.time() < end:
        api = Api.connect()
        if api:
            return api
        time.sleep(0.3)
    return None


def install() -> int:
    env = env_file()
    if not env.exists():
        print(f"找不到模型配置：当前目录没有 .env，{env} 也不存在。请在项目目录里运行，或把配置放到 {env}")
        return 1
    weaver_home = Path(os.environ["WEAVER_HOME"]).expanduser() if os.environ.get("WEAVER_HOME") else None
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if load_info():
        stop()                                          # 先停掉手动起的那个，交给 launchd
    _launchctl("bootout", f"{_domain()}/{LABEL}")       # 装过就先卸载旧的
    p.write_bytes(plistlib.dumps(plist(env, weaver_home=weaver_home)))
    r = _launchctl("bootstrap", _domain(), str(p))
    if r.returncode != 0:
        print(f"launchctl 加载失败：{r.stderr.strip() or r.stdout.strip()}")
        return 1
    api = wait_ready()
    print(f"已安装：登录后自动启动，异常退出会自动拉起。\n  配置 {p}\n  模型配置 {env}\n  Python {sys.executable}")
    print("  " + ("weaverd 已在运行" if api else f"weaverd 还没起来，看日志：{home() / 'daemon.out'}"))
    return 0 if api else 1


def uninstall() -> int:
    p = plist_path()
    if not p.exists():
        print("没有安装")
        return 0
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    p.unlink()
    print("已卸载（weaverd 已停止，不再开机自启）")
    return 0


def start() -> int:
    if Api.connect():
        print("weaverd 已经在运行")
        return 0
    if installed():
        _launchctl("kickstart", f"{_domain()}/{LABEL}")
    else:
        env = env_file()
        out = home() / "daemon.out"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "ab") as f:
            subprocess.Popen(daemon_argv(env), cwd=PROJECT, stdout=f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "PYTHONPATH": str(PROJECT)})
    api = wait_ready()
    if not api:
        print(f"weaverd 没起来，看日志：{home() / 'daemon.out'}")
        return 1
    info = load_info()
    print(f"weaverd 已启动：127.0.0.1:{info['port']}，pid {info['pid']}")
    return 0


def stop() -> int:
    info = load_info()
    if not info:
        print("weaverd 没在运行")
        return 0
    os.kill(int(info["pid"]), signal.SIGTERM)          # 正常退出（退出码 0），launchd 不会拉起
    end = time.time() + 15
    while time.time() < end and pid_alive(info["pid"]):
        time.sleep(0.2)
    if pid_alive(info["pid"]):
        print("weaverd 15 秒内没退出")
        return 1
    print("weaverd 已停止" + ("（下次登录会自动启动；不想要就 weaver daemon uninstall）" if installed() else ""))
    return 0


def status() -> int:
    api = Api.connect()
    print(f"launchd：{'已安装（登录后自动启动）' if installed() else '没安装'}")
    if not api:
        print("weaverd：没在运行")
        return 3
    info, s = load_info(), api.call("GET", "/v1/status")
    print(f"weaverd：在运行  127.0.0.1:{info['port']}  pid {info['pid']}  版本 {s.get('version')}")
    print(f"任务：在跑 {s['running']}，排队 {s['queued']}，等你 {s['waiting']}"
          + (f"，出错：{s['error']['task_title']}（{s['error']['note']}）" if s.get("error") else ""))
    return 0


def logs(n: int = 50) -> int:
    p = home() / "daemon.log"
    if not p.exists():
        print("还没有日志")
        return 0
    print("\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]))
    return 0


def main(argv: list[str]) -> int:
    cmds = {"install": install, "uninstall": uninstall, "start": start, "stop": stop, "status": status, "logs": logs}
    if not argv or argv[0] not in cmds:
        print("用法：weaver daemon " + " | ".join(cmds))
        return 2
    return cmds[argv[0]]()
