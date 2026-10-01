"""Skills 测试：发现、背景信息、skill 工具、信任、兼容 Claude Code 格式。全部离线。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from weaver.context import pending_context
from weaver.models import FakeModel, reply
from weaver.policy import Policy
from weaver.runner import Runner
from weaver.skills import MAX_DESC, Skills, parse_frontmatter
from weaver.stores import MemoryEventStore
from weaver.tools import ToolBox


def write_skill(base: Path, name: str, desc: str = "", body: str = "正文", extra: str = "", files=()):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\n" + (f"description: {desc}\n" if desc else "") + extra + "---\n"
    (d / "SKILL.md").write_text(fm + body)
    for f in files:
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_text("x")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.project, self.home, self.claude = t / "proj", t / "home", t / "claude"
        self.builtin = t / "builtin"                # 不用真的内置目录：测试只看自己造的
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def skills(self):
        return Skills(self.project, self.home, self.claude, trust_file=self.home / "trust.json",
                      builtin_dir=self.builtin)


class Parse(unittest.TestCase):
    def test_frontmatter_variants(self):
        meta, body = parse_frontmatter('---\nname: "pdf"\ndescription: >\n  处理 PDF：\n  拆分、合并\n'
                                       'allowed-tools: Read, Bash\n---\n# 正文\n')
        self.assertEqual(meta["name"], "pdf")
        self.assertEqual(meta["description"], "处理 PDF： 拆分、合并")
        self.assertEqual(body, "# 正文\n")
        self.assertEqual(parse_frontmatter("没有 frontmatter"), ({}, "没有 frontmatter"))


class Discover(Base):
    def test_dirs_precedence_and_problems(self):
        write_skill(self.home / "skills", "release", "用户级发版")
        write_skill(self.project / ".claude" / "skills", "release", "项目级发版（.claude）")
        write_skill(self.project / ".weaver" / "skills", "release", "项目级发版（.weaver）")
        write_skill(self.claude / "skills", "pdf", "处理 PDF")
        write_skill(self.home / "skills", "broken")                       # 缺 description
        write_skill(self.home / "skills", "secret", "不给模型看", extra="disable-model-invocation: true\n")
        s = self.skills()
        found = s.discover()
        self.assertEqual(found["release"].description, "项目级发版（.weaver）")   # 项目级、.weaver 优先
        self.assertEqual(found["pdf"].level, "user")
        self.assertIn("broken", s.problems[0])
        s.trust_project()
        self.assertEqual(sorted(s.available()), ["pdf", "release"])        # 隐藏的不列


class Inject(Base):
    def test_render_and_updates(self):
        write_skill(self.home / "skills", "pdf", "处理 PDF" + "很长" * 300, extra="when_to_use: 用户给了 PDF\n")
        s = self.skills()
        text = s.render()
        self.assertIn("## 可用的 skills", text)
        self.assertLessEqual(len(text.splitlines()[1]), MAX_DESC + 20)        # 描述截短
        first = pending_context([s], [])
        self.assertEqual([c["kind"] for c in first], ["skills"])
        events = [{"type": "InputReceived", "source": "context", "context_kind": "skills", "digest": first[0]["digest"]}]
        self.assertEqual(pending_context([s], events), [])                   # 没变化不追加
        write_skill(self.home / "skills", "xlsx", "处理表格")
        again = pending_context([s], events)
        self.assertIn("有变化", again[0]["text"])
        self.assertIn("xlsx", again[0]["text"])

    def test_many_skills_names_only(self):
        for i in range(105):
            write_skill(self.home / "skills", f"s{i:03d}", f"描述 {i}")
        text = self.skills().render()
        self.assertIn("只列名字", text)
        self.assertNotIn("描述 5", text)
        self.assertIn("描述 5", self.skills().load(query="s005"))

    def test_empty_adds_nothing(self):
        self.assertEqual(self.skills().render(), "")


class Load(Base):
    def test_body_files_and_source(self):
        write_skill(self.home / "skills", "release", "发版", body="1. 跑测试\n2. 打 tag\n",
                    files=["scripts/bump.sh", "references/changelog.md", ".hidden/x"])
        out = self.skills().load("release")
        self.assertIn("skill「release」的说明（来自用户目录", out)
        self.assertIn("1. 跑测试", out)
        self.assertNotIn("description:", out)                                 # 去掉 frontmatter
        self.assertIn("scripts/bump.sh", out)
        self.assertNotIn(".hidden", out)
        with self.assertRaisesRegex(ValueError, "可用的：release"):
            self.skills().load("nope")

    def test_claude_code_format_skill(self):
        write_skill(self.claude / "skills", "review", "按团队规范审查代码",
                    extra="allowed-tools: Read, Grep\nmodel: sonnet\nuser-invocable: true\n", body="# 审查规范\n")
        self.assertIn("# 审查规范", self.skills().load("review"))


class Trust(Base):
    def test_project_skills_need_trust_and_retrust_on_change(self):
        write_skill(self.project / ".claude" / "skills", "deploy", "部署")
        write_skill(self.home / "skills", "pdf", "处理 PDF")
        s = self.skills()
        self.assertFalse(s.project_trusted())
        self.assertEqual(sorted(s.available()), ["pdf"])                      # 没信任：项目级不列
        s.trust_project()
        self.assertEqual(sorted(s.available()), ["deploy", "pdf"])
        write_skill(self.project / ".claude" / "skills", "deploy", "部署（被改过）")
        self.assertFalse(self.skills().project_trusted())                     # 内容变了要重新问

    def test_no_project_skills_means_trusted(self):
        self.assertTrue(self.skills().project_trusted())


class EndToEnd(Base):
    def test_model_sees_list_and_loads(self):
        write_skill(self.home / "skills", "release", "发版流程", body="先跑 make test")
        s = self.skills()
        box = ToolBox(self.project)
        box.tools["skill"] = s.tool()
        model = FakeModel([reply(calls=[("c1", "skill", {"name": "release"})]), reply("照做")])
        r = Runner("s", MemoryEventStore(), model, box, Policy(), "SYS", context=[s])
        r.submit("帮我发个版")
        self.assertEqual(r.run().run.status, "done")
        first = model.calls[0]["messages"][0]["content"][0]["text"]
        self.assertIn("- release：发版流程", first)
        self.assertIn("先跑 make test", model.calls[1]["messages"][-1]["content"])


class Builtin(Base):
    """出厂内置的 skill（weaver/builtin_skills）：最低优先级、不用信任、weaver-when、带前缀的名字。见 design/builtin-skills.md。"""

    def test_builtin_discovered(self):
        write_skill(self.builtin, "verify", "做完前先验证")
        s = self.skills().available()
        self.assertEqual(s["verify"].level, "builtin")
        self.assertIn("来自内置", self.skills().load("verify"))

    def test_user_overrides_builtin(self):
        write_skill(self.builtin, "verify", "内置的")
        write_skill(self.claude / "skills", "verify", "用户的")
        self.assertEqual(self.skills().available()["verify"].level, "user")

    def test_project_overrides_builtin(self):
        write_skill(self.builtin, "verify", "内置的")
        write_skill(self.project / ".weaver" / "skills", "verify", "项目的")
        sk = self.skills()
        sk.trust_project()
        self.assertEqual(sk.available()["verify"].level, "project")

    def test_builtin_listed_when_project_untrusted(self):
        write_skill(self.builtin, "verify", "内置的")
        write_skill(self.project / ".claude" / "skills", "deploy", "部署")
        s = self.skills().available()
        self.assertIn("verify", s)
        self.assertNotIn("deploy", s)

    def test_when_git(self):
        write_skill(self.builtin, "tdd", "先写测试", extra="weaver-when: git\n")
        self.assertNotIn("tdd", self.skills().available())
        (self.project / ".git").mkdir()
        self.assertIn("tdd", self.skills().available())
        self.assertIn("tdd", self.skills().render())

    def test_when_git_subdir(self):
        write_skill(self.builtin, "tdd", "先写测试", extra="weaver-when: git\n")
        (self.project / ".git").write_text("gitdir: elsewhere")     # worktree / 子模块里 .git 是个文件
        sub = self.project / "docs"
        sub.mkdir()
        sk = Skills(sub, self.home, self.claude, trust_file=self.home / "trust.json", builtin_dir=self.builtin)
        self.assertIn("tdd", sk.available())

    def test_when_unknown_value(self):
        write_skill(self.builtin, "x", "随便", extra="weaver-when: 别的\n")
        self.assertIn("x", self.skills().available())

    def test_prefix_fallback(self):
        write_skill(self.claude / "skills", "pdf", "处理 PDF")
        self.assertIn("skill「pdf」", self.skills().load("superpowers:pdf"))
        with self.assertRaises(ValueError):
            self.skills().load("x:不存在")

    def test_prefix_exact_name_wins(self):
        write_skill(self.claude / "skills", "a-b", "全名", body="全名的正文")
        d = write_skill(self.claude / "skills", "b", "短名", body="短名的正文")
        (d.parent / "a-b" / "SKILL.md").write_text("---\nname: a:b\ndescription: 全名\n---\n全名的正文")
        self.assertIn("全名的正文", self.skills().load("a:b"))

    def test_body_limit_40k(self):
        write_skill(self.builtin, "big", "大", body="字" * 31_000)
        write_skill(self.builtin, "huge", "更大", body="a" * 41_000)
        self.assertNotIn("已截断", self.skills().load("big"))
        self.assertIn("已截断", self.skills().load("huge"))

    def test_main_only_hidden_from_subagents(self):
        write_skill(self.builtin, "plan", "写计划", extra="weaver-agent: main\n")
        write_skill(self.builtin, "verify", "做完前先验证")
        main = self.skills()
        sub = main.for_subagent()
        self.assertEqual(sorted(main.available()), ["plan", "verify"])
        self.assertEqual(sorted(sub.available()), ["verify"])
        self.assertNotIn("plan", sub.render())
        with self.assertRaises(ValueError):
            sub.load("plan")

    def test_git_search_stops_at_home(self):
        """~ 本身是个 dotfiles 仓库时，~ 下面的普通目录不算在 git 仓库里。"""
        home = self.project.parent
        (home / ".git").mkdir()
        write_skill(self.builtin, "tdd", "先写测试", extra="weaver-when: git\n")
        sk = Skills(self.project, self.home, self.claude, trust_file=self.home / "trust.json",
                    builtin_dir=self.builtin, user_home=home)
        self.assertNotIn("tdd", sk.available())

    def test_license_not_listed_as_attachment(self):
        write_skill(self.builtin, "verify", "做完前先验证", files=["LICENSE", "notes.md"])
        out = self.skills().load("verify")
        self.assertIn("notes.md", out)
        self.assertNotIn("LICENSE", out)


if __name__ == "__main__":
    unittest.main()
