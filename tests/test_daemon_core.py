"""常驻服务的纯函数部分：任务存储、步骤翻译、状态推导。全部离线。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from weaver.daemon.humanize import steps, title
from weaver.daemon.status import summarize
from weaver.daemon.tasks import LEDGER, TaskStore, default_title
from weaver.models import FakeModel, reply
from weaver.permissions import PermissionPolicy
from weaver.policy import Policy, WaitSpec
from weaver.runner import Runner
from weaver.stores import MemoryEventStore
from weaver.tools import Tool, ToolBox, _spec


def box(delays=None):
    import time as _t

    def read(path="", **_):
        _t.sleep((delays or {}).get(path, 0))
        return f"内容 {path}"
    b = ToolBox(tools={})
    b.tools["read_file"] = Tool(_spec("read_file", "", {"path": {"type": "string"}}, ["path"]), read)
    b.tools["bash"] = Tool(_spec("bash", "", {"command": {"type": "string"}}, ["command"]),
                           lambda command="": "ok", readonly=False)
    return b


def run(script, policy=None, approver=None, tools=None, store=None, text="帮我看看"):
    store = store or MemoryEventStore()
    r = Runner("s", store, FakeModel(script), tools or box(), policy or Policy(), "SYS", approver=approver)
    r.submit(text)
    r.run()
    return r, store.load("s")


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.ts = TaskStore(t / "tasks", scratch=t / "scratch")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_get_title_list_archive(self):
        a = self.ts.create("帮我整理一下这个季度的客户流失数据并给出分析结论")
        self.assertEqual(a.title, "帮我整理一下这个季度的客户流失数据并给出…")
        self.assertTrue(Path(a.workdir).is_dir())                      # 默认给一个空的 scratch 目录
        self.assertEqual(self.ts.get(a.id), a)
        self.assertEqual(list(self.ts.dir(a.id).glob("*.tmp-*")), [])   # 原子写，没有残留
        b = self.ts.create("第二个", workdir=self.tmp.name)
        self.assertEqual(b.workdir, str(Path(self.tmp.name).resolve()))
        with self.assertRaisesRegex(ValueError, "工作目录不存在"):
            self.ts.create("x", workdir="/definitely/not/here")
        time.sleep(0.01)
        self.ts.store(a.id).append(LEDGER, [{"type": "X"}], 0)          # a 有了新动静，排到前面
        self.assertEqual([m.id for m in self.ts.list()], [a.id, b.id])
        self.ts.set_title(a.id, "  Q3 流失分析  ")
        self.assertEqual(self.ts.get(a.id).title, "Q3 流失分析")
        dest = self.ts.archive(a.id)
        self.assertTrue((dest / "meta.json").exists())
        self.assertEqual([m.id for m in self.ts.list()], [b.id])
        self.assertEqual(default_title(""), "未命名任务")


class Titles(unittest.TestCase):
    def test_templates(self):
        cases = [
            ("read_file", {"path": "calc.py", "offset": 1, "limit": 40}, "读 calc.py 第 1–40 行"),
            ("read_file", {"path": "a.md"}, "读 a.md"),
            ("grep", {"pattern": "def main", "glob": "*.py"}, '搜索 "def main"（*.py）'),
            ("bash", {"command": "npm test"}, "跑命令 npm test"),
            ("bash", {"command": "npm run dev", "background": True}, "跑命令 npm run dev（后台）"),
            ("task", {"description": "查错误处理"}, "派子 Agent：查错误处理"),
            ("task", {"description": "改", "mode": "general"}, "派子 Agent：改（可改文件）"),
            ("todo_write", {"todos": [{"status": "completed"}, {"status": "pending"}]}, "更新任务清单（1/2）"),
            ("mcp__fs__read_text_file", {}, "MCP fs：read_text_file"),
            ("mcp_call", {"server": "gh", "tool": "create_issue"}, "MCP gh：create_issue"),
            ("weird_tool", {}, "调用 weird_tool"),
        ]
        for name, args, want in cases:
            self.assertEqual(title(name, args), want, name)


class Steps(unittest.TestCase):
    def test_basic_flow_with_why(self):
        _, ev = run([reply("我先读一下", calls=[("c1", "read_file", {"path": "a.py"})]), reply("看完了")])
        st = steps(ev)
        self.assertEqual([s["kind"] for s in st], ["you", "agent", "step", "agent"])
        self.assertEqual(st[0]["text"], "帮我看看")
        self.assertEqual((st[2]["title"], st[2]["out"], st[2]["why"], st[2]["status"]),
                         ("读 a.py", "内容 a.py", "我先读一下", "ok"))

    def test_parallel_results_in_call_order(self):
        _, ev = run([reply(calls=[("c1", "read_file", {"path": "slow"}), ("c2", "read_file", {"path": "fast"})]),
                     reply("好")], tools=box({"slow": 0.2}))
        done = [e["action_id"] for e in ev if e["type"] == "ActionCompleted" and e["kind"] == "tool"]
        self.assertEqual(done, ["c2", "c1"])                                  # 账本里是乱序的
        self.assertEqual([s["title"] for s in steps(ev) if s["kind"] == "step"], ["读 slow", "读 fast"])

    def test_statuses_denied_cancelled_waiting(self):
        _, ev = run([reply(calls=[("c1", "bash", {"command": "sudo rm -rf /x"})]), reply("好吧")],
                    policy=PermissionPolicy(".", yes=True))
        self.assertEqual(steps(ev)[1]["status"], "denied")

        class Ask(Policy):
            def wait_for(self, call, state):
                return WaitSpec("approval", "危险")
        r, ev = run([reply(calls=[("c1", "bash", {"command": "rm a"})])], policy=Ask())
        self.assertEqual(steps(ev)[1]["status"], "waiting")
        r.cancel("不要了")
        r.run()
        self.assertEqual(steps(r.store.load("s"))[1]["status"], "cancelled")

    def test_hidden_and_notes(self):
        from tests.test_todo_loop import call, run as run_todo
        ev = run_todo([reply(calls=[call(i, x="same")]) for i in range(6)])[2].load("s")
        st = steps(ev)
        self.assertTrue(any(s["title"].startswith("Weaver 提醒") for s in st))
        self.assertFalse(any("环境" in s["title"] for s in st))
        ctx = [{"type": "InputReceived", "id": "x", "seq": 1, "source": "context", "content": [{"type": "text", "text": "记忆"}]}]
        self.assertEqual(steps(ctx), [])                                      # 背景信息不进步骤

    def test_model_error_and_compact(self):
        def boom(_):
            raise RuntimeError("网络挂了")
        _, ev = run([boom])
        self.assertEqual((steps(ev)[-1]["title"], steps(ev)[-1]["status"]), ("模型调用失败", "error"))
        compact = {"type": "ActionCompleted", "kind": "compact", "seq": 99, "action_id": "z",
                   "output": {"mode": "summary", "tokens_before": 9000, "tokens_after": 3000}}
        self.assertEqual(steps([compact])[0]["title"], "上下文压缩（写摘要）")


class Status(unittest.TestCase):
    def test_done_running_idle(self):
        _, ev = run([reply("结论：一切正常\n细节……")])
        self.assertEqual({k: v for k, v in summarize(ev).items() if k in ("status", "now")},
                         {"status": "done", "now": "结论：一切正常"})
        cut = next(i for i, e in enumerate(ev) if e["type"] == "ActionStarted") + 1
        self.assertEqual(summarize(ev[:cut])["status"], "running")
        self.assertEqual(summarize([])["status"], "idle")
        r, ev = run([reply("## 结果是 **5050**\n解释")])
        self.assertEqual(summarize(ev)["now"], "结果是 5050")
        # 新输入还没被内核接住（一轮开始前在连 MCP）：已经算“在跑”，不显示成上一轮的“完成”或“空闲”
        first = next(i for i, e in enumerate(ev) if e["type"] == "InputJudged")
        self.assertEqual(summarize(ev[:first])["status"], "running")
        r.submit("再来")
        self.assertEqual((summarize(r.store.load("s"))["status"], summarize(r.store.load("s"))["now"]),
                         ("running", "准备开始"))

    def test_running_now_is_current_tool(self):
        _, ev = run([reply(calls=[("c1", "read_file", {"path": "big.csv"})]), reply("好")])
        cut = next(i for i, e in enumerate(ev) if e["type"] == "ActionStarted" and e["kind"] == "tool") + 1
        self.assertEqual(summarize(ev[:cut])["now"], "读 big.csv")

    def test_waiting_approval_choices(self):
        class Ask(Policy):
            def wait_for(self, call, state):
                return WaitSpec("approval", "要执行命令")
        _, ev = run([reply(calls=[("c1", "bash", {"command": "npm publish"})])], policy=Ask())
        s = summarize(ev)
        self.assertEqual((s["status"], s["now"]), ("waiting", "跑命令 npm publish（等你放行）"))
        w = s["waiting"][0]
        self.assertEqual((w["kind"], w["title"], w["body"]), ("approval", "跑命令 npm publish", "要执行命令"))
        self.assertEqual(w["choices"], ["allow", "deny", "always", "edit"])
        self.assertEqual(w["call"]["args"], {"command": "npm publish"})

    def test_stuck_wait_and_error_notes(self):
        from tests.test_todo_loop import call, run as run_todo
        _, _, store, _ = run_todo([reply(calls=[call(i, x="same")]) for i in range(5)], Policy(interactive=True))
        s = summarize(store.load("s"))
        self.assertEqual((s["status"], s["waiting"][0]["kind"]), ("waiting", "stuck"))
        self.assertEqual(s["waiting"][0]["choices"], ["continue", "stop", "hint"])
        _, _, store, _ = run_todo([reply(calls=[call(i, x="same")]) for i in range(6)])
        self.assertEqual((summarize(store.load("s"))["status"], summarize(store.load("s"))["note"]),
                         ("error", "卡住了，已停下"))
        _, ev = run([reply(calls=[("c1", "read_file", {"path": "a"})]), reply("收尾")], policy=Policy(max_steps=1))
        s = summarize(ev)
        self.assertEqual((s["status"], s["note"], s["final"]), ("error", "预算用完，已收尾", "收尾"))

    def test_cancelled_and_model_error(self):
        class Ask(Policy):
            def wait_for(self, call, state):
                return WaitSpec("approval", "")
        r, _ = run([reply(calls=[("c1", "bash", {"command": "x"})])], policy=Ask())
        r.cancel("")
        r.run()
        self.assertEqual(summarize(r.store.load("s"))["status"], "cancelled")

        def boom(_):
            raise RuntimeError("HTTP 500: 服务器炸了\n详情")
        _, ev = run([boom])
        s = summarize(ev)
        self.assertEqual((s["status"], s["note"]), ("error", "RuntimeError: HTTP 500: 服务器炸了"))


if __name__ == "__main__":
    unittest.main()
