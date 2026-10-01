"""存储：账本（EventStore）和文件仓库（BlobStore）。本地用文件，服务器以后换数据库/对象存储。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path


class Conflict(Exception):
    """有别人先写了一笔：重新翻账本再试。"""


class MemoryEventStore:
    def __init__(self):
        self.sessions: dict[str, list[dict]] = {}

    def load(self, session_id: str) -> list[dict]:
        return [dict(e) for e in self.sessions.get(session_id, [])]

    def append(self, session_id: str, events: list[dict], expected_seq: int) -> int:
        log = self.sessions.setdefault(session_id, [])
        if len(log) != expected_seq:
            raise Conflict(f"expected seq {expected_seq}, got {len(log)}")
        for i, e in enumerate(events, expected_seq + 1):
            log.append({**e, "seq": i})
        return len(log)


class _Tail:
    """一个会话文件读到哪了：文件身份（inode）、已解析到的字节位置、已解析的事件。"""
    __slots__ = ("ino", "pos", "events")

    def __init__(self, ino: int):
        self.ino, self.pos, self.events = ino, 0, []


class JsonlEventStore:
    """每个会话一个 JSONL 文件，一行一笔，只追加。seq 从 1 开始，等于行号。

    读：记住每个文件读到了哪个字节，下次只解析新增的部分（以前每次都整本重读，长会话越来越慢）。
    文件被换掉或变短了（inode 变了、比读到的位置还短），就从头重读。
    写：追加前如果文件末尾有写到一半的残行（上次写的时候崩了），先截掉，免得新记录接在残行后面、
    把它变成文件中间的坏行（那样它后面的记录都读不到了）。
    返回的事件列表是新列表，但里面的事件对象是共用的：调用方不要修改它们。
    多线程：读和写共用一把锁（常驻服务里接口线程和工作线程会同时读写同一本账本）。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._tails: dict[str, _Tail] = {}
        self._lock = threading.RLock()

    def path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def exists(self, session_id: str) -> bool:
        return self.path(session_id).exists()

    def _refresh(self, session_id: str) -> _Tail | None:
        p = self.path(session_id)
        try:
            st = p.stat()
        except FileNotFoundError:
            self._tails.pop(session_id, None)
            return None
        tail = self._tails.get(session_id)
        if tail is None or tail.ino != st.st_ino or st.st_size < tail.pos:
            tail = self._tails[session_id] = _Tail(st.st_ino)
        if st.st_size > tail.pos:
            with p.open("rb") as f:
                f.seek(tail.pos)
                chunk = f.read()
            offset = 0
            for raw in chunk[:chunk.rfind(b"\n") + 1].splitlines(keepends=True):   # 只解析完整的行
                if raw.strip():
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        break                           # 坏行：停在它前面（下次追加前会把它和后面的挪进备份）
                    tail.events.append(event)
                offset += len(raw)
            tail.pos += offset
        return tail

    def load(self, session_id: str) -> list[dict]:
        with self._lock:
            tail = self._refresh(session_id)
            return list(tail.events) if tail else []

    def append(self, session_id: str, events: list[dict], expected_seq: int) -> int:
        with self._lock:
            return self._append(session_id, events, expected_seq)

    def _append(self, session_id: str, events: list[dict], expected_seq: int) -> int:
        tail = self._refresh(session_id)
        current = len(tail.events) if tail else 0
        if current != expected_seq:
            raise Conflict(f"expected seq {expected_seq}, got {current}")
        p = self.path(session_id)
        if tail is not None and p.stat().st_size > tail.pos:
            # 读到的位置之后还有东西：上次崩溃留下的残行，或者一行坏数据。
            # 先挪进备份文件（不真删），再截掉，免得新记录接在它后面、让后面的都读不到
            with p.open("r+b") as f:
                f.seek(tail.pos)
                leftover = f.read()
                backup = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{tail.pos}")
                backup.write_bytes(leftover)
                f.truncate(tail.pos)
        lines = [json.dumps({**e, "seq": i}, ensure_ascii=False) + "\n" for i, e in enumerate(events, expected_seq + 1)]
        data = "".join(lines).encode("utf-8")
        with p.open("ab") as f:
            f.write(data)
            f.flush()
        tail = self._refresh(session_id) if tail is None else tail
        if tail.pos + len(data) == p.stat().st_size and tail.ino == p.stat().st_ino:
            tail.events.extend(json.loads(line) for line in lines)
            tail.pos += len(data)
        return expected_seq + len(events)


class MemoryBlobStore:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        ref = "blob://sha256-" + hashlib.sha256(data).hexdigest()
        self.data[ref] = data
        return ref

    def get(self, ref: str) -> bytes:
        return self.data[ref]


class DirBlobStore:
    """按内容哈希存文件，同样的内容只存一份。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        p = self.root / digest
        if not p.exists():
            p.write_bytes(data)
        return "blob://sha256-" + digest

    def get(self, ref: str) -> bytes:
        return (self.root / ref.removeprefix("blob://sha256-")).read_bytes()
