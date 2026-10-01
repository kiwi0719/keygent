"""环境信息：工作目录、平台、可用命令、git 分支、沙箱、日期。见 design/environment.md。

作为背景输入放在会话开头；换了分支、过了一天等变化时，在下一条用户输入前追加更新。
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from .context import ContextProvider

COMMANDS = ("python3", "python", "pip3", "pip", "uv", "node", "npm", "pnpm", "yarn", "bun", "go", "cargo",
            "rustc", "java", "make", "docker", "git")
VERSIONED = ("python3", "python", "node")


def _platform() -> str:
    system, machine = platform.system(), platform.machine()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}（{machine}）"
    if system == "Linux":
        try:
            text = Path("/etc/os-release").read_text()
            m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.M)
            if m:
                return f"Linux {m.group(1)}（{machine}）"
        except OSError:
            pass
        return f"Linux（{machine}）"
    return f"{system}（{machine}）"


def _version(path: str) -> str:
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"v?\d+(\.\d+)+", (r.stdout or r.stderr).strip())
    return m.group(0) if m else ""


def _git_branch(root: Path) -> str | None:
    """None 表示不是 git 仓库。直接读 .git/HEAD，不起子进程。"""
    for d in (root, *root.parents):
        head = d / ".git" / "HEAD"
        if head.is_file():
            text = head.read_text().strip()
            return text.removeprefix("ref: refs/heads/") if text.startswith("ref:") else f"（游离 HEAD {text[:8]}）"
        if (d / ".git").exists():             # worktree 等情况：.git 是文件
            return "（未知分支）"
    return None


class Environment(ContextProvider):
    kind = "environment"
    first_head = ""
    update_head = "环境有变化，以下是最新的环境信息。"

    def __init__(self, root: str | Path = ".", sandbox=None, which=shutil.which, today=None):
        self.root = Path(root).resolve()
        self.sandbox = sandbox
        self.which = which
        self.today = today or (lambda: time.strftime("%Y-%m-%d"))
        self._commands: str | None = None     # 可用命令和版本在一个进程里不会变，只查一次

    def commands(self) -> str:
        if self._commands is None:
            found = []
            for c in COMMANDS:
                path = self.which(c)
                if not path:
                    continue
                v = _version(path) if c in VERSIONED else ""
                found.append(f"{c}（{v}）" if v else c)
            text = "、".join(found) or "（没找到常用的开发命令）"
            if self.which("python3") and not self.which("python"):
                text += "。没有 python，请用 python3"
            self._commands = text
        return self._commands

    def render(self) -> str:
        branch = _git_branch(self.root)
        lines = [
            "## 环境",
            f"- 工作目录：{self.root}（bash 每条命令都从这里开始，不用 cd 到别处；相对路径都相对它）",
            f"- 平台：{_platform()}",
            "- shell：/bin/bash，每条命令一个新 shell，不保留 cd 和环境变量",
            f"- 可用命令：{self.commands()}",
            f"- git：{'是 git 仓库，当前分支 ' + branch if branch else '不是 git 仓库'}",
        ]
        if self.sandbox is not None:
            lines.append(f"- {self.sandbox.describe()}")
        lines.append(f"- 日期：{self.today()}")
        return "\n".join(lines)
