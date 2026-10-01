"""Skills：按需加载的说明书。见 design/skills.md。

调度拆成四步，挂在已有的环节上：
  ① 发现：扫描各 skills 目录的 SKILL.md（兼容 Claude Code 的 .claude/skills）
  ② 注入：名字 + 描述作为背景信息（context 机制，不碰 system prompt 和工具清单）
  ③ 选择：模型自己决定
  ④ 加载：skill 工具返回正文和附带文件的路径
项目级 skill 是别人写的指令，要先信任一次（按项目路径 + 内容哈希记住，内容变了重新问）。
出厂内置的在 weaver/builtin_skills，优先级最低、不用信任（design/builtin-skills.md）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .context import ContextProvider
from .tools import Tool, _spec

BUILTIN_DIR = Path(__file__).parent / "builtin_skills"
MAX_DESC = 300
MAX_LISTED = 100           # 超过这么多只列名字
MAX_BODY = 40_000           # subagent-driven-development 正文 30KB 出头
MAX_FILES = 20


@dataclass
class Skill:
    name: str
    description: str
    path: Path               # SKILL.md 所在目录
    level: str               # project / user / builtin
    when_to_use: str = ""
    hidden: bool = False     # disable-model-invocation
    when: str = ""           # weaver-when：git = 只在 git 仓库里列出
    main_only: bool = False  # weaver-agent: main = 只给主 Agent（流程类：要问人、要派子 Agent）
    meta: dict = field(default_factory=dict)

    @property
    def file(self) -> Path:
        return self.path / "SKILL.md"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """只支持 SKILL.md 里常见的 YAML 写法：key: value、引号、> 和 | 多行块。返回 (字段, 正文)。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return {}, text
    meta, lines, i = {}, m.group(1).splitlines(), 0
    while i < len(lines):
        line = lines[i]
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        i += 1
        if not km:
            continue
        key, value = km.group(1), km.group(2).strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            block = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            value = ("\n" if value.startswith("|") else " ").join(b for b in block).strip()
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key] = value
    return meta, text[m.end():]


class Skills(ContextProvider):
    kind = "skills"
    first_head = ""
    update_head = "可用的 skills 有变化，以下是最新的列表。"

    def __init__(self, project_root: str | Path = ".", home: str | Path | None = None,
                 claude_home: str | Path | None = None, trust_file: str | Path | None = None,
                 builtin_dir: str | Path | None = None, user_home: str | Path | None = None):
        self.root = Path(project_root).resolve()
        self.home = Path(home or os.environ.get("WEAVER_HOME") or "~/.weaver").expanduser()
        claude = Path(claude_home or "~/.claude").expanduser()
        self.dirs = [(self.root / ".weaver" / "skills", "project"), (self.root / ".claude" / "skills", "project"),
                     (self.home / "skills", "user"), (claude / "skills", "user"),
                     (Path(builtin_dir) if builtin_dir else BUILTIN_DIR, "builtin")]
        self.trust_file = Path(trust_file) if trust_file else self.home / "trusted-skills.json"
        self.user_home = Path(user_home or Path.home()).resolve()
        self.sub = False                    # 给子 Agent 用的视图：不列 weaver-agent: main 的
        self.problems: list[str] = []

    # ------------------------------------------------ ① 发现

    def discover(self) -> dict[str, Skill]:
        """项目级覆盖同名的用户级；同一级里先找到的（.weaver 在 .claude 前）为准。"""
        found: dict[str, Skill] = {}
        self.problems = []
        for d, level in self.dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*/SKILL.md")):
                try:
                    meta, _ = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
                except OSError as e:
                    self.problems.append(f"{f}：读不了（{e}）")
                    continue
                name, desc = meta.get("name", "").strip(), " ".join(meta.get("description", "").split())
                if not name or not desc:
                    self.problems.append(f"{f}：frontmatter 缺 name 或 description，跳过")
                    continue
                if name in found:
                    continue
                found[name] = Skill(name, desc, f.parent, level, " ".join(meta.get("when_to_use", "").split()),
                                    str(meta.get("disable-model-invocation", "")).lower() == "true",
                                    meta.get("weaver-when", "").strip().lower(),
                                    meta.get("weaver-agent", "").strip().lower() == "main", meta)
        return found

    # ------------------------------------------------ 信任

    def project_digest(self) -> str | None:
        """项目级所有 SKILL.md 的内容哈希；没有项目级 skill 时返回 None。"""
        files = sorted(f for d, level in self.dirs if level == "project" and d.is_dir() for f in d.glob("*/SKILL.md"))
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

    def for_subagent(self) -> "Skills":
        """子 Agent 用的视图：同样的目录和信任，但不列只给主 Agent 的流程类 skill。"""
        view = copy.copy(self)
        view.sub = True
        return view

    def in_git(self) -> bool:
        """项目根（或它的上级）是不是 git 仓库；.git 是文件也算（worktree、子模块）。
        查到用户主目录为止、不含主目录本身：~ 是 dotfiles 仓库时，~ 下面的普通目录不算。"""
        for d in (self.root, *self.root.parents):
            if d == self.user_home or d in self.user_home.parents:
                return False
            if (d / ".git").exists():
                return True
        return False

    def available(self) -> dict[str, Skill]:
        """给模型用的：去掉隐藏的；项目级没被信任就整个不要；weaver-when: git 的只在 git 仓库里给。"""
        trusted = self.project_trusted()
        git = None
        out = {}
        for n, s in self.discover().items():
            if s.hidden or (s.level == "project" and not trusted) or (self.sub and s.main_only):
                continue
            if s.when == "git":
                git = self.in_git() if git is None else git
                if not git:
                    continue
            out[n] = s
        return out

    # ------------------------------------------------ ② 注入（背景提供者）

    def render(self) -> str:
        skills = self.available()
        if not skills:
            return ""
        lines = ["## 可用的 skills（需要时用 skill 工具加载全文）"]
        if len(skills) > MAX_LISTED:
            lines.append(f"共 {len(skills)} 个，只列名字；用 skill(query=…) 按关键字查描述。")
            lines.append("、".join(sorted(skills)))
        else:
            for s in sorted(skills.values(), key=lambda s: s.name):
                desc = s.description + (f" 适用：{s.when_to_use}" if s.when_to_use else "")
                if len(desc) > MAX_DESC:
                    desc = desc[:MAX_DESC] + "…"
                lines.append(f"- {s.name}：{desc}")
        return "\n".join(lines)

    # ------------------------------------------------ ④ 加载（工具）

    def _display(self, p: Path) -> str:
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return str(p).replace(str(Path.home()), "~", 1)

    def load(self, name: str = "", query: str = "") -> str:
        skills = self.available()
        if query and not name:
            q = query.lower()
            hits = [s for s in skills.values() if q in s.name.lower() or q in s.description.lower()]
            if not hits:
                return f"没有和“{query}”相关的 skill。"
            return "\n".join(f"- {s.name}：{s.description[:MAX_DESC]}" for s in sorted(hits, key=lambda s: s.name))
        s = skills.get(name)
        if s is None and ":" in name:               # superpowers:tdd 这种带命名空间的引用：去掉前缀再找
            s = skills.get(name.rsplit(":", 1)[1])
        if s is None:
            raise ValueError(f"没有名为 {name} 的 skill。可用的：{'、'.join(sorted(skills)) or '（没有）'}")
        _, body = parse_frontmatter(s.file.read_text(encoding="utf-8", errors="replace"))
        body = body.strip()
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY] + f"\n…[正文超过 {MAX_BODY // 1000}KB 已截断，完整内容见 {self._display(s.file)}]"
        files = sorted(p for p in s.path.rglob("*") if p.is_file() and p.name != "SKILL.md"
                       and not any(part.startswith(".") for part in p.relative_to(s.path).parts)
                       and not p.name.startswith("LICENSE"))
        where = {"project": "项目目录", "user": "用户目录", "builtin": "内置"}[s.level]
        out = [f"以下是 skill「{s.name}」的说明（来自{where} {self._display(s.path)}/）。按它的步骤做；"
               "里面提到的脚本和文件用 read_file 读、用 bash 跑。", "", body]
        if files:
            out += ["", f"## 附带的文件（{len(files)} 个{'，只列前 ' + str(MAX_FILES) + ' 个' if len(files) > MAX_FILES else ''}）"]
            out += [f"- {self._display(p)}" for p in files[:MAX_FILES]]
        return "\n".join(out)

    def tool(self) -> Tool:
        return Tool(_spec(
            "skill",
            "加载一个 skill（按需读入的说明书）的全文和附带文件列表。可用的 skill 列在对话开头的背景信息里；"
            "任务和某个 skill 的描述对得上时，先加载它再照着做。也可以用 query 按关键字查找。",
            {"name": {"type": "string", "description": "skill 名"},
             "query": {"type": "string", "description": "按关键字查找 skill（不知道名字时用）"}},
            []),
            self.load, readonly=True)
