"""设置 › 权限：信任过（或拒绝过）的项目。“总是允许”按任务记，在任务管理器里（weaver/daemon/manager.py）。
见 design/keygent-gaps.md 第三节。

信任按项目记在几个文件里（键都是项目路径，MCP 的是“项目路径::服务器名”）：
  trusted-skills.json、trusted-agents.json、trusted-mcp.json（~/.weaver/）
  tasks/.trust-denied.json（App 里点了“不信任”的项目）
"""
from __future__ import annotations

import json
from pathlib import Path

from ..errors import NotFound

FILES = (("trusted-skills.json", "skill"), ("trusted-agents.json", "子 Agent 类型"), ("trusted-mcp.json", "MCP 服务器"))

BUILTIN = [
    "工作目录里读写文件：直接放行（改了能撤销）",
    "只读的命令（ls、cat、git status……）：直接放行",
    "其它命令、写工作目录外的文件、读 ~/.ssh 这类敏感路径：每次问你",
    "sudo、删家目录、强推 main 这类危险命令：一律拒绝，请你自己来",
    "项目里别人写的 skill、子 Agent 类型、MCP 服务器：先问你信不信任",
]


def _read(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _root(key: str) -> str:
    return key.split("::", 1)[0]


def projects(home: Path) -> list[dict]:
    """[{root, what: [...], state: trusted | denied}]，信任的在前，按路径排。"""
    found: dict[str, list[str]] = {}
    for name, what in FILES:
        for key in _read(home / name):
            roots = found.setdefault(_root(key), [])
            if what not in roots:
                roots.append(what)
    out = [{"root": r, "what": w, "state": "trusted"} for r, w in sorted(found.items())]
    for r in sorted(_read(home / "tasks" / ".trust-denied.json")):
        if r not in found:
            out.append({"root": r, "what": [], "state": "denied"})
    return out


def forget(home: Path, root: str) -> None:
    """不再信任（或不再拒绝）这个项目：从所有信任文件里删掉，下一轮开始时会重新问。"""
    hit = False
    for name, _ in FILES:
        p = home / name
        data = _read(p)
        keep = {k: v for k, v in data.items() if _root(k) != root}
        if len(keep) != len(data):
            _write(p, keep)
            hit = True
    p = home / "tasks" / ".trust-denied.json"
    data = _read(p)
    if root in data:
        data.pop(root)
        _write(p, data)
        hit = True
    if not hit:
        raise NotFound(f"没有关于这个项目的信任记录：{root}")
