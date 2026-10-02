"""权限：允不允许做。见 design/write-tools.md 第四节。

三种结果：放行、问人、拒绝。工作目录内的读写放行（写有撤销兜底），bash 除只读命令外都问人，
极少数明显危险的命令直接拒绝（--yes 也拦）。正则挡不住有心绕过的人，真正兜底的是沙箱。
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from .ask import validate
from .kernel import Deny, State, WaitSpec
from .policy import Policy

READ_TOOLS = ("read_file", "grep", "find_files")
WRITE_TOOLS = ("write_file", "edit_file")
# task 本身放行：子 Agent 的每一步都走它自己的权限规则；后台任务的几个工具只能操作本任务自己启动的
SAFE_TOOLS = ("remember", "forget", "task", "todo_write", "skill", "recall", "ask_user",
              "jobs", "job_output", "job_kill", "job_wait")
PROTECTED_DIRS = (".git", ".weaver")
SENSITIVE = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gcloud", "~/.kube", "~/.docker/config.json",
             "~/.netrc", "~/.npmrc", "~/.pypirc")
ENV_SAMPLES = (".env.example", ".env.sample", ".env.template", ".env.dist")

HARD_DENY = [
    (r"(^|[\s;&|(])sudo\b", "sudo"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME)/?(\s|$|\*)", "删除根目录或家目录"),
    (r"\bmkfs(\.\w+)?\b", "格式化磁盘"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "直接写磁盘设备"),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b", "下载后直接执行"),
    (r"\bchmod\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)?[0-7]*777\s+/(\s|$)", "把根目录改成 777"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork 炸弹"),
    (r"\bgit\s+push\b[^;&|]*(--force\b|-f\b)[^;&|]*\b(main|master)\b", "强推 main / master"),
    (r"\b(shutdown|reboot|halt)\b", "关机或重启"),
]

READONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "echo", "printf", "which", "type", "file", "stat", "du", "df",
    "tree", "rg", "grep", "egrep", "fgrep", "sort", "uniq", "diff", "cmp", "basename", "dirname", "realpath",
    "readlink", "date", "whoami", "uname", "env", "printenv", "true", "false", "cut", "tr", "column", "nl",
    "less", "more", "jq", "md5", "md5sum", "shasum", "sha256sum", "cksum", "od", "hexdump", "strings",
}
GIT_READONLY = {"status", "diff", "log", "show", "branch", "blame", "rev-parse", "ls-files", "remote", "describe",
                "shortlog", "tag", "config"}
VERSION_FLAGS = {"--version", "-V", "version"}


def _segments(command: str) -> list[str] | None:
    """按 | && || ; 换行拆开。有命令替换、重定向写文件、后台运行时返回 None（当作不只读）。"""
    if re.search(r"`|\$\(|<\(|>\(", command):
        return None
    no_quotes = re.sub(r"'[^']*'|\"[^\"]*\"", "''", command)
    # 2>&1、丢进 /dev/null 不算写文件
    cleaned = re.sub(r"\d?>&\d|\d?>{1,2}\s*/dev/null", " ", no_quotes)
    if re.search(r">", cleaned):                                   # 还有别的重定向写文件
        return None
    if re.search(r"(^|[^&])&($|[^&])", cleaned):                  # 后台运行
        return None
    return [s.strip() for s in re.split(r"\|\||&&|[|;\n]", command) if s.strip()]


def is_readonly_command(command: str) -> bool:
    segs = _segments(command)
    if not segs:
        return False
    for seg in segs:
        try:
            words = shlex.split(seg)
        except ValueError:
            return False
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):   # 前面的环境变量赋值
            words = words[1:]
        if not words:
            return False
        cmd, args = os.path.basename(words[0]), words[1:]
        if cmd in ("tee", "xargs", "sh", "bash", "zsh", "eval", "exec", "source", "."):
            return False
        if cmd == "find":
            if any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")
                   for a in args):
                return False
            continue
        if cmd == "sed":
            if any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") for a in args) or "-n" not in args:
                return False
            continue
        if cmd == "git":
            sub = next((a for a in args if not a.startswith("-")), "")
            if sub not in GIT_READONLY or (sub in ("branch", "tag", "remote", "config") and
                                           any(a.startswith("-") and a not in ("-a", "-r", "-v", "-vv", "--list",
                                                                               "-l", "--get", "--show-current")
                                               for a in args[1:])):
                return False
            if sub in ("branch", "tag") and len([a for a in args[1:] if not a.startswith("-")]) > 0:
                return False
            continue
        if args and all(a in VERSION_FLAGS for a in args):          # python --version 之类
            continue
        if cmd not in READONLY_COMMANDS:
            return False
    return True


def hard_deny(command: str) -> str | None:
    for pat, what in HARD_DENY:
        if re.search(pat, command):
            return what
    return None


def command_prefix(command: str) -> str:
    """“总是允许”记的前缀：前两个词，如 npm test、git commit。"""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    words = [w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
    return " ".join(words[:2])


def mcp_key(name: str, args: dict | None) -> str:
    """MCP 工具“总是允许”记的键：工具名（mcp_call 记它要调的那个工具）。"""
    if name == "mcp_call":
        from .mcp.tools import tool_name
        return tool_name((args or {}).get("server", ""), (args or {}).get("tool", ""))
    return name


def always_allowed(state: State) -> set[str]:
    """账本里记下的“总是允许”的前缀（写在 WaitResolved 里，崩溃恢复、换进程都不丢）。"""
    return {w.value["always"] for w in state.waits.values()
            if w.resolved and isinstance(w.value, dict) and w.value.get("allow") and w.value.get("always")}


class PermissionPolicy(Policy):
    """默认规则之上加权限。yes=True 时不问人（硬拦截照样拒绝）。
    ask_user=True 只给主 Agent：它的 ask_user 调用一律等人回答；子 Agent 的规则不为它等人（分叉的在执行时被拒绝）。"""

    def __init__(self, root: str | Path = ".", yes: bool = False, mcp=None, ask_user: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.root = Path(root).resolve()
        self.yes = yes
        self.ask_user = ask_user
        self.mcp = mcp                          # weaver.mcp.tools.McpTools：MCP 工具按只读标记和 trust 配置决定
        self.revoked = lambda: set()            # 用户在设置页撤销过的“总是允许”（常驻服务从任务的 meta.json 读）

    def _always(self, state: State) -> set[str]:
        return always_allowed(state) - self.revoked()

    def _inside(self, path: str) -> tuple[bool, Path]:
        p = Path(path).expanduser()
        p = (p if p.is_absolute() else self.root / p).resolve()     # 真实路径：防符号链接绕出去
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            return False, p
        return not (rel.parts and rel.parts[0] in PROTECTED_DIRS), p

    def _sensitive(self, path: str) -> bool:
        p = (Path(path).expanduser() if Path(path).expanduser().is_absolute()
             else self.root / path).resolve()
        if p.name.startswith(".env") and p.name not in ENV_SAMPLES:
            return True
        return any(p == Path(s).expanduser().resolve() or Path(s).expanduser().resolve() in p.parents
                   for s in SENSITIVE)

    def decide(self, call: dict, state: State) -> tuple[str, str]:
        """返回 (allow | ask | deny, 原因)。"""
        name, args = call["name"], call.get("args") or {}
        if name in READ_TOOLS:
            target = args.get("path", ".")
            return ("ask", f"要读敏感路径 {target}") if self._sensitive(target) else ("allow", "")
        if name in WRITE_TOOLS:
            inside, real = self._inside(args.get("path", ""))
            return ("allow", "") if inside else ("ask", f"要写工作目录外或受保护的路径：{real}")
        if name in SAFE_TOOLS:
            return "allow", ""
        if self.mcp is not None and (name.startswith("mcp__") or name.startswith("mcp_")):
            if self.mcp.rule(name, args) == "allow" or mcp_key(name, args) in self._always(state):
                return "allow", ""
            return "ask", "要调用 MCP 工具（服务器没标只读，可能有副作用）"
        if name == "bash":
            command = args.get("command", "")
            danger = hard_deny(command)
            if danger:
                return "deny", f"命令里有{danger}，这类操作不允许 Agent 执行，请用户自己来"
            if is_readonly_command(command):
                return "allow", ""
            if command_prefix(command) in self._always(state):
                return "allow", ""
            return "ask", "要执行命令"
        return "ask", f"未知工具 {name}"

    def wait_for(self, call: dict, state: State):
        if call["name"] == "ask_user" and self.ask_user and not self.yes:
            # 在所有自动放行规则之前：任何设置都不跳过问人。参数不合法就不等，交给执行报错
            return None if validate(call.get("args") or {}) else WaitSpec("question", "")
        verdict, reason = self.decide(call, state)
        if verdict == "deny":
            return Deny(reason)
        if verdict == "ask" and not self.yes:
            return WaitSpec("approval", reason)
        return None
