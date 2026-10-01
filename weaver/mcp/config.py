"""MCP 配置：读三处配置文件、展开 ${VAR}、项目级配置要先信任一次。见 design/mcp.md 第三节。

格式和 Claude Code 的 .mcp.json 一样：{"mcpServers": {名字: {command, args, env, cwd} 或 {type: "http", url, headers}}}。
这个文件不依赖 MCP SDK，没装 SDK 也能读配置、给出提示。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ServerConfig:
    name: str
    level: str                       # project / user
    source: str                      # 来自哪个文件
    raw: dict
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    trust: str = ""                  # "" / readonly / all
    problem: str = ""                # 配置有问题时说明原因（不连接）

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _expand(value, env: dict, missing: set):
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in env:
                missing.add(m.group(1))
                return ""
            return env[m.group(1)]
        return VAR.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v, env, missing) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, env, missing) for k, v in value.items()}
    return value


def parse(name: str, raw: dict, level: str, source: str, env: dict) -> ServerConfig:
    missing: set[str] = set()
    data = _expand(raw, env, missing)
    cfg = ServerConfig(name=name, level=level, source=source, raw=raw, trust=str(raw.get("trust", "")))
    kind = str(data.get("type", "")).lower()
    if kind in ("sse",):
        cfg.problem = "旧的 SSE 传输不支持，请改用 Streamable HTTP（type: http）"
    elif data.get("url") or kind in ("http", "streamable-http", "streamable_http"):
        cfg.url, cfg.headers = str(data.get("url", "")), {k: str(v) for k, v in (data.get("headers") or {}).items()}
        if not cfg.url:
            cfg.problem = "http 类型缺少 url"
    elif data.get("command"):
        cfg.command = str(data["command"])
        cfg.args = [str(a) for a in data.get("args") or []]
        cfg.env = {k: str(v) for k, v in (data.get("env") or {}).items()}
        cfg.cwd = data.get("cwd")
    else:
        cfg.problem = "缺少 command（本地服务器）或 url（远程服务器）"
    if missing and not cfg.problem:
        cfg.problem = f"环境变量没设置：{'、'.join(sorted(missing))}"
    if cfg.trust not in ("", "readonly", "all"):
        cfg.problem = f"trust 只能是 readonly 或 all，现在是 {cfg.trust!r}"
    return cfg


class McpConfig:
    def __init__(self, project_root: str | Path = ".", home: str | Path | None = None, env: dict | None = None,
                 project: bool = True):
        """project=False：只读用户级配置（常驻服务里临时目录的任务没有“项目”）。"""
        self.root = Path(project_root).resolve()
        self.home = Path(home or os.environ.get("WEAVER_HOME") or "~/.weaver").expanduser()
        self._env = env                  # None：每次都读当前进程的环境变量（设置页填的令牌对已开着的任务也生效）
        self.trust_file = self.home / "trusted-mcp.json"
        # 用户级先读，项目级后读覆盖同名的
        self.files = [(self.home / "mcp.json", "user")] + ([(self.root / ".mcp.json", "project"),
                      (self.root / ".weaver" / "mcp.json", "project")] if project else [])
        self.problems: list[str] = []

    @property
    def env(self) -> dict:
        return dict(os.environ if self._env is None else self._env)

    def servers(self) -> dict[str, ServerConfig]:
        found: dict[str, ServerConfig] = {}
        self.problems = []
        env = self.env
        for f, level in self.files:
            if not f.exists():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self.problems.append(f"{f}：读不了（{e}）")
                continue
            for name, raw in (data.get("mcpServers") or {}).items():
                if isinstance(raw, dict) and not raw.get("disabled"):
                    found[name] = parse(name, raw, level, str(f), env)
        return found

    # ------------------------------------------------ 项目级信任

    def _trusted(self) -> dict:
        try:
            return json.loads(self.trust_file.read_text())
        except (OSError, ValueError):
            return {}

    def is_trusted(self, cfg: ServerConfig) -> bool:
        return cfg.level == "user" or self._trusted().get(f"{self.root}::{cfg.name}") == cfg.digest()

    def trust(self, cfgs: list[ServerConfig]) -> None:
        data = self._trusted()
        for c in cfgs:
            data[f"{self.root}::{c.name}"] = c.digest()
        self.trust_file.parent.mkdir(parents=True, exist_ok=True)
        self.trust_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def untrusted(self) -> list[ServerConfig]:
        return [c for c in self.servers().values() if not c.problem and not self.is_trusted(c)]
