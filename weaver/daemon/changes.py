"""一个任务改了哪些文件：按文件汇总撤销日志、出 diff、整个文件撤销。见 design/keygent-gaps.md 第一节。

撤销日志（weaver/tools/edit.py 的 UndoLog）在任务工作目录的 .weaver/undo/<任务 id>.jsonl：
每次改文件前存原文，改完记新内容的哈希（新记录还存一份新内容）。主任务和它的子 Agent 记在同一个文件里。
"""
from __future__ import annotations

import difflib
from pathlib import Path

from ..tools.edit import UndoLog, sha

DIFF_LINES = 400                   # 一个文件的 diff 最多给几行
PREVIEW_LINES = 200                # 步骤、审批里从参数算的 diff 最多几行


def _text(data: bytes | None) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace")


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def counts(before: str, after: str) -> tuple[int, int]:
    """加了几行、删了几行。"""
    added = removed = 0
    for line in difflib.unified_diff(_lines(before), _lines(after), n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def unified(before: str, after: str, name: str, limit: int = DIFF_LINES, context: int = 3) -> str:
    """统一 diff 文本（不带 ---/+++ 文件头），超出 limit 行截断并说明。"""
    out = []
    for line in difflib.unified_diff(_lines(before), _lines(after), n=context):
        if line.startswith(("---", "+++")):
            continue
        out.append(line if line.endswith("\n") else line + "\n")
    if len(out) > limit:
        more = len(out) - limit
        out = out[:limit] + [f"…… 还有 {more} 行\n"]
    return "".join(out)


def edit_diff(name: str, args: dict) -> str:
    """从工具参数算 diff（步骤里用，不读磁盘）：edit_file 是 old_string → new_string，write_file 全是新加的行。"""
    if not isinstance(args, dict):
        return ""
    if name == "edit_file":
        old, new = args.get("old_string"), args.get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            return unified(old, new, "", limit=PREVIEW_LINES)
    if name == "write_file" and isinstance(args.get("content"), str):
        return unified("", args["content"], "", limit=PREVIEW_LINES)
    return ""


def call_diff(name: str, args: dict, root: str | Path) -> str:
    """审批里的 diff：按磁盘上的现状和参数算（文件不存在 = 新建）。算不出来回空字符串。"""
    if name not in ("edit_file", "write_file") or not isinstance(args, dict):
        return ""
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        return ""
    p = Path(raw).expanduser()
    p = p if p.is_absolute() else Path(root) / p
    try:
        current = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    except OSError:
        return edit_diff(name, args)
    if name == "write_file":
        content = args.get("content")
        return unified(current, content, p.name) if isinstance(content, str) else ""
    old, new = args.get("old_string"), args.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not old or old not in current:
        return edit_diff(name, args)
    after = current.replace(old, new) if args.get("replace_all") else current.replace(old, new, 1)
    return unified(current, after, p.name)


class Changes:
    """一个任务的撤销日志，按文件看。"""

    def __init__(self, workdir: str | Path, session: str, blobs):
        self.root = Path(workdir)
        self.log = UndoLog(self.root, session, blobs)
        self.blobs = blobs

    def _rel(self, path: str) -> str:
        try:
            return Path(path).relative_to(self.root.resolve()).as_posix()
        except ValueError:
            home = str(Path.home())
            return "~" + path[len(home):] if path.startswith(home + "/") else path

    def _blob(self, ref: str | None) -> bytes | None:
        if not ref:
            return None
        try:
            return self.blobs.get(ref)
        except OSError:
            return None

    def _groups(self) -> tuple[dict[str, list[tuple[int, dict]]], set[int]]:
        entries, undone = self.log.history()
        groups: dict[str, list[tuple[int, dict]]] = {}
        for i, e in enumerate(entries):
            groups.setdefault(e["path"], []).append((i, e))
        return groups, undone

    def _ends(self, items: list[tuple[int, dict]], undone: set[int]) -> tuple[bytes | None, bytes | None, bool]:
        """(任务第一次改之前, 任务最后一次改之后, 拿得到“之后”的内容吗)。只看没撤销的那几次。"""
        live = [e for i, e in items if i not in undone]
        first, last = live[0], live[-1]
        before = self._blob(first["before"])
        after = self._blob(last.get("after_blob"))
        if after is None:                           # 老记录没存新内容：磁盘上的现状哈希对得上就用它
            p = Path(last["path"])
            cur = p.read_bytes() if p.is_file() else None
            if cur is not None and sha(cur) == last["after"]:
                after = cur
        return before, after, after is not None

    def summary(self) -> list[dict]:
        """[{path, rel, added, removed, created, undone, can_undo, why}]，按第一次改的先后。"""
        groups, undone = self._groups()
        out = []
        for path, items in groups.items():
            item = {"path": path, "rel": self._rel(path), "added": 0, "removed": 0,
                    "created": items[0][1]["before"] is None, "undone": False, "can_undo": False, "why": ""}
            if all(i in undone for i, _ in items):
                item["undone"] = True
                out.append(item)
                continue
            before, after, known = self._ends(items, undone)
            if known:
                item["added"], item["removed"] = counts(_text(before), _text(after))
            last = [e for i, e in items if i not in undone][-1]
            p = Path(path)
            cur = p.read_bytes() if p.is_file() else None
            if cur is None:
                item["why"] = "文件被删了"
            elif sha(cur) != last["after"]:
                item["why"] = "之后又被改过"
            else:
                item["can_undo"] = True
            out.append(item)
        return out

    def diff(self, path: str) -> dict:
        groups, undone = self._groups()
        key = self._match(path, groups)
        items = groups[key]
        if all(i in undone for i, _ in items):
            first = items[0][1]
            before, after = self._blob(first["before"]), self._blob(items[-1][1].get("after_blob"))
        else:
            before, after, known = self._ends(items, undone)
            if not known:
                return {"path": key, "rel": self._rel(key), "diff": "", "why": "改动之后文件又变了，算不出当时的 diff"}
        return {"path": key, "rel": self._rel(key), "diff": unified(_text(before), _text(after), Path(key).name)}

    def rel(self, path: str) -> str:
        groups, _ = self._groups()
        return self._rel(self._match(path, groups))

    def undo(self, path: str) -> int:
        groups, _ = self._groups()
        return self.log.undo_file(self._match(path, groups))

    def _match(self, path: str, groups: dict) -> str:
        if path in groups:
            return path
        p = Path(path).expanduser()
        key = str((p if p.is_absolute() else self.root / p).resolve())
        if key not in groups:
            raise KeyError(path)
        return key
