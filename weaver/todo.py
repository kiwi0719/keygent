"""todo 清单：todo_write 工具。见 design/todo-loop.md 第一部分。

清单不另存：当前清单 = 账本里最近一次成功的 todo_write 的参数（内核 fold 里推导），
崩溃恢复、-s 接着跑都天然正确。
"""
from __future__ import annotations

from .tools import Tool, _spec

STATUSES = ("pending", "in_progress", "completed", "cancelled")
MARKS = {"pending": "[ ]", "in_progress": "[→]", "completed": "[✓]", "cancelled": "[✗]"}
MAX_ITEMS = 50


def unfinished(todos: list[dict] | None) -> list[dict]:
    return [t for t in (todos or []) if t.get("status") in ("pending", "in_progress")]


def render(todos: list[dict]) -> str:
    if not todos:
        return "（清单是空的）"
    return "\n".join(f"{MARKS.get(t.get('status'), '[?]')} {t.get('content', '')}" for t in todos)


def todo_write(todos: list) -> str:
    if not isinstance(todos, list):
        raise ValueError("todos 必须是列表：[{content, status}]")
    if len(todos) > MAX_ITEMS:
        raise ValueError(f"清单最多 {MAX_ITEMS} 项，请合并一些步骤")
    for i, t in enumerate(todos, 1):
        if not isinstance(t, dict) or not str(t.get("content", "")).strip():
            raise ValueError(f"第 {i} 项缺少 content")
        if t.get("status") not in STATUSES:
            raise ValueError(f"第 {i} 项的 status 只能是 {' / '.join(STATUSES)}")
    doing = [t for t in todos if t["status"] == "in_progress"]
    if len(doing) > 1:
        raise ValueError(f"同时有 {len(doing)} 项 in_progress。一次只做一步：只把当前正在做的标成 in_progress")
    left = len(unfinished(todos))
    tail = f"还有 {left} 项没完成。" if left else "全部完成。"
    return "清单已更新：\n" + render(todos) + "\n" + tail


def tool() -> Tool:
    return Tool(_spec(
        "todo_write",
        "写 / 更新任务清单（每次提交整张清单）。三步以上的任务开工前先列出步骤；"
        "开始做某一步时标 in_progress（同时只能一项），做完马上标 completed；计划变了就改清单，不做的标 cancelled。"
        "一两步就能完成的简单任务不用写。",
        {"todos": {"type": "array", "description": "整张清单",
                   "items": {"type": "object", "properties": {
                       "content": {"type": "string", "description": "这一步要做什么"},
                       "status": {"type": "string", "enum": list(STATUSES)}},
                       "required": ["content", "status"]}}},
        ["todos"]),
        todo_write, readonly=True, parallel=lambda args: False)
