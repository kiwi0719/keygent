"""工具箱：内核只认 specs 和 execute 两个接口。见 design/tools.md。

只读：read_file、grep、find_files。写：write_file、edit_file、bash（add_write_tools 加上，
需要会话 id、BlobStore 做撤销，bash 在沙箱里跑）。权限在 weaver/permissions.py。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .edit import FileEditor, FileTracker, UndoLog
from .files import MAX_BYTES, read_file
from .search import AUTO, MAX_LINE, Searcher, find_rg
from .shell import DEFAULT_TIMEOUT, MAX_OUTPUT, MAX_TIMEOUT, Running, run_bash


@dataclass
class Tool:
    spec: dict
    fn: Callable[..., Any]        # 返回文字；也可以返回 (文字, 附加信息)，如子 Agent 的 {"usage": …}
    readonly: bool = True
    parallel: Callable[[dict], bool] | None = None   # 这次调用能不能和别的并行；None 表示跟 readonly 走
    wants_call_id: bool = False   # 要不要把调用 id 传给 fn（子 Agent 用它记 parent_id）


def _bash_parallel(args: dict) -> bool:
    from ..permissions import is_readonly_command          # 延迟导入，避免循环依赖
    return is_readonly_command(args.get("command", ""))


def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


class ToolBox:
    def __init__(self, root: str | Path = ".", rg=AUTO, tools: dict[str, Tool] | None = None):
        self.root = Path(root).resolve()
        self.search = Searcher(self.root, rg)
        self.tracker = FileTracker()
        self.running = Running()                 # 正在跑的前台命令：取消时整组结束
        self.on_interrupt: list = []             # 取消时还要做的事（比如取消正在跑的子 Agent）
        self.jobs = None                         # 后台命令登记表（weaver/jobs.py），没有就不支持 background
        self.tools = tools if tools is not None else self._default_tools()

    def interrupt(self) -> int:
        """取消：结束正在跑的前台命令。返回结束了几个。"""
        n = self.running.interrupt()
        for fn in self.on_interrupt:
            n += fn() or 0
        return n

    def _read(self, path, offset=1, limit=2000) -> str:
        p = self.search.resolve(path)
        out = read_file(str(p), offset, limit)
        self.tracker.mark(p)                     # 读过才能改
        return out

    def add_write_tools(self, session: str, blobs, sandbox=None, jobs=None) -> "ToolBox":
        """加上会改东西的三件工具。session、blobs 用于撤销；sandbox 用于 bash；jobs 给了就支持后台命令。"""
        self.jobs = jobs
        self.undo = UndoLog(self.root, session, blobs)
        editor = FileEditor(self.tracker, self.undo)
        self.sandbox = sandbox
        resolve = self.search.resolve
        self.tools.update({
            "write_file": Tool(_spec(
                "write_file",
                "写整个文件：新建，或整体覆盖已有文件。覆盖已有文件前必须先 read_file 读过。"
                "只改一部分时用 edit_file，别整个重写。",
                {"path": {"type": "string", "description": "文件路径，相对路径相对于工作目录"},
                 "content": {"type": "string", "description": "文件的完整内容"}},
                ["path", "content"]),
                lambda path, content: editor.write_file(resolve(path), content), readonly=False),
            "edit_file": Tool(_spec(
                "edit_file",
                "字符串替换：把文件里的 old_string 换成 new_string。old_string 必须和原文完全一致"
                "（包括缩进和换行），且只出现一次；出现多次时多带几行上下文，或设 replace_all=true。"
                "改之前必须先 read_file 读过这个文件。",
                {"path": {"type": "string", "description": "文件路径"},
                 "old_string": {"type": "string", "description": "要被替换的原文"},
                 "new_string": {"type": "string", "description": "替换成的新内容，必须和 old_string 不同"},
                 "replace_all": {"type": "boolean", "description": "替换所有出现的地方，默认 false"}},
                ["path", "old_string", "new_string"]),
                lambda path, old_string, new_string, replace_all=False:
                    editor.edit_file(resolve(path), old_string, new_string, replace_all), readonly=False),
            "bash": Tool(_spec(
                "bash",
                "在工作目录执行一条 shell 命令（每次都是新的 shell，不保留 cd 和环境变量）。"
                f"适合跑测试、构建、git 查询等。输出只保留最后 {MAX_OUTPUT // 1000}KB。"
                "查找文件和内容请用 grep / find_files，读文件用 read_file，改文件用 edit_file，别用 cat、sed、echo 重定向。"
                + ("命令在沙箱里执行：只能写工作目录和临时目录，工作目录里的 .git 只读。" if sandbox and sandbox.kind else ""),
                {"command": {"type": "string", "description": "要执行的命令"},
                 "timeout": {"type": "integer",
                             "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}，最多 {MAX_TIMEOUT}"},
                 **({"background": {"type": "boolean",
                                    "description": "后台运行（开发服务器、很久的测试、tail -f 这类不会很快结束的命令）："
                                                   "立刻返回任务 id 和前 3 秒的输出，结束时会通知你"}} if jobs else {})},
                ["command"]),
                self._bash, readonly=False, parallel=_bash_parallel),
        })
        if jobs:
            self.tools.update(_job_tools(jobs))
        return self

    def _bash(self, command, timeout=DEFAULT_TIMEOUT, background=False):
        if background:
            if self.jobs is None:
                raise ValueError("这里不支持后台运行")
            return self.jobs.start(command)
        return run_bash(command, self.root, self.sandbox, timeout, self.root / ".weaver" / "outputs", self.running)

    def _default_tools(self) -> dict[str, Tool]:
        root = self.root
        return {
            "read_file": Tool(_spec(
                "read_file",
                "读取文本文件，输出带行号。大文件请用 offset/limit 只读需要的部分；"
                f"单次最多返回 {MAX_BYTES // 1000}KB。先用 grep / find_files 定位，再读相关的几行。",
                {"path": {"type": "string", "description": "文件路径，相对路径相对于工作目录"},
                 "offset": {"type": "integer", "description": "从第几行开始读，从 1 开始，默认 1"},
                 "limit": {"type": "integer", "description": "最多读几行，默认 2000"}},
                ["path"]),
                self._read),
            "grep": Tool(_spec(
                "grep",
                "按正则搜索文件内容，返回“路径:行号: 内容”。遵守 .gitignore，跳过 .git 等目录。"
                f"最多返回 limit 条（默认 100）、{MAX_BYTES // 1000}KB；单行超过 {MAX_LINE} 字会截断。",
                {"pattern": {"type": "string", "description": "正则表达式；literal=true 时按普通字符串搜"},
                 "path": {"type": "string", "description": "在哪个目录或文件里搜，默认工作目录"},
                 "glob": {"type": "string", "description": "只搜匹配的文件，如 *.py、src/**/*.ts、*.{ts,tsx}"},
                 "ignore_case": {"type": "boolean", "description": "忽略大小写，默认 false"},
                 "literal": {"type": "boolean", "description": "把 pattern 当普通字符串，默认 false"},
                 "context": {"type": "integer", "description": "每个匹配前后各显示几行，默认 0"},
                 "limit": {"type": "integer", "description": "最多返回多少条匹配，默认 100"}},
                ["pattern"]),
                self.search.grep),
            "find_files": Tool(_spec(
                "find_files",
                "按 glob 查找文件，返回相对路径，最近修改的排前面。遵守 .gitignore。",
                {"pattern": {"type": "string", "description": "glob，如 *.py、**/test_*.py、src/**/*.{ts,tsx}"},
                 "path": {"type": "string", "description": "在哪个目录下找，默认工作目录"},
                 "limit": {"type": "integer", "description": "最多返回多少个，默认 200"}},
                ["pattern"]),
                self.search.find_files),
        }

    @property
    def specs(self) -> list[dict]:
        return [t.spec for t in self.tools.values()]

    def is_readonly(self, name: str) -> bool:
        return name in self.tools and self.tools[name].readonly

    def concurrency_safe(self, name: str, args: dict) -> bool:
        """这次调用能不能和相邻的调用并行。工具自己声明，默认只读的能。"""
        tool = self.tools.get(name)
        if tool is None:
            return False
        if tool.parallel is not None:
            try:
                return bool(tool.parallel(args or {}))
            except Exception:
                return False
        return tool.readonly

    def execute(self, name: str, args: dict, scope: Any = None, call_id: str | None = None) -> tuple:
        """返回 (文字, 是否出错) 或 (文字, 是否出错, 附加信息)。"""
        if name not in self.tools:
            return f"没有名为 {name} 的工具。可用：{', '.join(self.tools)}", True
        tool = self.tools[name]
        missing = [r for r in tool.spec["parameters"].get("required", []) if r not in args]
        if missing:
            return f"缺少必填参数：{', '.join(missing)}", True
        try:
            out = tool.fn(**args, _call_id=call_id) if tool.wants_call_id else tool.fn(**args)
            if isinstance(out, tuple):
                text, meta = out
                return text, bool(meta.get("is_error")), meta
            return out, False
        except TypeError as e:
            return f"参数不对：{e}", True
        except Exception as e:
            return f"{type(e).__name__}: {e}", True


def _job_tools(jobs) -> dict[str, Tool]:
    return {
        "job_output": Tool(_spec("job_output", "读后台命令上次之后的新输出（最多 30KB）、或后台子 Agent 的进度，并告诉你它是否还在运行。",
                                 {"id": {"type": "string", "description": "后台命令的 id"}}, ["id"]),
                           jobs.output),
        "job_kill": Tool(_spec("job_kill", "结束一个后台命令（连同它的子进程）。",
                               {"id": {"type": "string", "description": "后台命令的 id"}}, ["id"]),
                         jobs.kill, readonly=False, parallel=lambda _: False),
        "jobs": Tool(_spec("jobs", "列出这个任务的全部后台命令、后台子 Agent 和状态。", {}, []), lambda: jobs.list()),
        "job_wait": Tool(_spec("job_wait", "等一个后台子 Agent 做完并拿到汇报，最多等 timeout 秒（默认 60）。",
                               {"id": {"type": "string", "description": "后台子 Agent 的 id"},
                                "timeout": {"type": "integer", "description": "最多等多少秒，默认 60，最多 600"}},
                               ["id"]),
                         lambda id, timeout=60: jobs.wait(id, timeout)),
    }


__all__ = ["ToolBox", "Tool", "read_file", "Searcher", "find_rg", "FileEditor", "FileTracker", "UndoLog",
           "run_bash"]
