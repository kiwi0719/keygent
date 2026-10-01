"""出厂内置的 skill（weaver/builtin_skills）：哪些、什么时候列出、改过的内容在不在。见 design/builtin-skills.md。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from weaver.skills import BUILTIN_DIR, MAX_BODY, MAX_FILES, Skills, parse_frontmatter

ALWAYS = {"verification-before-completion", "systematic-debugging"}
IN_GIT = {"brainstorming", "writing-plans", "executing-plans", "subagent-driven-development",
          "test-driven-development", "using-git-worktrees", "requesting-code-review",
          "finishing-a-development-branch"}


def texts():
    for f in sorted(BUILTIN_DIR.rglob("*")):
        if f.is_file() and f.name != "NOTICE.md":         # NOTICE 里记着删了什么，会提到这些词
            yield f, f.read_text(encoding="utf-8", errors="replace")


class Content(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.sk = Skills(t, t / "home", t / "claude", trust_file=t / "trust.json")   # 只有内置的

    def tearDown(self):
        self.tmp.cleanup()

    def builtin(self):
        return {n: s for n, s in self.sk.discover().items() if s.level == "builtin"}

    def test_ten_skills(self):
        self.assertEqual(set(self.builtin()), ALWAYS | IN_GIT)

    def test_when_flags(self):
        b = self.builtin()
        self.assertEqual({n for n, s in b.items() if s.when == "git"}, IN_GIT)
        self.assertEqual({n for n, s in b.items() if s.when == ""}, ALWAYS)

    def test_no_model_selection(self):
        for f, text in texts():
            for banned in ("Model Selection", "most capable", "more capable", "mid-tier", "model: [MODEL", "[MODEL]"):
                self.assertNotIn(banned, text, f"{f.relative_to(BUILTIN_DIR)} 里还有 {banned!r}")

    def test_weaver_notes(self):
        for name in ("subagent-driven-development", "executing-plans", "requesting-code-review"):
            body = (BUILTIN_DIR / name / "SKILL.md").read_text()
            self.assertIn('task(mode="general")', body, name)
            self.assertIn("ask_user", body, name)
        self.assertNotIn("using-superpowers/references", (BUILTIN_DIR / "executing-plans" / "SKILL.md").read_text())

    def test_dev_artifacts_removed(self):
        d = BUILTIN_DIR / "systematic-debugging"
        self.assertFalse((d / "CREATION-LOG.md").exists())
        self.assertFalse((d / "test-academic.md").exists())
        self.assertEqual(list(d.glob("test-pressure-*.md")), [])

    def test_license_and_notice(self):
        for name in ALWAYS | IN_GIT:
            self.assertTrue((BUILTIN_DIR / name / "LICENSE").is_file(), name)
        self.assertIn("8ca22db", (BUILTIN_DIR / "NOTICE.md").read_text())

    def test_fits_limits(self):
        for name in ALWAYS | IN_GIT:
            d = BUILTIN_DIR / name
            meta, body = parse_frontmatter((d / "SKILL.md").read_text())
            self.assertTrue(meta.get("name") == name and meta.get("description"), name)
            self.assertLess(len(body.strip()), MAX_BODY, name)
            files = [p for p in d.rglob("*") if p.is_file() and p.name != "SKILL.md"]
            self.assertLessEqual(len(files), MAX_FILES, name)

    def test_main_only_flags(self):
        main_only = {"brainstorming", "writing-plans", "executing-plans", "subagent-driven-development",
                     "using-git-worktrees", "finishing-a-development-branch"}
        self.assertEqual({n for n, s in self.builtin().items() if s.main_only}, main_only)

    def test_listing_outside_git(self):
        """普通目录里只列总是可用的两个。"""
        self.assertEqual({n for n, s in self.sk.available().items() if s.level == "builtin"}, ALWAYS)


if __name__ == "__main__":
    unittest.main()
