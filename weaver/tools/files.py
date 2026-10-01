"""读文件。"""
from __future__ import annotations

from pathlib import Path

MAX_LINE = 2000
MAX_BYTES = 50_000


def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if p.is_dir():
        raise IsADirectoryError(f"这是目录，不是文件：{path}")
    raw = p.read_bytes()
    if b"\0" in raw[:8000]:
        raise ValueError(f"看起来是二进制文件，不读：{path}")
    lines = raw.decode("utf-8", "replace").splitlines()
    offset, limit = max(1, int(offset)), max(1, int(limit))
    out, size, end = [], 0, offset - 1
    for i in range(offset - 1, min(len(lines), offset - 1 + limit)):
        line = lines[i]
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + "…[该行过长已截断]"
        row = f"{i + 1:6}\t{line}"
        size += len(row.encode()) + 1
        if size > MAX_BYTES and out:
            break
        out.append(row)
        end = i + 1
    text = "\n".join(out)
    if end < len(lines):
        text += f"\n…[文件共 {len(lines)} 行，已显示 {offset}–{end} 行；用 offset={end + 1} 继续读]"
    return text or f"[空内容：文件共 {len(lines)} 行]"
