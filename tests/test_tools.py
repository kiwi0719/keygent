"""工具测试：read_file、grep、find_files。ripgrep 和纯 Python 两种后端跑同一组测试。全部离线。"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from weaver.tools import ToolBox, read_file
from weaver.tools.search import MAX_LINE, Searcher, find_rg, match_glob, vendored_rg


def build(root: Path) -> None:
    files = {
        "src/app.py": "import os\n\ndef main():\n    print('Hello World')\n    return 0\n",
        "src/util.py": "def helper():\n    return 'hello'\n",
        "src/web/page.ts": "export const hello = 1\n",
        "src/web/page.tsx": "export const Hello = () => null\n",
        "README.md": "# Demo\nhello readme\n",
        "notes/long.txt": "hello " + "x" * 1000 + "\n",
        "build/out.py": "hello from build\n",             # .gitignore 忽略的目录
        "debug.log": "hello log\n",                        # .gitignore 忽略的文件
        "keep.log": "hello kept\n",                        # 被 !keep.log 取反
        ".git/config": "hello git\n",
        ".weaver/sessions/s.jsonl": "hello ledger\n",
        "node_modules/pkg/index.js": "hello nm\n",
        ".env.example": "HELLO=1\n",                       # 隐藏文件照样搜
        "many.txt": "".join(f"hit {i}\n" for i in range(30)),
        "regex.txt": "a.b\naxb\n(paren)\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "bin.dat").write_bytes(b"hello\0binary")
    (root / ".gitignore").write_text("# 注释\nbuild/\n*.log\n!keep.log\n")
    old = time.time() - 1000
    for rel in files:
        os.utime(root / rel, (old, old))
    os.utime(root / "src/util.py", (old + 500, old + 500))   # 最近修改


class Backends:
    """两种后端共用的测试。子类设置 rg。"""
    rg = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        build(self.root)
        self.box = ToolBox(self.root, rg=self.rg)

    def tearDown(self):
        self.tmp.cleanup()

    def grep(self, **kw):
        out, err = self.box.execute("grep", kw)
        self.assertFalse(err, out)
        return out

    def test_basic_match_and_format(self):
        out = self.grep(pattern="Hello World")
        self.assertEqual(out, "src/app.py:4:     print('Hello World')")

    def test_ignore_case_and_skips(self):
        out = self.grep(pattern="hello", ignore_case=True, limit=1000)
        files = {line.split(":")[0] for line in out.splitlines() if ":" in line}
        for want in ("src/app.py", "src/util.py", "README.md", "keep.log", ".env.example"):
            self.assertIn(want, files)
        for skip in ("build/out.py", "debug.log", ".git/config", ".weaver/sessions/s.jsonl",
                     "node_modules/pkg/index.js", "bin.dat"):
            self.assertNotIn(skip, files)

    def test_glob_filters(self):
        out = self.grep(pattern="hello", ignore_case=True, glob="*.{ts,tsx}")
        self.assertEqual(sorted(line.split(":")[0] for line in out.splitlines()),
                         ["src/web/page.ts", "src/web/page.tsx"])
        out = self.grep(pattern="hello", glob="src/**/*.py")
        self.assertEqual(out.splitlines(), ["src/util.py:2:     return 'hello'"])

    def test_literal_vs_regex(self):
        self.assertEqual(len(self.grep(pattern="a.b", path="regex.txt").splitlines()), 2)
        self.assertEqual(self.grep(pattern="a.b", path="regex.txt", literal=True), "regex.txt:1: a.b")
        self.assertEqual(self.grep(pattern="(paren)", path="regex.txt", literal=True), "regex.txt:3: (paren)")

    def test_context_lines(self):
        out = self.grep(pattern="print", path="src/app.py", context=1)
        self.assertEqual(out.splitlines(), ["src/app.py-3- def main():",
                                            "src/app.py:4:     print('Hello World')",
                                            "src/app.py-5-     return 0"])

    def test_limit_notice(self):
        out = self.grep(pattern="hit", path="many.txt", limit=5)
        lines = out.split("\n\n")[0].splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn("5 条上限", out)

    def test_long_line_clipped(self):
        out = self.grep(pattern="hello", path="notes/long.txt")
        first = out.splitlines()[0]
        self.assertLess(len(first), MAX_LINE + 40)
        self.assertIn("read_file", out)

    def test_no_match_and_errors(self):
        self.assertEqual(self.grep(pattern="zzz_nothing"), "没有找到匹配")
        out, err = self.box.execute("grep", {"pattern": "(unclosed"})
        self.assertTrue(err)
        self.assertIn("literal=true", out)
        out, err = self.box.execute("grep", {"pattern": "x", "path": "nope/"})
        self.assertTrue(err)
        self.assertIn("路径不存在", out)

    def test_find_files_sorted_by_mtime(self):
        out, err = self.box.execute("find_files", {"pattern": "*.py"})
        self.assertFalse(err, out)
        self.assertEqual(out.splitlines(), ["src/util.py", "src/app.py"])       # build/ 被忽略
        out, _ = self.box.execute("find_files", {"pattern": "src/**/*.{ts,tsx}"})
        self.assertEqual(sorted(out.splitlines()), ["src/web/page.ts", "src/web/page.tsx"])
        out, _ = self.box.execute("find_files", {"pattern": "*", "limit": 3})
        self.assertIn("只显示最近修改的 3 个", out)
        out, _ = self.box.execute("find_files", {"pattern": "*.nothing"})
        self.assertEqual(out, "没有找到文件")

    def test_read_file_relative_to_root(self):
        out, err = self.box.execute("read_file", {"path": "src/app.py", "offset": 4, "limit": 1})
        self.assertFalse(err, out)
        self.assertIn("print('Hello World')", out)
        self.assertIn("offset=5", out)


class PythonBackend(Backends, unittest.TestCase):
    rg = None


@unittest.skipUnless(find_rg(), "没有可用的 ripgrep")
class RipgrepBackend(Backends, unittest.TestCase):
    rg = find_rg()


class Helpers(unittest.TestCase):
    def test_match_glob(self):
        self.assertTrue(match_glob("a/b/c.py", "*.py"))
        self.assertTrue(match_glob("src/x.ts", "src/**/*.ts"))
        self.assertTrue(match_glob("src/a/b/x.tsx", "src/**/*.{ts,tsx}"))
        self.assertFalse(match_glob("lib/x.ts", "src/**/*.ts"))

    def test_vendored_rg_on_this_machine(self):
        import platform
        if (platform.system(), platform.machine()) in (("Darwin", "arm64"), ("Linux", "x86_64")):
            self.assertIsNotNone(vendored_rg())
            self.assertIn("vendor/ripgrep", vendored_rg())

    def test_env_override(self):
        old = os.environ.get("WEAVER_RG")
        try:
            os.environ["WEAVER_RG"] = "/definitely/not/here/rg"
            self.assertIsNone(find_rg())
        finally:
            if old is None:
                os.environ.pop("WEAVER_RG")
            else:
                os.environ["WEAVER_RG"] = old

    def test_read_file_function(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text("1\n2\n3\n")
            self.assertIn("2", read_file(str(p), offset=2, limit=1))

    def test_specs_and_readonly(self):
        box = ToolBox()
        self.assertEqual([s["name"] for s in box.specs], ["read_file", "grep", "find_files"])
        self.assertTrue(all(box.is_readonly(n) for n in ("read_file", "grep", "find_files")))
        self.assertFalse(box.is_readonly("nope"))

    def test_searcher_paths_outside_root(self):
        s = Searcher("/tmp")
        self.assertEqual(s.rel("/etc/hosts"), "/etc/hosts")


if __name__ == "__main__":
    unittest.main()
