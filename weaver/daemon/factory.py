"""真跑用的执行者工厂：照命令行的搭法，但所有路径都以任务的工作目录为根。见 design/daemon.md 第五节。

和命令行的区别：
- 不给 approver：审批、卡住了问人都停在账本里，等客户端回答（interactive=True）
- 项目级 skill、子 Agent 类型、MCP 服务器只加载已经信任过的；没信任的由任务管理器问（App 里的“信任这个项目吗”）
- MCP 连接所有任务共用（weaver/mcp/pool.py），每一轮开始前按需连上、工具清单变了才换
- 后台命令的日志和登记表放在任务目录的 jobs/ 下
- 子 Agent 要审批、卡住了：升到主任务的“等你的事”里（Runner.ask_up），阻塞等你回答
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from ..corpus import Corpus, daemon_sources
from ..agents import AgentTypes, ProjectTrust
from ..environment import Environment
from ..jobs import Jobs
from ..mcp import McpConfig, sdk_available
from ..mcp.pool import McpPool, TaskMcp
from ..memory import GUIDE as MEMORY_GUIDE, Memory
from ..permissions import PermissionPolicy
from ..providers import make_model
from ..providers.catalog import context_window
from ..runner import Runner
from ..sandbox import Sandbox
from ..skills import Skills
from ..stores import DirBlobStore
from ..subagent import GUIDE as SUBAGENT_GUIDE, SubAgents
from ..todo import tool as todo_tool
from ..ask import tool as ask_tool
from ..tools import ToolBox
from ..tools.recall import tool as recall_tool
from .tasks import LEDGER, TaskMeta

log = logging.getLogger("weaverd")

SYSTEM = ("你在用户的 Mac 上后台工作：用户把事情交给你，你自己做完，"
          "需要用户同意的操作会停下来等用户放行，用户随时可能插话。"
          "找东西时先用 grep（按内容）或 find_files（按文件名）定位，再用 read_file 只读相关的几行，"
          "不要整个文件读进来。互不依赖的查找可以在一轮里同时发起。"
          "改文件用 edit_file（改之前先读），新建文件用 write_file；改完用 bash 跑测试或运行来验证。"
          "被拒绝或被沙箱拦下的操作不要换个写法硬来，说明情况交给用户。"
          "最后的回答第一行写结论（会显示在任务列表上），简洁、直接。")


def _under(path: Path, parent: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


class RunnerFactory:
    def __init__(self, home: str | Path | None = None, model=None):
        self.home = Path(home or os.environ.get("WEAVER_HOME") or "~/.weaver").expanduser()
        self.blobs = DirBlobStore(self.home / "blobs")
        self.model = model or make_model(blobs=self.blobs)       # 所有任务共用一个模型客户端
        self.window = (int(os.environ["WEAVER_CONTEXT_WINDOW"]) if os.environ.get("WEAVER_CONTEXT_WINDOW")
                       else context_window(self.model.name, self.model.base_url, self.home) or 128_000)
        self.compact_at = int(os.environ["WEAVER_COMPACT_AT"]) if os.environ.get("WEAVER_COMPACT_AT") else None
        self.scratch = Path(os.environ.get("WEAVER_SCRATCH") or "~/Weaver/scratch").expanduser()
        self._models: dict = {}
        self._pool: McpPool | None = None
        self._pool_lock = threading.Lock()
        self.corpus = Corpus(daemon_sources(self.home / "tasks"))   # 跨任务搜索，所有任务共用一份缓存

    def __call__(self, meta: TaskMeta, store, sink, on_delta) -> Runner:
        root = Path(meta.workdir)
        sandbox = Sandbox.from_env(root)
        policy = PermissionPolicy(root, max_steps=meta.max_steps, token_budget=meta.token_budget,
                                  context_window=self.window, max_output=self.model.max_tokens,
                                  compact_at=self.compact_at, interactive=True, ask_user=True)
        scratch = _under(root, self.scratch)            # 临时目录里的任务没有“项目”，只有用户级记忆
        tools, memory, skills = ToolBox(root), Memory(root, project=not scratch), Skills(root)
        agents = AgentTypes(root, project=not scratch)
        mcp = TaskMcp(McpConfig(root, self.home, project=not scratch), self.mcp_pool, tools)
        policy.mcp = mcp
        system = SYSTEM + MEMORY_GUIDE + SUBAGENT_GUIDE
        tools.tools.update(memory.tools())
        jobs = Jobs(root, store.root / "jobs", sandbox)  # 结束通知由任务管理器接上（它知道怎么叫醒任务）
        tools.add_write_tools(meta.id, self.blobs, sandbox, jobs=jobs)
        tools.tools["recall"] = recall_tool(lambda: store.load(LEDGER), self.corpus, meta.id, meta.workdir)
        parent: list = []                                   # 子 Agent 的审批升到父任务里：父执行者造好后接上
        subagents = SubAgents(session=LEDGER, store=store, model=self.model, root=root.resolve(), sandbox=sandbox,
                              blobs=self.blobs, approver=lambda w, info: parent[0].ask_up(w, info),
                              approver_info=True, display=lambda *_: None,
                              context_window=self.window, max_output=self.model.max_tokens,
                              compact_at=self.compact_at, skills=skills, agent_types=agents,
                              model_for=self.model_for, jobs=jobs, mcp=mcp,
                              fork_source={"model": self.model, "system": system, "tools": tools})
        tools.on_interrupt.append(subagents.cancel_all)   # 取消主任务时，正在跑的子 Agent 一起取消（含后台的）
        tools.on_interrupt.append(lambda: jobs.cancel_agents())
        tools.tools["task"] = subagents.tool()
        tools.tools["todo_write"] = todo_tool()
        tools.tools["skill"] = skills.tool()
        tools.tools["ask_user"] = ask_tool()
        runner = Runner(LEDGER, store, self.model, tools, policy, system, sink=sink, on_delta=on_delta,
                        context=[Environment(root, sandbox), memory, skills, agents, mcp], extract_memory=memory)
        runner.before_run.append(lambda: self._prepare_mcp(meta.id, runner, mcp))
        runner.project_trust = None if scratch else ProjectTrust(skills, agents, mcp)   # 任务管理器据此问“信任吗”
        parent.append(runner)
        return runner

    # ------------------------------------------------ MCP

    def mcp_pool(self) -> McpPool | None:
        """所有任务共用的 MCP 连接。第一次用到才建（导入 SDK 要 30 多 MB）；没装 SDK 返回 None。"""
        with self._pool_lock:
            if self._pool is None and sdk_available():
                self._pool = McpPool(self.home)
            return self._pool

    def warm_mcp(self) -> int:
        """启动时：有用户级 MCP 服务器就先连起来（不等）。返回发起了几个。"""
        conf = McpConfig(self.home, self.home, project=False)
        if not conf.servers():
            return 0
        pool = self.mcp_pool()
        if pool is None:
            log.warning("配置了 MCP 服务器，但 weaverd 的 Python 里没装 MCP SDK，不连接")
            return 0
        return len(pool.warm(conf))

    @staticmethod
    def _prepare_mcp(task_id: str, runner, mcp: TaskMcp) -> None:
        try:
            if mcp.prepare():                       # 能用的服务器变了：背景信息跟着更新
                runner.sync_context()
        except Exception:                           # MCP 出问题不能挡着任务
            log.exception("任务 %s 准备 MCP 出错", task_id)

    def close(self) -> None:
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    def model_for(self, name: str):
        """自定义子 Agent 类型指定了模型：同一个服务商、换个模型名。造过的复用。"""
        if name not in self._models:
            self._models[name] = make_model({**os.environ, "WEAVER_MODEL": name}, blobs=self.blobs)
        return self._models[name]
