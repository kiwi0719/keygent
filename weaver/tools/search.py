"""搜索：grep（按内容）和 find_files（按文件名）。见 design/tools.md。

优先用 ripgrep（项目自带固定版本）；找不到就用纯 Python，输出格式一样。运行时从不联网下载。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

MAX_LINE = 300                  # 搜索结果里单行最多多少字
MAX_BYTES = 50_000
TIMEOUT = 30
MAX_FILE_BYTES = 5_000_000      # 纯 Python 搜索时跳过的大文件
SKIP_DIRS = (".git", ".weaver", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache")
RG_CANDIDATES = ("/opt/homebrew/bin/rg", "/usr/local/bin/rg", "/usr/bin/rg", "~/.cargo/bin/rg")
AUTO = object()


# 随项目打包的 ripgrep 15.2.0（MIT / Unlicense，许可证见 weaver/vendor/ripgrep/）。
# 来源：github.com/BurntSushi/ripgrep/releases/tag/15.2.0，压缩包已用官方 .sha256 校验；这里记的是解压后 rg 的哈希。
VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "ripgrep"
VENDORED = {
    ("Darwin", "arm64"): ("aarch64-apple-darwin",
                          "a326a1fb48074202e9ad41e4cd1e389eeea372c8c6f7d7e80da81176d5d9430e"),
    ("Linux", "x86_64"): ("x86_64-unknown-linux-musl",
                          "e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849"),
}
_vendored_cache: dict = {}


def vendored_rg() -> str | None:
    """本平台有打包的 rg、且哈希对得上，才用它（文件被替换过就不用）。"""
    key = (platform.system(), platform.machine())
    if key in _vendored_cache:
        return _vendored_cache[key]
    found = None
    if key in VENDORED:
        target, digest = VENDORED[key]
        p = VENDOR / target / "rg"
        if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == digest:
            if not os.access(p, os.X_OK):
                p.chmod(0o755)
            found = str(p)
    _vendored_cache[key] = found
    return found


def find_rg() -> str | None:
    """WEAVER_RG → 项目自带 → PATH → 常见安装位置。Claude Code 终端里的 rg 是 shell 函数，这里找不到它。"""
    env = os.environ.get("WEAVER_RG")
    if env:
        return env if Path(env).expanduser().exists() else None
    found = vendored_rg() or shutil.which("rg")
    if found:
        return found
    for c in RG_CANDIDATES:
        p = Path(c).expanduser()
        if p.exists():
            return str(p)
    return None


def _clip(line: str) -> tuple[str, bool]:
    line = line.rstrip("\r\n")
    return (line[:MAX_LINE] + "…", True) if len(line) > MAX_LINE else (line, False)


def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    return [x for alt in m.group(1).split(",") for x in _expand_braces(pattern[:m.start()] + alt + pattern[m.end():])]


def match_glob(rel: str, pattern: str) -> bool:
    """近似 ripgrep 的 glob：不含 / 时匹配文件名，含 / 时匹配相对路径；** 可以匹配零层目录。"""
    name = rel.rsplit("/", 1)[-1]
    for p in _expand_braces(pattern):
        if "/" not in p.lstrip("/"):
            if fnmatch.fnmatchcase(name, p):
                return True
            continue
        p = p.lstrip("/")
        if fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(rel, p.replace("**/", "")):
            return True
    return False


class GitIgnore:
    """只支持常见写法的 .gitignore：注释、取反、目录（结尾 /）、带 / 的路径模式、通配符。"""

    def __init__(self, root: Path):
        self.rules: list[tuple[str, bool, bool]] = []      # (模式, 是否取反, 是否只匹配目录)
        f = root / ".gitignore"
        if f.exists():
            for line in f.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                neg = line.startswith("!")
                line = line[1:] if neg else line
                self.rules.append((line.rstrip("/"), neg, line.endswith("/")))

    def ignored(self, rel: str, is_dir: bool) -> bool:
        result = False
        for pat, neg, dir_only in self.rules:
            if dir_only and not is_dir:
                continue
            if "/" in pat:
                hit = fnmatch.fnmatchcase(rel, pat.lstrip("/"))
            else:
                hit = any(fnmatch.fnmatchcase(part, pat) for part in rel.split("/"))
            if hit:
                result = not neg
        return result


class Searcher:
    def __init__(self, root: str | Path = ".", rg=AUTO):
        self.root = Path(root).resolve()
        self.rg = find_rg() if rg is AUTO else rg

    # ------------------------------------------------ 公共

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else (self.root / p)

    def rel(self, p: str | Path) -> str:
        p = Path(p)
        p = p if p.is_absolute() else (self.root / p)
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    @staticmethod
    def _finish(lines: list[str], notices: list[str]) -> str:
        out, size = [], 0
        for line in lines:
            size += len(line.encode()) + 1
            if size > MAX_BYTES:
                notices.append(f"输出超过 {MAX_BYTES // 1000}KB 已截断，请缩小范围")
                break
            out.append(line)
        text = "\n".join(out)
        return text + (f"\n\n[{'；'.join(notices)}]" if notices else "")

    # ------------------------------------------------ grep

    def grep(self, pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False,
             literal: bool = False, context: int = 0, limit: int = 100) -> str:
        target = self.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"路径不存在：{path}")
        limit, context = max(1, int(limit)), max(0, int(context))
        if not literal:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"正则写错了：{e}。想按普通字符串搜，请设 literal=true") from None
        hits = (self._grep_rg if self.rg else self._grep_py)(pattern, target, glob, ignore_case, literal,
                                                             context, limit)
        rows, matched, clipped = hits
        if not matched:
            return "没有找到匹配"
        notices = []
        if matched >= limit:
            notices.append(f"已达到 {limit} 条上限，可以调大 limit，或用更具体的 pattern / path / glob")
        if clipped:
            notices.append(f"部分行超过 {MAX_LINE} 字已截断，完整内容用 read_file 看")
        return self._finish(rows, notices)

    def _format(self, blocks: list[tuple[str, int, str, bool]], context: int) -> tuple[list[str], bool]:
        """blocks: (路径, 行号, 内容, 是否匹配行)。上下文行用 path-N- 标记，不连续的块用 -- 隔开。"""
        rows, clipped, last = [], False, None
        for path, n, text, is_match in blocks:
            if context and last and (last[0] != path or n > last[1] + 1):
                rows.append("--")
            text, c = _clip(text)
            clipped |= c
            rows.append(f"{path}:{n}: {text}" if is_match else f"{path}-{n}- {text}")
            last = (path, n)
        return rows, clipped

    def _grep_rg(self, pattern, target, glob, ignore_case, literal, context, limit):
        args = [self.rg, "--json", "--line-number", "--color=never", "--hidden"]
        for d in SKIP_DIRS:
            args += ["--glob", f"!{d}"]
        if ignore_case:
            args.append("--ignore-case")
        if literal:
            args.append("--fixed-strings")
        if context:
            args += ["--context", str(context)]
        if glob:
            args += ["--glob", glob]
        args += ["--", pattern, str(target)]
        blocks, seen, matched = [], set(), 0
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.root)
        timer = threading.Timer(TIMEOUT, proc.kill)
        timer.start()
        try:
            for raw in proc.stdout:
                ev = json.loads(raw)
                if ev["type"] not in ("match", "context"):
                    continue
                if ev["type"] == "match" and matched >= limit:
                    proc.kill()
                    break
                d = ev["data"]
                path = self.rel(d["path"].get("text", ""))
                key = (path, d["line_number"])
                if key in seen:
                    continue
                seen.add(key)
                text = d["lines"].get("text", "")
                blocks.append((path, d["line_number"], text, ev["type"] == "match"))
                matched += ev["type"] == "match"
            proc.wait()
            err = proc.stderr.read().decode(errors="replace").strip()
        finally:
            timer.cancel()
            proc.stdout.close()
            proc.stderr.close()
        if proc.returncode not in (0, 1) and matched < limit and proc.returncode != -9:
            raise RuntimeError(f"ripgrep 出错：{err or proc.returncode}")
        if proc.returncode == -9 and matched < limit:
            raise TimeoutError(f"搜索超过 {TIMEOUT} 秒，请缩小范围")
        # 上限之后多读到的上下文行保留，但只留在最后一个匹配附近
        rows, clipped = self._format(blocks, context)
        return rows, matched, clipped

    def _walk(self, target: Path, glob: str | None):
        if target.is_file():
            yield target
            return
        ignore = GitIgnore(self.root)
        for dirpath, dirnames, filenames in os.walk(target):
            base = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                rel = self.rel(base / d)
                if d in SKIP_DIRS or ignore.ignored(rel, True):
                    continue
                keep.append(d)
            dirnames[:] = keep
            for f in sorted(filenames):
                p = base / f
                rel = self.rel(p)
                if ignore.ignored(rel, False) or (glob and not match_glob(rel, glob)):
                    continue
                yield p

    def _grep_py(self, pattern, target, glob, ignore_case, literal, context, limit):
        rx = re.compile(re.escape(pattern) if literal else pattern, re.IGNORECASE if ignore_case else 0)
        deadline = time.monotonic() + TIMEOUT
        blocks, matched = [], 0
        for p in self._walk(target, glob):
            if time.monotonic() > deadline:
                raise TimeoutError(f"搜索超过 {TIMEOUT} 秒，请缩小范围")
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8000]:
                continue
            lines = data.decode("utf-8", "replace").splitlines()
            path = self.rel(p)
            shown = set()
            for i, line in enumerate(lines):
                if not rx.search(line):
                    continue
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    if j not in shown:
                        shown.add(j)
                        blocks.append((path, j + 1, lines[j], j == i or bool(rx.search(lines[j]))))
                matched += 1
                if matched >= limit:
                    break
            if matched >= limit:
                break
        rows, clipped = self._format(blocks, context)
        return rows, matched, clipped

    # ------------------------------------------------ find_files

    def find_files(self, pattern: str, path: str = ".", limit: int = 200) -> str:
        target = self.resolve(path)
        if not target.is_dir():
            raise NotADirectoryError(f"不是目录：{path}")
        limit = max(1, int(limit))
        files = self._find_rg(pattern, target) if self.rg else \
            [p for p in self._walk(target, None) if match_glob(self.rel(p), pattern)]
        if not files:
            return "没有找到文件"

        def mtime(p):
            try:
                return -Path(p).stat().st_mtime
            except OSError:
                return 0
        files = sorted(files, key=lambda p: (mtime(p), self.rel(p)))       # 最近修改的排前面
        notices = []
        if len(files) > limit:
            notices.append(f"共 {len(files)} 个，只显示最近修改的 {limit} 个；可以调大 limit 或用更具体的 pattern")
        return self._finish([self.rel(p) for p in files[:limit]], notices)

    def _find_rg(self, pattern, target) -> list[Path]:
        args = [self.rg, "--files", "--hidden", "--color=never"]
        for d in SKIP_DIRS:
            args += ["--glob", f"!{d}"]
        args += ["--glob", pattern, str(target)]
        try:
            r = subprocess.run(args, capture_output=True, timeout=TIMEOUT, cwd=self.root)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"查找超过 {TIMEOUT} 秒，请缩小范围") from None
        if r.returncode not in (0, 1):
            raise RuntimeError(f"ripgrep 出错：{r.stderr.decode(errors='replace').strip()}")
        return [Path(line) for line in r.stdout.decode(errors="replace").splitlines() if line]
