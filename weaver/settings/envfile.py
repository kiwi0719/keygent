"""~/.weaver/.env：按行读写，只改指定的几项，注释和别的配置原样保留。

解析规则和 weaver.providers.load_env 一致（KEY=VALUE、去引号、同一个 key 以第一次出现的为准）；
写法和 Keygent 的 EnvFile（Model/ModelConfig.swift）一致。
"""
from __future__ import annotations

import os
from pathlib import Path


def _parse(line: str) -> tuple[str, str] | None:
    t = line.strip()
    if not t or t.startswith("#") or "=" not in t:
        return None
    key, val = t.split("=", 1)
    return key.strip(), val.strip().strip('"').strip("'")


def write_private(path: Path, text: str) -> None:
    """先写临时文件（建成 0600）再改名：别人任何时候都读不到，写到一半崩溃也不留半个文件。
    软链（比如 .env 链到 dotfiles）写到它指向的文件，不把链接本身换掉。"""
    path = path.resolve() if path.is_symlink() else path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class EnvFile:
    def __init__(self, lines: list[str] | None = None):
        self.lines = list(lines or [])

    @classmethod
    def load(cls, path: Path) -> "EnvFile":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return cls(lines)

    def get(self, key: str) -> str | None:
        for line in self.lines:
            kv = _parse(line)
            if kv and kv[0] == key:
                return kv[1] or None
        return None

    def set(self, key: str, value: str | None) -> None:
        """None 或空 = 删掉这一项。同一个 key 有多行时只留第一行（改成新值），其余删掉。"""
        v = (value or "").replace("\n", "").strip()
        out, done = [], False
        for line in self.lines:
            kv = _parse(line)
            if not kv or kv[0] != key:
                out.append(line)
                continue
            if not done and v:
                out.append(f"{key}={v}")
            done = True
        if not done and v:
            out.append(f"{key}={v}")
        self.lines = out

    def write(self, path: Path) -> None:
        write_private(Path(path), "\n".join(self.lines) + "\n")
