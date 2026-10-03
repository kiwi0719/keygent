"""常驻服务里的 MCP：所有任务共用连接，每个任务一份自己的工具清单。见 design/mcp.md 第九节。

- McpPool：weaverd 里只有一个。同样的服务器（配置相同、工作目录相同）只起一个；闲置 10 分钟关掉，下次调用时再连；
  连接失败的过一会儿才再试。用户级服务器的工作目录是家目录（所有任务共用一个），项目级的是项目目录。
- TaskMcp：一个任务的 MCP。每一轮开始前（Runner.before_run）读一次配置、把用得上的服务器连上；
  能用的服务器没变就什么都不动，变了（第一次、信任了项目、配置改了、之前连不上的连上了）才换工具清单和背景信息。
  工具清单变了会让缓存前缀失效一次，所以只在这些时候变。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

from ..context import ContextProvider
from . import sdk_available
from .config import McpConfig, ServerConfig

log = logging.getLogger("weaverd")
IDLE = float(os.environ.get("WEAVER_MCP_IDLE") or 600)
RETRY_AFTER = 60.0


class McpPool:
    def __init__(self, home: str | Path, connect_timeout: float | None = None, idle: float | None = IDLE,
                 retry_after: float = RETRY_AFTER):
        from .manager import McpManager
        self.home = Path(home)
        timeout = connect_timeout or float(os.environ.get("WEAVER_MCP_TIMEOUT") or 10)   # npx 第一次要下载，可调大
        self.manager = McpManager([], self.home / "mcp-logs", connect_timeout=timeout, idle=idle,
                                  retry_after=retry_after)
        from ..stores import DirBlobStore
        self.manager.blobs = DirBlobStore(self.home / "blobs")      # 工具返回的图片（和附件同一个仓库）

    def register(self, cfg: ServerConfig, root: Path | None) -> str:
        """登记一个服务器，返回它在管理器里的 key。stdio 服务器的工作目录按级别定，写进 key。"""
        cwd = ""
        if cfg.transport == "stdio":
            base = Path(root) if cfg.level == "project" and root is not None else Path.home()
            cwd = str((base / Path(cfg.cwd).expanduser()) if cfg.cwd else base)
            cfg = dataclasses.replace(cfg, cwd=cwd)
        # 按 ${VAR} 展开之后的内容算：令牌换了（设置页重填、.env 改了）就是新连接，不沿用旧令牌的那个
        expanded = json.dumps([cfg.transport, cfg.command, cfg.args, cfg.env, cwd, cfg.url, cfg.headers, cfg.trust],
                              ensure_ascii=False, sort_keys=True)
        h = hashlib.sha256(expanded.encode()).hexdigest()[:10]
        key = re.sub(r"[^A-Za-z0-9_-]", "_", cfg.name)[:40] + "-" + h
        self.manager.add(key, cfg)
        return key

    def warm(self, conf: McpConfig) -> list[str]:
        """启动时先把用户级服务器连起来（不等），任务开始时多半已经连好了。"""
        keys = [self.register(c, None) for c in conf.servers().values() if c.level == "user" and not c.problem]
        self.manager.ensure(keys, wait=False)
        return keys

    def close(self) -> None:
        self.manager.close()


class TaskMcp(ContextProvider):
    kind = "mcp"
    first_head = ""
    update_head = "MCP 服务器的情况有变化，以下是最新的说明。"

    def __init__(self, conf: McpConfig, pool, toolbox):
        """pool：返回 McpPool 的函数（第一次用到才建，没装 SDK 时返回 None）。"""
        self.conf, self._pool, self.toolbox = conf, pool, toolbox
        self.current = None                          # McpTools；没有服务器时是 None
        self.sig = ((), ())                          # 没有服务器时的样子：没配 MCP 的任务从不“变”
        self.installed: set[str] = set()
        self.caller = None                           # 这个任务（weaver.mcp.callers.Caller）：服务器提问、借模型时找它
        self._lock = threading.Lock()

    # ------------------------------------------------ 每轮开始前

    def prepare(self, wait: bool = True) -> bool:
        """读配置、连服务器；能用的服务器变了就换工具清单。返回是否变了。"""
        from .tools import McpTools, McpView
        servers = self.conf.servers()
        notes, usable = [], []
        for c in servers.values():
            if c.problem:
                notes.append(f"- {c.name}：配置有问题（{c.problem}），用不了")
            elif not self.conf.is_trusted(c):
                notes.append(f"- {c.name}：项目配置里的服务器，用户还没确认信任，用不了")
            else:
                usable.append(c)
        pool = self._pool() if usable else None
        if usable and pool is None:
            notes = [f"- {c.name}：weaverd 没装 MCP SDK，用不了" for c in usable] + notes
            usable = []
        keys = {c.name: pool.register(c, self.conf.root) for c in usable} if pool else {}
        if keys:
            pool.manager.ensure(list(keys.values()), wait=wait)
        # version：服务器说过“工具清单变了”，重新拉到的清单不一样就在这一轮换上（design/mcp2.md 第六节）
        state = tuple(sorted((n, k, pool.manager.servers[k].connected_once, pool.manager.servers[k].version)
                             for n, k in keys.items()))
        sig = (state, tuple(notes))
        with self._lock:
            if sig == self.sig:
                return False
            self.sig = sig
            self.current = (McpTools(McpView(pool.manager if pool else None, keys, self.caller), notes)
                            if (keys or notes) else None)
            new = self.current.tools() if self.current else {}
            for name in self.installed - set(new):
                self.toolbox.tools.pop(name, None)
            self.toolbox.tools.update(new)
            self.installed = set(new)
        return True

    # ------------------------------------------------ prompts（启动器里 / 选用）

    def usable(self) -> dict[str, str]:
        """能用的服务器：显示名 → 连接池里的 key（用户级 + 已信任的项目级）。不连接。"""
        pool = self._pool()
        if pool is None:
            return {}
        return {c.name: pool.register(c, self.conf.root) for c in self.conf.servers().values()
                if not c.problem and self.conf.is_trusted(c)}

    # ------------------------------------------------ 信任（并进“信任这个项目吗”，见 agents.ProjectTrust）

    def untrusted(self) -> list[ServerConfig]:
        if not sdk_available():                      # 信任了也用不了，就不问
            return []
        return self.conf.untrusted()

    def trust(self, cfgs: list[ServerConfig]) -> None:
        self.conf.trust(cfgs)
        pool = self._pool()
        if pool is not None:                         # 先连起来（不等），下一轮开始时多半已经好了
            pool.manager.ensure([pool.register(c, self.conf.root) for c in cfgs], wait=False)

    # ------------------------------------------------ 给工具箱、权限、子 Agent、背景信息

    def tools(self, readonly_only: bool = False) -> dict:
        cur = self.current
        return cur.tools(readonly_only) if cur else {}

    def rule(self, name: str, args: dict | None = None) -> str:
        cur = self.current
        return cur.rule(name, args) if cur else "ask"

    def render(self) -> str:
        cur = self.current
        return cur.render() if cur else ""
