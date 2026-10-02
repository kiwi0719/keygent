"""设置 › 记忆：看、改、删用户级和各个项目的记忆。见 design/keygent-gaps.md 第四节。

读写都走 weaver/memory.py 的 Memory（同一把文件锁、同样的检查：像密钥、像注入的拒绝；旧版本归档、重建索引）。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..errors import BadRequest, NotFound
from ..memory import TYPES, Memory, _frontmatter

TEMPLATE = "---\nname: \ndescription: \ntype: feedback\n---\n\n"


def _memory(home: Path, scope: str, root: str) -> Memory:
    if scope == "user":
        return Memory(home, home=home, project=False)
    if scope != "project":
        raise BadRequest("scope 只能是 user 或 project")
    if not root or not Path(root).is_dir():
        raise BadRequest(f"项目目录不存在：{root}")
    return Memory(root, home=home, project=True)


def _items(d: Path) -> list[dict]:
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        meta = _frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        out.append({"name": p.stem, "type": meta.get("type", ""), "description": meta.get("description", ""),
                    "path": str(p)})
    order = {t: i for i, t in enumerate(TYPES)}
    return sorted(out, key=lambda m: (order.get(m["type"], 9), m["name"]))


def list_all(home: Path, roots: list[str]) -> dict:
    scopes = [{"scope": "user", "root": "", "label": "所有任务", "items": _items(home / "memory")}]
    seen = set()
    for r in roots:
        if not r or r in seen:
            continue
        seen.add(r)
        d = Path(r) / ".weaver" / "memory"
        items = _items(d)
        if items:
            scopes.append({"scope": "project", "root": r, "label": Path(r).name, "items": items})
    return {"scopes": scopes, "template": TEMPLATE}


def read(home: Path, scope: str, root: str, name: str) -> dict:
    mem = _memory(home, scope, root)
    p = mem.dirs[scope] / f"{name}.md"
    if not p.is_file():
        raise NotFound(f"没有这条记忆：{name}")
    return {"name": name, "text": p.read_text(encoding="utf-8", errors="replace")}


def _split(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        raise BadRequest("开头要有 frontmatter：--- name / description / type ---")
    return _frontmatter(text), text[m.end():]


def save(home: Path, scope: str, root: str, name: str | None, text: str) -> str:
    """新建（name=None）或保存；frontmatter 里的 name 改了就是改名（旧的归档）。返回保存后的名字。"""
    mem = _memory(home, scope, root)
    meta, body = _split(text)
    new = meta.get("name", "").strip()
    if not new:
        raise BadRequest("frontmatter 里要有 name")
    if not body.strip():
        raise BadRequest("记忆正文不能为空")
    if name != new and (mem.dirs[scope] / f"{new}.md").exists():
        raise BadRequest(f"已经有一条叫 {new} 的记忆了")
    if name is not None and not (mem.dirs[scope] / f"{name}.md").exists():
        raise NotFound(f"没有这条记忆：{name}")
    try:
        mem.remember(new, meta.get("type", "").strip(), meta.get("description", ""), body, scope=scope)
        if name is not None and name != new:
            mem.forget(name, scope=scope)
    except ValueError as e:
        raise BadRequest(str(e)) from None
    return new


def remove(home: Path, scope: str, root: str, name: str) -> None:
    mem = _memory(home, scope, root)
    try:
        mem.forget(name, scope=scope)
    except FileNotFoundError:
        raise NotFound(f"没有这条记忆：{name}") from None
