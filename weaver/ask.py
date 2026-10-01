"""ask_user：任务中途问用户一个问题，等回答再接着做。见 design/ask-user.md。

复用审批的等待机制：主 Agent 的权限规则对它一律“等人”（kind=question），有结论后内核把答案
当成这次调用的结果交回模型（kernel.decide 第 6 条），工具函数本身不会被执行。
只有不等人时（--yes、或参数不合法）才真的执行到下面的 fn：参数不合法报错，合法就当作“没人回答”。
"""
from __future__ import annotations

from .kernel import ANSWERED, SKIPPED
from .tools import Tool, _spec

__all__ = ["ANSWERED", "SKIPPED", "validate", "tool", "parse_reply", "prompt_lines"]
MIN_OPTIONS, MAX_OPTIONS = 2, 4


def validate(args: dict) -> str | None:
    """参数有问题返回原因，没问题返回 None。"""
    if not str(args.get("question") or "").strip():
        return "question 不能为空"
    options = args.get("options")
    if options in (None, []):
        return None
    if not isinstance(options, list):
        return "options 必须是字符串列表"
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return f"options 要么不给，要么 {MIN_OPTIONS}~{MAX_OPTIONS} 个（现在 {len(options)} 个）"
    if any(not isinstance(o, str) or not o.strip() for o in options):
        return "options 里每一项都必须是非空字符串"
    return None


def prompt_lines(question: str, options: list[str]) -> list[str]:
    """命令行里怎么显示一个问题。"""
    lines = [f"问你：{question}"] + [f"  {i}. {o}" for i, o in enumerate(options, 1)]
    hint = "数字选选项，直接打字自己写，" if options else "直接打字回答，"
    return lines + [f"回答（{hint}空行 = 你自己定）："]


def parse_reply(text: str, options: list[str]) -> str | None:
    """命令行里的回答：选项编号 → 选项原文；其它文字照原样；空 → None（让它自己定）。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit() and 1 <= int(text) <= len(options):
        return options[int(text) - 1]
    return text


def tool() -> Tool:
    def run(question: str = "", options: list | None = None) -> str:
        problem = validate({"question": question, "options": options})
        if problem:
            raise ValueError(problem)
        return SKIPPED

    return Tool(_spec(
        "ask_user",
        "问用户一个问题，等他回答后再接着做。用在：需求不清楚、要用户在几个方案里拍板、方案要用户批准才能动手。"
        "一次只问一个问题；能列出选项就给 2~4 个 options（用户也总能自己写）。"
        "普通的进度汇报、做完了的结论不要用它，直接写进回复。",
        {"question": {"type": "string", "description": "问题，一句话说清楚要用户定什么"},
         "options": {"type": "array", "items": {"type": "string"},
                     "description": "可选的答案，2~4 个；没有合适的选项就不给"}},
        ["question"]),
        run, readonly=True)
