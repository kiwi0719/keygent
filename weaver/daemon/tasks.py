"""任务存储：一个任务 = 一个会话 = 一本账本 + 元信息。见 design/daemon.md 第四节。

~/.weaver/tasks/<任务 id>/
    ledger.jsonl    账本（JsonlEventStore，会话名固定为 "ledger"）
    meta.json       元信息：标题、工作目录、创建时间、来源、预算
归档 = 整个目录移到 ~/.weaver/tasks/.archive/，不真删。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..stores import JsonlEventStore

LEDGER = "ledger"
TITLE_LEN = 20


@dataclass
class TaskMeta:
    id: str
    title: str
    workdir: str
    created: float
    source: str = "user"                 # user / trigger / …
    max_steps: int = 200
    token_budget: int = 1_000_000
    extra: dict = field(default_factory=dict)


def default_title(text: str) -> str:
    line = " ".join((text or "").split())
    return line[:TITLE_LEN] + ("…" if len(line) > TITLE_LEN else "") or "未命名任务"


def revoked_keys(task_dir: Path) -> set[str]:
    """这个任务里被撤销的“总是允许”（meta.json 的 extra.revoked）。读不到当作没有。"""
    try:
        data = json.loads((Path(task_dir) / "meta.json").read_text(encoding="utf-8"))
        return set((data.get("extra") or {}).get("revoked") or [])
    except (OSError, ValueError, AttributeError):
        return set()


def _write_atomic(path: Path, data: str) -> None:
    """先写临时文件再改名：进程在写的过程中崩溃也不会留下半个文件。"""
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


class TaskStore:
    def __init__(self, root: str | Path | None = None, scratch: str | Path | None = None):
        home = Path(os.environ.get("WEAVER_HOME") or "~/.weaver").expanduser()
        self.root = Path(root) if root else home / "tasks"
        self.scratch = Path(scratch) if scratch else Path(os.environ.get("WEAVER_SCRATCH") or "~/Weaver/scratch").expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._stores: dict[str, JsonlEventStore] = {}

    def dir(self, task_id: str) -> Path:
        return self.root / task_id

    def store(self, task_id: str) -> JsonlEventStore:
        """这个任务的账本存储（每个任务一个实例，复用增量读取的缓存）。"""
        if task_id not in self._stores:
            self._stores[task_id] = JsonlEventStore(self.dir(task_id))
        return self._stores[task_id]

    def events(self, task_id: str) -> list[dict]:
        if not self.dir(task_id).is_dir():          # 已归档：别因为读一下又把目录建回来
            return []
        try:
            return self.store(task_id).load(LEDGER)
        except FileNotFoundError:                   # 读的同时被归档移走了
            return []

    # ------------------------------------------------ 元信息

    def create(self, text: str = "", workdir: str | None = None, source: str = "user", **opts) -> TaskMeta:
        task_id = uuid.uuid4().hex[:12]
        if workdir:
            wd = Path(workdir).expanduser().resolve()
            if not wd.is_dir():
                raise ValueError(f"工作目录不存在：{workdir}")
        else:
            wd = self.scratch / task_id                 # 不指定就给一个空目录，免得默认在家目录乱写
            wd.mkdir(parents=True, exist_ok=True)
        meta = TaskMeta(id=task_id, title=default_title(text), workdir=str(wd), created=time.time(),
                        source=source, **{k: v for k, v in opts.items() if k in ("max_steps", "token_budget")})
        self.dir(task_id).mkdir(parents=True, exist_ok=True)
        self.save(meta)
        return meta

    def save(self, meta: TaskMeta) -> None:
        _write_atomic(self.dir(meta.id) / "meta.json", json.dumps(asdict(meta), ensure_ascii=False, indent=2))

    def get(self, task_id: str) -> TaskMeta | None:
        try:
            data = json.loads((self.dir(task_id) / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        known = {k: data[k] for k in TaskMeta.__dataclass_fields__ if k in data}
        return TaskMeta(**known)

    def set_title(self, task_id: str, title: str) -> None:
        meta = self.get(task_id)
        if meta:
            meta.title = " ".join(title.split())[:60] or meta.title
            self.save(meta)

    def updated(self, task_id: str) -> float:
        """最后一次有动静的时间：账本最后修改时间，没有账本就用创建时间。"""
        ledger = self.dir(task_id) / f"{LEDGER}.jsonl"
        try:
            return ledger.stat().st_mtime
        except FileNotFoundError:
            pass
        meta = self.get(task_id)
        return meta.created if meta else 0.0

    def list(self) -> list[TaskMeta]:
        """所有没归档的任务，最近有动静的排前面。"""
        metas = [m for d in self.root.iterdir() if d.is_dir() and not d.name.startswith(".")
                 if (m := self.get(d.name)) is not None]
        return sorted(metas, key=lambda m: -self.updated(m.id))

    # ------------------------------------------------ 归档

    def archived_dirs(self) -> dict[str, Path]:
        """归档的任务：任务 id → 最新的那个归档目录（同一个任务恢复后又归档过，取最新的）。"""
        out: dict[str, Path] = {}
        root = self.root / ".archive"
        if not root.is_dir():
            return out
        for d in sorted(root.iterdir(), key=lambda d: d.name):
            if d.is_dir() and (d / "meta.json").exists():
                task_id = d.name.rsplit("-", 2)[0] if d.name.count("-") >= 2 else d.name
                out[task_id] = d
        return out

    def archived_meta(self, d: Path) -> TaskMeta | None:
        try:
            data = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return TaskMeta(**{k: data[k] for k in TaskMeta.__dataclass_fields__ if k in data})

    def archived_store(self, d: Path) -> JsonlEventStore:
        return JsonlEventStore(d)

    def restore(self, task_id: str) -> TaskMeta:
        d = self.archived_dirs().get(task_id)
        if d is None:
            raise FileNotFoundError(f"没有这个归档的任务：{task_id}")
        if self.dir(task_id).exists():
            raise FileExistsError(f"已经有这个任务了：{task_id}")
        shutil.move(str(d), str(self.dir(task_id)))
        self._stores.pop(task_id, None)
        meta = self.get(task_id)
        assert meta is not None
        return meta

    def archive(self, task_id: str) -> Path:
        src = self.dir(task_id)
        if not (src / "meta.json").exists():
            raise FileNotFoundError(f"没有这个任务：{task_id}")
        dest = self.root / ".archive" / f"{task_id}-{time.strftime('%Y%m%d-%H%M%S')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        self._stores.pop(task_id, None)
        return dest
