"""自定义子 Agent 类型（.weaver/agents/*.md）。见 design/round2.md 第五节、design/round3.md 第二节。

一个 Markdown 文件定义一种子 Agent：frontmatter 写名字、描述、能用的工具、可选的模型；正文就是它的 system prompt。
兼容 Claude Code 的 .claude/agents/（工具名自动对应）。项目级的是别人写的指令，要先信任（和 skills 共用一次确认）。
可用的类型作为背景信息列给主 Agent，不写进 task 工具的说明（写进去会改工具清单、破坏缓存）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .context import ContextProvider
from .skills import parse_frontmatter

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_PROMPT = 20_000
# Claude Code 的工具名 → 我们的
CC_TOOLS = {"Read": "read_file", "Grep": "grep", "Glob": "find_files", "LS": "find_files", "Bash": "bash",
            "Edit": "edit_file", "MultiEdit": "edit_file", "Write": "write_file", "Skill": "skill",
            "Task": "task", "Agent": "task"}
READONLY_TOOLS = {"read_file", "grep", "find_files", "skill", "recall"}
FORBIDDEN = {"task"}                     # 深度 1：子 Agent 不能再派子 Agent


@dataclass
class AgentType:
    name: str
    description: str
    prompt: str
    path: Path
    level: str                           # project / user
    tools: list[str] | None = None       # None：general 的全部工具
    model: str = ""

    @property
    def readonly(self) -> bool:
        """工具全是只读的：可以并行、可以放后台。bash 不算只读（命令可能改东西）。"""
        return self.tools is not None and set(self.tools) <= READONLY_TOOLS


def parse_tools(value) -> list[str] | None:
    if not value or not str(value).strip():
        return None
    out = []
    for raw in re.split(r"[,\s]+", str(value).strip("[] ")):
        name = CC_TOOLS.get(raw.strip("'\""), raw.strip("'\""))
        if name and name not in out and name not in FORBIDDEN:
            out.append(name)
    return out


class AgentTypes(ContextProvider):
    kind = "agents"
    first_head = ""
    update_head = "可用的子 Agent 类型有变化，以下是最新的列表。"

    def __init__(self, project_root: str | Path = ".", home: str | Path | None = None,
                 claude_home: str | Path | None = None, trust_file: str | Path | None = None,
                 project: bool = True):
        self.root = Path(project_root).resolve()
        self.home = Path(home or os.environ.get("WEAVER_HOME") or "~/.weaver").expanduser()
        claude = Path(claude_home or "~/.claude").expanduser()
        self.dirs = ([(self.root / ".weaver" / "agents", "project"), (self.root / ".claude" / "agents", "project")]
                     if project else []) + [(self.home / "agents", "user"), (claude / "agents", "user")]
        self.trust_file = Path(trust_file) if trust_file else self.home / "trusted-agents.json"
        self.problems: list[str] = []

    def discover(self) -> dict[str, AgentType]:
        """项目级覆盖同名的用户级；同一级里 .weaver 在 .claude 前。"""
        found: dict[str, AgentType] = {}
        self.problems = []
        for d, level in self.dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                try:
                    meta, body = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
                except OSError as e:
                    self.problems.append(f"{f}：读不了（{e}）")
                    continue
                name, desc = meta.get("name", "").strip(), " ".join(meta.get("description", "").split())
                if not NAME_RE.match(name) or not desc or not body.strip():
                    self.problems.append(f"{f}：缺 name / description 或正文，跳过")
                    continue
                if name in found or name in ("explore", "general"):
                    continue
                found[name] = AgentType(name, desc, body.strip()[:MAX_PROMPT], f, level,
                                        parse_tools(meta.get("tools")), meta.get("model", "").strip())
        return found

    # ------------------------------------------------ 信任（和 skills 一样：按项目路径 + 内容哈希）

    def project_digest(self) -> str | None:
        files = sorted(f for d, level in self.dirs if level == "project" and d.is_dir() for f in d.glob("*.md"))
        if not files:
            return None
        h = hashlib.sha256()
        for f in files:
            h.update(str(f.relative_to(self.root)).encode())
            h.update(f.read_bytes())
        return h.hexdigest()[:16]

    def _trusted(self) -> dict:
        try:
            return json.loads(self.trust_file.read_text())
        except (OSError, ValueError):
            return {}

    def project_trusted(self) -> bool:
        digest = self.project_digest()
        return digest is None or self._trusted().get(str(self.root)) == digest

    def trust_project(self) -> None:
        data = self._trusted()
        data[str(self.root)] = self.project_digest()
        self.trust_file.parent.mkdir(parents=True, exist_ok=True)
        self.trust_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def available(self) -> dict[str, AgentType]:
        trusted = self.project_trusted()
        return {n: a for n, a in self.discover().items() if a.level == "user" or trusted}

    # ------------------------------------------------ 背景信息

    def render(self) -> str:
        types = self.available()
        if not types:
            return ""
        rows = [f"- {a.name}：{a.description}" + ("（只读，可以并行、可以放后台）" if a.readonly else "")
                for a in sorted(types.values(), key=lambda a: a.name)]
        return ("## 可用的子 Agent 类型\n用 task(agent=\"名字\", description, prompt) 派出，它按自己的说明和工具做事：\n"
                + "\n".join(rows))

    def digest(self, text: str = "") -> str:
        return hashlib.sha256((text or self.render()).encode()).hexdigest()[:16]


class ProjectTrust:
    """项目级的 skill、子 Agent 类型（常驻服务里还有 MCP 服务器）合在一起问一次信任。"""

    def __init__(self, skills, agents: AgentTypes, mcp=None):
        """mcp：weaver.mcp.pool.TaskMcp（有 untrusted() 和 trust(cfgs)）；命令行另外问，不传。"""
        self.skills, self.agents, self.mcp = skills, agents, mcp

    def pending(self) -> dict | None:
        """还没信任的项目级内容：{root, digest, skills: [名字], agents: [名字], mcp: [说明]}；都信任了返回 None。"""
        s_ok, a_ok = self.skills.project_trusted(), self.agents.project_trusted()
        servers = self.mcp.untrusted() if self.mcp is not None else []
        if s_ok and a_ok and not servers:
            return None
        skills = [n for n, s in self.skills.discover().items() if s.level == "project"] if not s_ok else []
        agents = [n for n, a in self.agents.discover().items() if a.level == "project"] if not a_ok else []
        digest = f"{self.skills.project_digest() or '-'}:{self.agents.project_digest() or '-'}"
        if servers:
            digest += ":" + ",".join(f"{c.name}={c.digest()}" for c in servers)
        mcp = [f"{c.name}（{' '.join([c.command] + c.args) if c.command else c.url}）"[:200] for c in servers]
        return {"root": str(self.agents.root), "digest": digest, "skills": skills, "agents": agents, "mcp": mcp}

    def trust(self) -> None:
        if not self.skills.project_trusted():
            self.skills.trust_project()
        if not self.agents.project_trusted():
            self.agents.trust_project()
        if self.mcp is not None:
            servers = self.mcp.untrusted()
            if servers:
                self.mcp.trust(servers)
