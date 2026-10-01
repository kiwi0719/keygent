"""写文件、改文件，以及“先读过”记录和撤销日志。见 design/write-tools.md 第二、三节。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileTracker:
    """记下哪些文件读过、当时内容的哈希。只在内存里：进程重启后要重新读，宁可多读，不盲改。"""

    def __init__(self):
        self.seen: dict[str, str] = {}

    def mark(self, path: Path) -> None:
        try:
            self.seen[str(path.resolve())] = sha(path.read_bytes())
        except OSError:
            pass

    def check(self, path: Path) -> str | None:
        """改已有文件前检查。返回问题说明，没问题返回 None。"""
        key = str(path.resolve())
        if key not in self.seen:
            return f"还没读过 {path.name}，请先用 read_file 读一下再改（进程重启后也要重新读）"
        if sha(path.read_bytes()) != self.seen[key]:
            return f"{path.name} 在你读过之后被改过了，请重新 read_file 再改"
        return None


class UndoLog:
    """每次改文件前把原文存进 BlobStore，并在 .weaver/undo/<会话>.jsonl 记一行。"""

    def __init__(self, root: Path, session: str, blobs):
        self.root, self.blobs = root, blobs
        self.path = root / ".weaver" / "undo" / f"{session}.jsonl"

    def record(self, path: Path, before: bytes | None, after: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"path": str(path.resolve()), "before": self.blobs.put(before) if before is not None else None,
                 "after": sha(after), "ts": time.time()}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load(self) -> tuple[list[dict], set[int]]:
        if not self.path.exists():
            return [], set()
        entries, undone = [], set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if "undone" in e:
                undone.add(e["undone"])
            else:
                entries.append(e)
        return entries, undone

    def undo_last(self) -> str:
        """撤销最近一次还没撤销的改动。文件之后又被改过就拒绝，免得冲掉别人的修改。"""
        entries, undone = self._load()
        idx = next((i for i in range(len(entries) - 1, -1, -1) if i not in undone), None)
        if idx is None:
            return "没有可以撤销的文件改动"
        e = entries[idx]
        path = Path(e["path"])
        current = path.read_bytes() if path.exists() else None
        if current is None or sha(current) != e["after"]:
            raise RuntimeError(f"{e['path']} 在那次改动之后又被改过（或被删了），为了不冲掉后来的修改，拒绝撤销")
        if e["before"] is None:
            path.unlink()
            msg = f"已撤销：删除了当时新建的 {e['path']}"
        else:
            path.write_bytes(self.blobs.get(e["before"]))
            msg = f"已撤销：{e['path']} 恢复到改动之前"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"undone": idx}) + "\n")
        left = sum(1 for i in range(len(entries)) if i not in undone and i != idx)
        return msg + (f"（还可以再撤销 {left} 次）" if left else "")


def _snippet(text: str, start: int, length: int, around: int = 3) -> str:
    """改动位置前后几行，带行号。"""
    lines = text.splitlines()
    first = text.count("\n", 0, start)
    last = first + max(text.count("\n", start, start + length), 0)
    lo, hi = max(0, first - around), min(len(lines), last + around + 1)
    return "\n".join(f"{i + 1:6}\t{lines[i]}" for i in range(lo, hi))


PLACEHOLDER = "[已脱敏 "
PLACEHOLDER_REFUSED = ("内容里有脱敏占位符“[已脱敏 …]”，写回去会把原来的密钥冲掉。"
                       "只改需要改的部分（用 edit_file，且不要包含有占位符的那一行），或者请用户自己改。")


class FileEditor:
    def __init__(self, tracker: FileTracker, undo: UndoLog | None):
        self.tracker, self.undo = tracker, undo

    def _save(self, path: Path, before: bytes | None, text: str) -> None:
        data = text.encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if self.undo:
            self.undo.record(path, before, data)
        self.tracker.mark(path)            # 自己写的内容算“读过”，可以接着改

    def write_file(self, path: Path, content: str) -> str:
        if PLACEHOLDER in content:
            raise ValueError(PLACEHOLDER_REFUSED)
        if path.is_dir():
            raise IsADirectoryError(f"这是目录：{path}")
        before = None
        if path.exists():
            problem = self.tracker.check(path)
            if problem:
                raise PermissionError(problem)
            before = path.read_bytes()
        self._save(path, before, content)
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        verb = "已覆盖" if before is not None else "已新建"
        return f"{verb} {path}（{lines} 行，{len(content.encode())} 字节）"

    def edit_file(self, path: Path, old_string: str, new_string: str, replace_all: bool = False) -> str:
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}（新建文件请用 write_file）")
        if old_string == new_string:
            raise ValueError("old_string 和 new_string 一样，没有要改的")
        if PLACEHOLDER in new_string:
            raise ValueError(PLACEHOLDER_REFUSED)
        if not old_string:
            raise ValueError("old_string 不能为空")
        problem = self.tracker.check(path)
        if problem:
            raise PermissionError(problem)
        before = path.read_bytes()
        text = before.decode("utf-8")
        count = text.count(old_string)
        if count == 0:
            raise ValueError("在文件里没找到 old_string。请确认空格、缩进、换行和原文完全一致（可以先 read_file 看一下）")
        if count > 1 and not replace_all:
            raise ValueError(f"old_string 在文件里出现了 {count} 次。请多带几行上下文让它唯一，"
                             "或者确实要全部替换时设 replace_all=true")
        start = text.find(old_string)
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        self._save(path, before, new_text)
        head = f"已修改 {path}（替换了 {count if replace_all else 1} 处）。改动附近："
        return head + "\n" + _snippet(new_text, start, len(new_string))
