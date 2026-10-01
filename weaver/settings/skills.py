"""用户级 skills 的管理：列出、读、新建、保存（改了 name 连目录一起改名）、归档。

Weaver 的放在 ~/.weaver/skills/<名字>/SKILL.md，可以改；Claude Code 的（~/.claude/skills/）和
内置的（weaver/builtin_skills/）只读。同名时按 weaver/skills.py 的发现顺序：Weaver > Claude Code > 内置，
被盖住的标 shadowed。新建时不许和 Claude Code 的重名；和内置的同名可以（这是定制内置 skill 的办法）。
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from ..errors import BadRequest, Conflict, NotFound
from ..skills import BUILTIN_DIR, parse_frontmatter

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


READONLY = {"claude": "来自 Claude Code 的 skill 只能看，不能改；要改就新建一个同样内容的",
            "builtin": "内置的 skill 只能看，不能改；新建一个同名的 skill 就能盖过它"}
LABEL = {"weaver": "", "claude": "（来自 Claude Code）", "builtin": "（内置）"}


def _dirs(home: Path, claude_home: Path, builtin: Path | None = None) -> list[tuple[Path, str]]:
    return [(Path(home) / "skills", "weaver"), (Path(claude_home) / "skills", "claude"),
            (Path(builtin or BUILTIN_DIR), "builtin")]


def _scan(home: Path, claude_home: Path) -> tuple[list[dict], list[dict]]:
    items, problems, seen = [], [], set()
    for root, source in _dirs(home, claude_home):
        if not root.is_dir():
            continue
        for f in sorted(root.glob("*/SKILL.md")):
            if f.parent.name.startswith("."):
                continue
            try:
                meta, _ = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            except OSError as e:
                problems.append({"path": str(f), "message": f"读不了：{e}"})
                continue
            name = str(meta.get("name") or "").strip()
            desc = " ".join(str(meta.get("description") or "").split())
            if not name or not desc:
                problems.append({"path": str(f), "message": "开头缺 name 或 description"})
                if source != "weaver":
                    continue
                name = f.parent.name                  # Weaver 的照样列出来，能打开修
            item = {"name": name, "description": desc, "source": source, "editable": source == "weaver",
                    "path": str(f.parent)}
            if name in seen:
                item["shadowed"] = True
            seen.add(name)
            items.append(item)
    return items, problems


def list_skills(home: Path, claude_home: Path) -> dict:
    items, problems = _scan(home, claude_home)
    return {"skills": items, "problems": problems}


def _find(home: Path, claude_home: Path, name: str) -> dict:
    item = next((s for s in _scan(home, claude_home)[0] if s["name"] == name and not s.get("shadowed")), None)
    if item is None:
        raise NotFound(f"没有叫 {name} 的 skill")
    return item


def read_skill(home: Path, claude_home: Path, name: str) -> dict:
    item = _find(home, claude_home, name)
    d = Path(item["path"])
    files = sorted(str(f.relative_to(d)) for f in d.rglob("*") if f.is_file() and f.name != "SKILL.md"
                   and not any(part.startswith(".") for part in f.relative_to(d).parts))
    return {"name": name, "text": (d / "SKILL.md").read_text(encoding="utf-8", errors="replace"),
            "editable": item["editable"], "files": files}


def _validate(home: Path, claude_home: Path, text: str, current: str | None) -> str:
    meta, _ = parse_frontmatter(text or "")
    if not meta:
        raise BadRequest("开头要有 --- 包起来的 name 和 description")
    name = str(meta.get("name") or "").strip()
    if not NAME.match(name):
        raise BadRequest("名字只能用小写字母、数字和 -，最长 64 个字")
    if not " ".join(str(meta.get("description") or "").split()):
        raise BadRequest("description 不能空：写一句它是干什么的、什么时候用")
    if name != current:
        other = next((s for s in _scan(home, claude_home)[0] if s["name"] == name and s["source"] != "builtin"), None)
        if other is not None:
            raise Conflict(f"已经有叫 {name} 的 skill 了{LABEL[other['source']]}")
    return name


def _write(f: Path, text: str) -> None:
    f = f.resolve() if f.is_symlink() else f           # 软链到 dotfiles 的：写到它指向的文件
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(".SKILL.md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(f)


def create_skill(home: Path, claude_home: Path, text: str) -> str:
    name = _validate(home, claude_home, text, None)
    target = Path(home) / "skills" / name
    if target.exists():                    # 目录名和 frontmatter 的 name 不一样时，按名字查重查不出来
        raise Conflict(f"已经有叫 {name} 的目录了：{target}")
    _write(target / "SKILL.md", text)
    return name


def _editable(home: Path, claude_home: Path, name: str) -> Path:
    item = _find(home, claude_home, name)
    if not item["editable"]:
        raise BadRequest(READONLY[item["source"]])
    return Path(item["path"])


def save_skill(home: Path, claude_home: Path, name: str, text: str) -> str:
    d = _editable(home, claude_home, name)
    new = _validate(home, claude_home, text, name)
    if new != name:                                    # 改名：整个目录（含脚本等）一起搬
        target = Path(home) / "skills" / new
        if target.exists():
            raise Conflict(f"已经有叫 {new} 的目录了：{target}")
        d.rename(target)
        d = target
    _write(d / "SKILL.md", text)
    return new


def remove_skill(home: Path, claude_home: Path, name: str) -> None:
    d = _editable(home, claude_home, name)
    dest = Path(home) / "skills" / ".archive" / f"{d.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(dest))
