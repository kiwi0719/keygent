"""任务管理器：醒来 / 睡着、并发上限、插话、回答等待、取消、归档、启动时恢复、执行者崩溃。全部离线。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from weaver import kernel as k
from weaver.daemon.manager import BadRequest, Conflict, NotFound, TaskManager
from weaver.daemon.tasks import TaskStore
from weaver.models import FakeModel, reply
from weaver.policy import Policy, WaitSpec
from weaver.runner import Runner
from weaver.stores import JsonlEventStore
from weaver.tools import Tool, ToolBox, _spec

T = 5


def box():
    b = ToolBox(tools={})
    b.tools["bash"] = Tool(_spec("bash", "", {"command": {"type": "string"}}, ["command"]),
                           lambda command="": f"ran {command}", readonly=False)
    return b


class Ask(Policy):
    """bash 一律要审批，交互模式（卡住了问人）。"""
    def __init__(self, **kw):
        super().__init__(interactive=True, **kw)

    def wait_for(self, call, state):
        return WaitSpec("approval", "要跑命令") if call["name"] == "bash" else None


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ts = TaskStore(root / "tasks", scratch=root / "scratch")
        self.scripts: dict[str, list] = {}           # 按任务的第一句话分剧本
        self.next_script: list = []
        self.runner_cls = Runner
        self.policy = None
        self.mgr = self.make()
        self.seen: list[tuple] = []
        self.mgr.subscribe(lambda kind, task, data: self.seen.append((kind, task, data)))

    def make(self, max_running=4):
        def factory(meta, store, sink, on_delta):
            script = self.scripts.pop(meta.title, None) or self.next_script
            return self.runner_cls("ledger", store, FakeModel(script), box(), self.policy or Ask(), "SYS",
                                   sink=sink, on_delta=on_delta)
        return TaskManager(self.ts, factory, max_running=max_running)

    def tearDown(self):
        self.mgr.close(wait=True)
        self.tmp.cleanup()

    def done(self, task_id=None):
        self.assertTrue(self.mgr.wait_idle(task_id, timeout=T), "任务没在限定时间内停下")


def blocking(gate: threading.Event, text="好", entered: threading.Event | None = None):
    def step(_):
        if entered:
            entered.set()
        gate.wait(T)
        return reply(text)
    return step


class Basics(Harness):
    def test_create_runs_to_done_and_notifies(self):
        self.scripts["你好"] = [reply("结论：一切正常")]
        t = self.mgr.create("你好")
        self.done()
        s = self.mgr.summary(t["id"])
        self.assertEqual((s["status"], s["now"]), ("done", "结论：一切正常"))
        kinds = {k for k, task, _ in self.seen if task == t["id"]}
        self.assertTrue({"ledger", "delta", "task"} <= kinds)
        self.assertEqual(self.seen[-1][2]["status"], "done")          # 最后一条摘要是完成

    def test_bad_requests(self):
        with self.assertRaises(BadRequest):
            self.mgr.create("  ")
        with self.assertRaises(BadRequest):
            self.mgr.create("x", workdir="/definitely/not/here")
        with self.assertRaises(NotFound):
            self.mgr.input("nope", "hi")
        self.assertEqual(self.mgr.list(), [])

    def test_factory_failure_leaves_no_task(self):
        def broken(*_):
            raise ValueError("请在 .env 里设置 WEAVER_PROVIDER")
        mgr = TaskManager(self.ts, broken)
        with self.assertRaisesRegex(ValueError, "WEAVER_PROVIDER"):
            mgr.create("x")
        self.assertEqual(self.ts.list(), [])
        mgr.close()


class Concurrency(Harness):
    def test_limit_and_queue(self):
        self.mgr.close(wait=True)
        self.mgr = self.make(max_running=2)
        gate = threading.Event()
        for i in range(4):
            self.scripts[f"任务{i}"] = [blocking(gate, f"完成{i}")]
        ids = [self.mgr.create(f"任务{i}")["id"] for i in range(4)]
        st = sorted(self.mgr.summary(i)["status"] for i in ids)
        self.assertEqual(st.count("queued"), 2, st)
        self.assertEqual(self.mgr.status()["queued"], 2)
        self.assertEqual(self.mgr.summary(ids[3])["now"], "排队中")
        gate.set()
        self.done()
        self.assertEqual([self.mgr.summary(i)["status"] for i in ids], ["done"] * 4)

    def test_input_while_running_is_picked_up(self):
        gate = threading.Event()
        self.scripts["先做这个"] = [blocking(gate, "第一件做完"), reply("第二件也做完")]
        t = self.mgr.create("先做这个")["id"]
        self.mgr.input(t, "顺便再做那个")                # 在跑的时候插话：只记 dirty，不重复排队
        gate.set()
        self.done()
        s = self.mgr.summary(t)
        self.assertEqual(s["status"], "done")
        user = [e for e in self.mgr.events(t) if e["type"] == "InputReceived" and e["source"] == "user"]
        self.assertEqual(len(user), 2)

    def test_concurrent_writes_keep_ledger_consistent(self):
        gate = threading.Event()
        self.next_script = [blocking(gate)] + [reply("好")] * 20
        t = self.mgr.create("并发")["id"]
        threads = [threading.Thread(target=self.mgr.input, args=(t, f"插话{i}")) for i in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        gate.set()
        self.done()
        seqs = [e["seq"] for e in JsonlEventStore(self.ts.dir(t)).load("ledger")]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))          # 新开一个存储从文件重读：连续、无重复
        self.assertEqual(self.mgr.summary(t)["status"], "done")


class Waits(Harness):
    def approve_case(self):
        self.next_script = [reply("我要发布", calls=[("c1", "bash", {"command": "npm publish --tag beta"})]),
                            reply("发布完了")]
        t = self.mgr.create("发布")["id"]
        self.done()
        return t

    def test_allow(self):
        t = self.approve_case()
        self.assertEqual(self.mgr.summary(t)["status"], "waiting")
        ws = self.mgr.waits()
        self.assertEqual((len(ws), ws[0]["task"], ws[0]["task_title"]), (1, t, "发布"))
        self.assertEqual(self.mgr.status()["first_wait"]["title"], "跑命令 npm publish --tag beta")
        with self.assertRaises(BadRequest):
            self.mgr.answer(ws[0]["id"], "continue")                  # 不在 choices 里
        with self.assertRaisesRegex(BadRequest, "只能配 allow"):
            self.mgr.answer(ws[0]["id"], "deny", args={"command": "npm publish --dry-run"})
        self.mgr.answer(ws[0]["id"], "allow")
        self.done()
        self.assertEqual(self.mgr.summary(t)["status"], "done")
        with self.assertRaises(Conflict):
            self.mgr.answer(ws[0]["id"], "allow")                     # 已经回答过
        with self.assertRaises(NotFound):
            self.mgr.answer("nope", "allow")
        out = [e for e in self.mgr.events(t) if e["type"] == "ActionCompleted" and e["kind"] == "tool"]
        self.assertEqual(out[0]["output"], "ran npm publish --tag beta")

    def test_deny_and_always(self):
        t = self.approve_case()
        w = self.mgr.waits()[0]
        self.mgr.answer(w["id"], "always")
        self.done()
        resolved = [e for e in self.mgr.events(t) if e["type"] == "WaitResolved"][0]
        self.assertEqual(resolved["value"], {"allow": True, "always": "npm publish"})

        t2 = self.approve_case()
        w = [x for x in self.mgr.waits() if x["task"] == t2][0]
        self.mgr.answer(w["id"], "deny", note="先别发")
        self.done()
        steps = self.mgr.detail(t2)["steps"]
        self.assertEqual([s["status"] for s in steps if s["kind"] == "step"], ["denied"])
        self.assertIn("先别发", [e for e in self.mgr.events(t2) if e["type"] == "ActionCompleted"
                                 and e["kind"] == "tool"][0]["output"])

    def test_edit_then_allow(self):
        t = self.approve_case()
        w = self.mgr.waits()[0]
        self.mgr.answer(w["id"], "allow", args={"command": "npm publish --dry-run"})
        self.done()
        ev = self.mgr.events(t)
        started = [e for e in ev if e["type"] == "ActionStarted" and e["kind"] == "tool"][0]
        self.assertEqual((started["input"]["args"], started["input"]["edited_from"]),
                         ({"command": "npm publish --dry-run"}, {"command": "npm publish --tag beta"}))
        out = [e for e in ev if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]["output"]
        self.assertEqual(out, '[用户把参数改成了：{"command": "npm publish --dry-run"}]\nran npm publish --dry-run')
        step = [s for s in self.mgr.detail(t)["steps"] if s["kind"] == "step"][0]
        self.assertEqual((step["title"], step["status"]), ("跑命令 npm publish --dry-run（你改过参数）", "ok"))
        self.assertEqual(self.mgr.summary(t)["status"], "done")

    def test_edit_still_hits_hard_deny(self):
        from weaver.permissions import PermissionPolicy
        self.next_script = [reply(calls=[("c1", "bash", {"command": "npm publish"})]), reply("好吧")]
        self.policy = PermissionPolicy(self.tmp.name, interactive=True)
        t = self.mgr.create("发布")["id"]
        self.done()
        w = self.mgr.waits()[0]
        self.mgr.answer(w["id"], "allow", args={"command": "sudo rm -rf /"})
        self.done()
        out = [e for e in self.mgr.events(t) if e["type"] == "ActionCompleted" and e["kind"] == "tool"][0]
        self.assertTrue(out["synthetic"])
        self.assertIn("用户改过的参数被权限规则拒绝", out["output"])

    def test_cancel_while_waiting(self):
        t = self.approve_case()
        self.mgr.cancel(t)
        self.done()
        s = self.mgr.summary(t)
        self.assertEqual((s["status"], s["waiting"]), ("cancelled", 0))
        self.assertEqual(self.mgr.waits(), [])
        self.mgr.cancel(t)                                             # 再取消一次：什么都不做
        self.assertEqual(sum(e["type"] == "Cancelled" for e in self.mgr.events(t)), 1)


    def test_answer_right_when_worker_goes_to_sleep(self):
        """工作线程宣布“停下了”的那一刻立刻放行：这次叫醒不能丢（压测里撞到过）。"""
        self.next_script = [reply(calls=[("c1", "bash", {"command": "ls"})]), reply("好了")]
        answered, seen = [], []

        def on(kind, task, data):
            if kind == "task" and data["status"] == "waiting":
                seen.append(task)
                # 第一条是执行途中（记下等待时）发的；第二条是工作线程收尾、准备睡着时发的
                if len(seen) == 2 and self.mgr.busy(task):
                    answered.append(task)
                    self.mgr.answer(self.mgr.waits()[0]["id"], "allow")
        self.mgr.subscribe(on)
        t = self.mgr.create("ls 一下")["id"]
        self.done()
        self.assertEqual(answered, [t])
        self.assertEqual(self.mgr.summary(t)["status"], "done")


class Lifecycle(Harness):
    def test_archive_idle_and_running(self):
        self.next_script = [reply("好")]
        t = self.mgr.create("闲着的")["id"]
        self.done()
        self.mgr.archive(t)
        self.assertEqual(self.mgr.list(), [])
        self.assertIn(("archived", t, {}), self.seen)

        gate, entered = threading.Event(), threading.Event()
        self.next_script = [blocking(gate, "做完了", entered)]
        t = self.mgr.create("在跑的")["id"]
        self.assertTrue(entered.wait(T))                               # 真的在调模型了再归档（否则排队中就被取消、直接移走）
        self.mgr.archive(t)                                            # 在跑：先取消，停下后再移走
        self.assertEqual(len(self.mgr.list()), 1)
        gate.set()
        self.done()
        self.assertEqual(self.mgr.list(), [])
        with self.assertRaises(NotFound):
            self.mgr.summary(t)

    def test_recover_after_restart(self):
        self.next_script = [reply(calls=[("c1", "bash", {"command": "ls"})])]
        waiting = self.mgr.create("在等的")["id"]
        self.done()
        self.mgr.close(wait=True)
        # 模拟进程在“收到输入、还没开始跑”时退出：直接往账本里记一条输入
        pending = self.ts.create("没跑的")
        Runner("ledger", self.ts.store(pending.id), FakeModel([]), box(), Ask(), "SYS").submit("没跑的")

        self.ts = TaskStore(self.ts.root, scratch=self.ts.scratch)    # 新进程
        self.scripts["没跑的"] = [reply("补上了")]
        self.mgr = self.make()
        woken = self.mgr.recover()
        self.done()
        self.assertEqual(woken, [pending.id])
        self.assertEqual(self.mgr.summary(pending.id)["status"], "done")
        self.assertEqual(self.mgr.summary(waiting)["status"], "waiting")   # 在等的继续等
        self.assertEqual(len(self.mgr.waits()), 1)

    def test_runner_crash_becomes_error(self):
        class Boom(Runner):
            def run(self, max_iterations=10_000):
                for _ in range(2):                  # 走两步（审核输入、开始这一轮），然后出 bug
                    state = self._fold()
                    self._do_all(k.decide(state, self.policy), state)
                raise KeyError("意外")
        self.runner_cls = Boom
        t = self.mgr.create("会崩的")["id"]
        self.done()
        s = self.mgr.summary(t)
        self.assertEqual(s["status"], "error")
        self.assertIn("服务内部出错：KeyError", s["note"])
        self.assertFalse(self.mgr.busy(t))


class AsksQ(Policy):
    """主 Agent：ask_user 等人回答，bash 照旧要审批。"""
    def wait_for(self, call, state):
        if call["name"] == "ask_user":
            return WaitSpec("question", "")
        return WaitSpec("approval", "要跑命令") if call["name"] == "bash" else None


class Questions(Harness):
    """ask_user：问题的形状、回答 / 让它自己定、校验、胶囊、重启后还在。见 design/ask-user.md。"""
    def setUp(self):
        super().setUp()
        self.policy = AsksQ()

    def question_case(self, title="整理", options=("只看华北", "全国一起看")):
        args = {"question": "先看哪?", **({"options": list(options)} if options else {})}
        self.scripts[title] = [reply("问一下", calls=[("q1", "ask_user", args)]), reply("好的")]
        t = self.mgr.create(title)["id"]
        self.done()
        return t

    def step(self, t):
        return next(s for s in self.mgr.detail(t)["steps"] if s.get("tool") == "ask_user")

    def test_question_wait_shape(self):
        t = self.question_case()
        w = self.mgr.waits()[0]
        self.assertEqual({k: w[k] for k in ("kind", "title", "body", "options", "choices", "task")},
                         {"kind": "question", "title": "先看哪?", "body": "", "options": ["只看华北", "全国一起看"],
                          "choices": ["answer", "skip"], "task": t})
        self.assertEqual(self.mgr.summary(t)["status"], "waiting")
        self.assertEqual(self.mgr.summary(t)["now"], "问你：先看哪?")
        self.assertEqual(self.step(t)["title"], "问你：先看哪?")
        self.assertEqual(self.step(t)["status"], "waiting")

    def test_no_options(self):
        self.question_case(options=())
        self.assertEqual(self.mgr.waits()[0]["options"], [])

    def test_answer_and_skip(self):
        a = self.question_case("甲")
        b = self.question_case("乙")
        wa, wb = ({w["task"]: w for w in self.mgr.waits()}[x]["id"] for x in (a, b))
        self.mgr.answer(wa, "answer", note="华北")
        self.mgr.answer(wb, "skip")
        self.done()
        self.assertEqual(self.mgr.summary(a)["status"], "done")
        self.assertEqual((self.step(a)["out"], self.step(a)["status"]), ("你答：华北", "ok"))
        self.assertEqual((self.step(b)["out"], self.step(b)["status"]), ("你让它自己定", "ok"))

    def test_answer_needs_note(self):
        self.question_case()
        wid = self.mgr.waits()[0]["id"]
        with self.assertRaises(BadRequest):
            self.mgr.answer(wid, "answer")
        with self.assertRaises(BadRequest):
            self.mgr.answer(wid, "answer", note="   ")
        with self.assertRaises(BadRequest):
            self.mgr.answer(wid, "allow")
        self.mgr.answer(wid, "answer", note="华北")
        with self.assertRaises(Conflict):
            self.mgr.answer(wid, "skip")

    def test_first_wait_kind(self):
        self.question_case()
        self.assertEqual(self.mgr.status()["first_wait"]["kind"], "question")

    def test_question_survives_reload(self):
        t = self.question_case()
        self.mgr.close(wait=True)
        self.ts = TaskStore(self.ts.root, scratch=self.ts.scratch)    # 新进程
        self.mgr = self.make()
        self.mgr.recover()
        self.done()
        ws = self.mgr.waits()
        self.assertEqual([w["kind"] for w in ws], ["question"])
        self.scripts["整理"] = [reply("好的")]
        self.next_script = [reply("好的")]
        self.mgr.answer(ws[0]["id"], "answer", note="华北")
        self.done()
        self.assertEqual(self.mgr.summary(t)["status"], "done")

    def test_first_wait_prefers_question(self):
        """别处有更早的审批时，胶囊也先提醒“问你”。"""
        self.scripts["发布"] = [reply("我要发布", calls=[("c1", "bash", {"command": "npm publish"})]), reply("好")]
        self.mgr.create("发布")
        self.done()
        self.question_case()
        self.assertEqual(len(self.mgr.waits()), 2)
        self.assertEqual(self.mgr.status()["first_wait"]["kind"], "question")


if __name__ == "__main__":
    unittest.main()
