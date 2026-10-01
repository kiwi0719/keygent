"""沙箱：限制 bash 最多能造成多大破坏。见 design/write-tools.md 第五节。

macOS 用系统自带的 sandbox-exec。写法和 Codex 相反、更简单：默认全放开，只禁写，
再放开工作目录和临时目录；工作目录里的 .git、.weaver 仍只读。
Linux 用系统装的 bubblewrap（LGPL，不随项目打包）：整个系统只读挂载，再把可写目录挂成可写。
启动时先试跑一次，内核禁了用户命名空间（常见于容器里）就当作没有沙箱。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# bwrap 试跑结果（按进程缓存）：路径 → (能不能用, 要不要挂新的 /proc, 失败原因)
_bwrap_probe: dict[str, tuple[bool, bool, str]] = {}
STARTUP_FAILED = "\n[沙箱启动失败，命令没有执行。这是环境问题，不是命令本身的问题；请把情况告诉用户。]"
DENIED_HINT = ("\n[这是沙箱限制：命令只能写工作目录和临时目录，工作目录里的 .git、.weaver 也只读。"
               "不要反复重试；确实需要时请让用户自己执行。]")


def _quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Sandbox:
    def __init__(self, root: str | Path, net: bool = True, git_writable: bool = False,
                 extra_writable: list[str] | None = None, enabled: bool = True):
        self.root = Path(root).resolve()
        self.net, self.git_writable = net, git_writable
        self.extra_writable = [str(Path(p).expanduser().resolve()) for p in (extra_writable or []) if p]
        self.enabled = enabled

    @classmethod
    def from_env(cls, root: str | Path, env: dict | None = None) -> "Sandbox":
        env = os.environ if env is None else env
        off = lambda k: env.get(k, "").lower() in ("0", "false", "no")
        on = lambda k: env.get(k, "").lower() in ("1", "true", "yes")
        return cls(root, net=not off("WEAVER_SANDBOX_NET"), git_writable=on("WEAVER_SANDBOX_GIT"),
                   extra_writable=env.get("WEAVER_SANDBOX_WRITABLE", "").split(":"),
                   enabled=not off("WEAVER_SANDBOX"))

    @property
    def kind(self) -> str | None:
        """可用的沙箱种类；None 表示没有沙箱。"""
        if not self.enabled:
            return None
        if platform.system() == "Darwin" and Path(SANDBOX_EXEC).exists():
            return "seatbelt"
        if platform.system() == "Linux" and self.bwrap and self._probe()[0]:
            return "bwrap"
        return None

    @property
    def bwrap(self) -> str | None:
        return os.environ.get("WEAVER_BWRAP") or shutil.which("bwrap")

    def _probe(self) -> tuple[bool, bool, str]:
        """用和真正执行**完全相同**的参数试跑（只有 --proc 可能去掉）。
        有些环境（如 Docker 容器）不允许挂新的 /proc，就退一步：不挂新的，沿用只读的宿主 /proc（Codex 的 mount_proc 开关）。"""
        path = self.bwrap
        if not path:
            return False, False, "没找到 bwrap，请安装 bubblewrap（apt install bubblewrap / dnf install bubblewrap）"
        if path not in _bwrap_probe:
            errors = []
            result = None
            for proc in (True, False):
                try:
                    r = subprocess.run(self.bwrap_args(["/bin/true"], proc=proc), capture_output=True, text=True,
                                       timeout=10)
                except (OSError, subprocess.TimeoutExpired) as e:
                    errors.append(str(e))
                    continue
                if r.returncode == 0:
                    result = (True, proc, "")
                    break
                errors.append((r.stderr or "").strip()[:200])
            _bwrap_probe[path] = result or (False, False,
                f"bwrap 试跑失败（{errors[-1] if errors else '未知原因'}）。常见原因是在容器里、或内核禁了非特权用户命名空间；"
                "如果容器本身就是隔离环境，可以用 --no-sandbox")
        return _bwrap_probe[path]

    def bwrap_problem(self) -> str | None:
        ok, _, reason = self._probe()
        return None if ok else reason

    def bwrap_args(self, argv: list[str], proc: bool | None = None) -> list[str]:
        """bubblewrap 参数。挂载有先后：后挂载的覆盖先挂载的，所以 .git、.weaver 放在工作目录之后。"""
        root = str(self.root)
        args = [self.bwrap or "bwrap", "--new-session", "--die-with-parent",
                "--ro-bind", "/", "/",
                "--dev", "/dev", "--bind-try", "/dev/shm", "/dev/shm"]
        if proc is None:
            proc = _bwrap_probe.get(self.bwrap or "", (True, True, ""))[1]
        if proc:
            args += ["--proc", "/proc"]
        args += ["--bind", "/tmp", "/tmp",
                "--bind", root, root]
        for p in self.extra_writable:
            args += ["--bind-try", p, p]
        readonly = [".weaver"] + ([] if self.git_writable else [".git"])
        for d in readonly:
            args += ["--ro-bind-try", f"{root}/{d}", f"{root}/{d}"]
        args += ["--unshare-user", "--unshare-pid", "--unshare-ipc"]
        if not self.net:
            args.append("--unshare-net")
        args += ["--cap-drop", "ALL", "--chdir", root, "--", *argv]
        return args

    def profile(self) -> str:
        root = str(self.root)
        writable = [root, "/private/tmp", "/private/var/folders", *self.extra_writable]
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            "(allow file-write*",
            *[f"  (subpath {_quote(p)})" for p in writable],
            '  (literal "/dev/null") (literal "/dev/zero") (regex #"^/dev/tty") (regex #"^/dev/fd/"))',
        ]
        readonly = [".weaver"] + ([] if self.git_writable else [".git"])
        lines.append("(deny file-write* " + " ".join(f"(subpath {_quote(root + '/' + d)})" for d in readonly) + ")")
        if not self.net:
            lines.append("(deny network-outbound (remote ip))")
        return "\n".join(lines) + "\n"

    def wrap(self, argv: list[str]) -> list[str]:
        """把命令包进沙箱。没有沙箱就原样返回。"""
        kind = self.kind
        if kind == "seatbelt":
            return [SANDBOX_EXEC, "-p", self.profile(), *argv]
        if kind == "bwrap":
            return self.bwrap_args(argv)
        return argv

    def describe(self) -> str:
        if not self.kind:
            if self.enabled and platform.system() == "Linux":
                return f"无沙箱：{self.bwrap_problem()}"
            return "无沙箱" if self.enabled else "无沙箱（已用 WEAVER_SANDBOX=0 关闭）"
        net = "可联网" if self.net else "断网"
        git = "，.git 可写" if self.git_writable else ""
        return f"沙箱 {self.kind}：只能写工作目录和临时目录，{net}{git}"
