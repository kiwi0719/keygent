"""跨任务搜索：所有任务（会话）的账本里“可搜的文字”，按文件增量缓存。见 design/round3.md 第三节。

- 每本账本抽出用户的话、模型的回复、工具结果、压缩摘要（背景信息不搜），带上序号和时间
- 账本只追加：按（inode、读到的位置）接着读新增的部分，旧的不重读
- 查询：所有关键字都出现（子串匹配，中文不用分词）；同一工作目录的排前面，再按时间从新到旧
- 内存上限：超了按最久没用的丢掉，下次用到再读
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_CHARS = 100_000_000                  # 缓存的文字总量上限（约 200 MB 内存）
EXCERPT = 300


def entry_text(ev: dict) -> tuple[str, str]:
    """(哪一类, 文字)；不值得搜的返回空。"""
    t = ev.get("type")
    if t == "InputReceived" and ev.get("source") != "context":
        who = {"user": "用户", "system": "系统通知", "policy": "提醒", "trigger": "触发"}.get(ev.get("source"), "输入")
        return who, "\n".join(p.get("text", f"[{p.get('type')}：{p.get('name', '')}]") for p in ev.get("content") or [])
    if t == "ActionCompleted" and ev.get("kind") == "model" and not ev.get("is_error"):
        parts = (ev.get("output") or {}).get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        calls = [f"调用 {p['name']}({json.dumps(p.get('args'), ensure_ascii=False)})" for p in parts
                 if p.get("type") == "tool_call"]
        return "模型", "\n".join(x for x in [text] + calls if x)
    if t == "ActionCompleted" and ev.get("kind") == "tool":
        out = ev.get("output")
        return "工具结果", out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    if t == "ActionCompleted" and ev.get("kind") == "compact" and (ev.get("output") or {}).get("summary"):
        return "压缩摘要", ev["output"]["summary"]
    return "", ""


def excerpt(text: str, words: list[str], size: int = EXCERPT) -> str:
    low = text.lower()
    found = [i for i in (low.find(w) for w in words) if i >= 0]
    at = min(found) if found else 0
    start = max(0, at - size // 3)
    piece = " ".join(text[start:start + size].split())
    return ("…" if start else "") + piece + ("…" if start + size < len(text) else "")


@dataclass
class Source:
    """一个任务（会话）：主账本 + 子 Agent 的账本。"""
    task: str
    title: str
    workdir: str
    files: list[Path]                    # 第一本是主账本
    archived: bool = False


@dataclass
class _Doc:
    ino: int = 0
    pos: int = 0
    entries: list = field(default_factory=list)      # (seq, ts, kind, text, lower)
    chars: int = 0


class Corpus:
    def __init__(self, sources: Callable[[bool], list[Source]], max_chars: int = MAX_CHARS):
        self.sources = sources
        self.max_chars = max_chars
        self._docs: OrderedDict[Path, _Doc] = OrderedDict()
        self._chars = 0
        self._lock = threading.Lock()

    def _doc(self, path: Path) -> _Doc:
        """这本账本的可搜文字；只读新增的部分。"""
        try:
            st = path.stat()
        except OSError:
            return _Doc()
        with self._lock:
            doc = self._docs.pop(path, None)
            if doc is None or doc.ino != st.st_ino or st.st_size < doc.pos:
                if doc:
                    self._chars -= doc.chars
                doc = _Doc(ino=st.st_ino)
            self._docs[path] = doc                          # 移到“最近用过”的一端
        if st.st_size > doc.pos:
            try:
                with open(path, "rb") as f:
                    f.seek(doc.pos)
                    chunk = f.read(st.st_size - doc.pos)
            except OSError:
                return doc
            end = chunk.rfind(b"\n") + 1                     # 只处理完整的行
            added = 0
            for raw in chunk[:end].splitlines():
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                kind, text = entry_text(ev)
                if kind and text:
                    doc.entries.append((ev.get("seq", 0), ev.get("ts", 0), kind, text, text.lower()))
                    added += len(text)
            with self._lock:
                doc.pos += end
                doc.chars += added
                self._chars += added
                while self._chars > self.max_chars and len(self._docs) > 1:
                    old_path, old = self._docs.popitem(last=False)
                    if old_path == path:
                        self._docs[old_path] = old
                        break
                    self._chars -= old.chars
        return doc

    def search(self, query: str, archived: bool = False, workdir: str | None = None, limit: int = 10) -> list[dict]:
        words = [w.lower() for w in query.split() if w]
        if not words:
            return []
        hits = []
        for src in self.sources(archived):
            for i, path in enumerate(src.files):
                for seq, ts, kind, text, low in self._doc(path).entries:
                    if all(w in low for w in words):
                        hits.append({"task": src.task, "task_title": src.title, "workdir": src.workdir,
                                     "archived": src.archived, "sub": "" if i == 0 else path.stem,
                                     "seq": seq, "ts": ts, "kind": kind, "excerpt": excerpt(text, words)})
        same = (lambda h: h["workdir"] == workdir) if workdir else (lambda h: False)
        hits.sort(key=lambda h: (not same(h), -h["ts"]))
        return hits[:limit]

    def get(self, task: str, seq: int, archived: bool = True) -> tuple[Source, tuple] | None:
        """某个任务主账本里第 seq 条的原文。task 可以只写前几位。"""
        matches = [s for s in self.sources(archived) if s.task.startswith(task)]
        if len(matches) != 1:
            return None
        src = matches[0]
        for e in self._doc(src.files[0]).entries:
            if e[0] == seq:
                return src, e
        return src, ()


# ------------------------------------------------ 两种布局的任务来源

def daemon_sources(root: str | Path) -> Callable[[bool], list[Source]]:
    """常驻服务：<root>/<任务 id>/ledger*.jsonl，归档的在 <root>/.archive/<任务 id>-<时间>/。"""
    root = Path(root)

    def scan(d: Path, archived: bool) -> Source | None:
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        files = sorted(d.glob("ledger*.jsonl"), key=lambda p: (p.name != "ledger.jsonl", p.name))
        if not files:
            return None
        return Source(meta.get("id", d.name), meta.get("title", d.name), meta.get("workdir", ""), files, archived)

    def sources(archived: bool) -> list[Source]:
        out = []
        dirs = [(d, False) for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")] if root.exists() else []
        if archived and (root / ".archive").exists():
            dirs += [(d, True) for d in (root / ".archive").iterdir() if d.is_dir()]
        for d, arch in dirs:
            src = scan(d, arch)
            if src:
                out.append(src)
        return out
    return sources


def session_sources(sessions: str | Path, workdir: str | Path) -> Callable[[bool], list[Source]]:
    """命令行：<项目>/.weaver/sessions/<会话>.jsonl，子 Agent 是 <会话>--<id>.jsonl。标题取第一句用户的话。"""
    sessions, workdir = Path(sessions), str(Path(workdir).resolve())

    def title(path: Path) -> str:
        try:
            with open(path, encoding="utf-8") as f:
                for _, line in zip(range(50), f):
                    ev = json.loads(line)
                    if ev.get("type") == "InputReceived" and ev.get("source") == "user":
                        text = " ".join(entry_text(ev)[1].split())
                        return text[:20] + ("…" if len(text) > 20 else "")
        except (OSError, ValueError):
            pass
        return path.stem

    def sources(archived: bool) -> list[Source]:
        if not sessions.exists():
            return []
        groups: dict[str, list[Path]] = {}
        for p in sessions.glob("*.jsonl"):
            groups.setdefault(p.stem.split("--")[0], []).append(p)
        out = []
        for sid, files in groups.items():
            files.sort(key=lambda p: (p.stem != sid, p.name))
            if files[0].stem == sid:
                out.append(Source(sid, title(files[0]), workdir, files))
        return out
    return sources

