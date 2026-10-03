"""设置页的门面：weaverd 的 /v1/settings/* 路由都调这里。见 design/settings.md。

所有写操作排队（一把锁），每次都基于文件当前内容改。MCP 加了、改了就在后台连起来，
列表里的状态从连接池读（weaver/mcp/pool.py）。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

from ..errors import BadRequest, NotFound
from ..mcp import auth
from ..mcp.config import parse
from . import mcp, memories, model, permits, skills

DESKTOP = "~/Library/Application Support/Claude/claude_desktop_config.json"


class Settings:
    def __init__(self, home: str | Path, pool: Callable[[], object] = lambda: None,
                 claude_home: str | Path | None = None, desktop_json: str | Path | None = None,
                 claude_json: str | Path | None = None):
        """pool：返回 McpPool 的函数（没装 SDK 返回 None）。claude_* 可以指到别处（测试用）。"""
        self.home = Path(home)
        self.pool = pool
        self.claude_home = Path(claude_home or "~/.claude").expanduser()
        self.claude_json = Path(claude_json).expanduser() if claude_json else \
            (self.claude_home.parent / ".claude.json" if claude_home else Path("~/.claude.json").expanduser())
        self.desktop_json = Path(desktop_json or DESKTOP).expanduser()
        self.tokens = auth.TokenFile(self.home / "mcp-tokens.json")
        self._lock = threading.Lock()

    # ------------------------------------------------ 模型

    def model(self) -> dict:
        return model.read(self.home / ".env")

    def save_model(self, data: dict) -> dict:
        with self._lock:
            model.save(self.home / ".env", data)
        return {"restart": True}

    # ------------------------------------------------ MCP

    def _cfgs(self, raw: dict[str, dict]):
        return {n: parse(n, c, "user", str(self.home / "mcp.json"), dict(os.environ)) for n, c in raw.items()}

    def _connect(self, names: list[str]) -> None:
        pool = self.pool()
        if pool is None:
            return
        cfgs = self._cfgs({n: c for n, c in mcp.load(self.home).items() if n in names})
        keys = [pool.register(c, None) for c in cfgs.values() if not c.problem and not c.raw.get("disabled")]
        pool.manager.ensure(keys, wait=False, retry=True)

    def mcp_list(self) -> dict:
        try:
            raw = mcp.load(self.home)
        except mcp.BrokenConfig as e:
            return {"servers": [], "problem": str(e)}
        pool = self.pool() if raw else None
        out = []
        for name, cfg in self._cfgs(raw).items():
            item = {"name": name, "config": raw[name], "transport": cfg.transport, "status": "idle",
                    "error": "", "tools": []}
            if cfg.problem:
                item.update(status="invalid", error=cfg.problem)
            elif raw[name].get("disabled"):
                item["error"] = "已停用（配置里写了 disabled）"
            elif pool is None:
                item["error"] = "weaverd 没装 MCP SDK"
            else:
                st = pool.manager.servers.get(pool.register(cfg, None))
                pending = st.connecting is not None and not st.connecting.done()
                login = dict(st.login) if st.login else None
                if login and not login.get("error"):
                    item.update(status="logging_in", login=login)
                elif st.client is not None:
                    item["status"] = "connected"
                elif pending:
                    item["status"] = "connecting"
                elif st.needs_login:
                    item.update(status="needs_login", error=(login or {}).get("error") or st.error)
                elif st.error:
                    item.update(status="failed", error=st.error)
                elif st.connected_once:
                    item["status"] = "connected"            # 闲置被关了，工具清单还在，调用时再连
                item["tools"] = [t.name for t in st.tools]
            out.append(item)
        return {"servers": out, "problem": None}

    def mcp_parse(self, text: str) -> dict:
        servers = mcp.parse_text(text)
        current = mcp.load(self.home)
        return {"servers": servers, "conflicts": [n for n in servers if n in current]}

    def mcp_add(self, servers: dict, overwrite: bool = False) -> list[str]:
        with self._lock:
            names = mcp.add(self.home, servers, overwrite)
        self._connect(names)
        return names

    def mcp_replace(self, name: str, config: dict, rename: str | None = None) -> str:
        with self._lock:
            new = mcp.replace(self.home, name, config, rename)
        self._connect([new])
        return new

    def mcp_remove(self, name: str) -> None:
        with self._lock:
            url = str(mcp.load(self.home).get(name, {}).get("url") or "")
            mcp.remove(self.home, name)
            if url and url not in [str(c.get("url") or "") for c in mcp.load(self.home).values()]:
                self.tokens.drop(url)                # 没有别的服务器用这个地址了：令牌一起删

    def mcp_login(self, name: str) -> dict:
        """⌘L：按 design/mcp2.md 第一节的顺序挑一条路。返回 {"kind": "done" | "browser" | "device", …}。"""
        raw = mcp.load(self.home)
        if name not in raw:
            raise NotFound(f"没有叫 {name} 的 MCP 服务器")
        cfg = self._cfgs({name: raw[name]})[name]
        if cfg.transport != "http":
            raise BadRequest("本地服务器不用登录")
        pool = self.pool()
        if pool is None:
            raise BadRequest("weaverd 没装 MCP SDK，登录不了")
        if auth.is_github(cfg.url) and auth.gh_token():        # GitHub + gh 已登录：直接用它
            self.mcp_use_gh(name)
            return {"kind": "done", "via": "gh"}
        if cfg.problem:
            raise BadRequest(f"{name} 的配置有问题：{cfg.problem}")
        key = pool.register(cfg, None)
        info = pool.manager._run(auth.probe_login(cfg), 20)
        if info and info.get("registration"):
            return pool.manager.login(key, "browser")
        if auth.is_github(cfg.url):
            if auth.github_client_id():
                return pool.manager.login(key, "device", client_id=auth.github_client_id())
            raise BadRequest("GitHub 登录要用 gh：在终端里运行 gh auth login，登录后再按 ⌘L")
        if info is None:
            raise BadRequest(f"{name} 现在不要求登录（连不上的话看看地址和网络）")
        raise BadRequest("这个服务器不支持自动登录：在它的网站上生成一个令牌，填进配置的 headers（↵ 编辑）")

    def mcp_use_gh(self, name: str) -> None:
        """GitHub 改成用本机 gh 的登录：配置写 "auth": "gh"，去掉手写的 Authorization 头。"""
        with self._lock:
            config = dict(mcp.load(self.home)[name])
            headers = {k: v for k, v in (config.get("headers") or {}).items() if k.lower() != "authorization"}
            config.pop("headers", None)
            if headers:
                config["headers"] = headers
            config["auth"] = "gh"
            mcp.replace(self.home, name, config)
        self._connect([name])

    def mcp_reconnect(self, name: str) -> None:
        raw = mcp.load(self.home)
        if name not in raw:
            raise NotFound(f"没有叫 {name} 的 MCP 服务器")
        cfg = self._cfgs({name: raw[name]})[name]
        if cfg.problem:
            raise BadRequest(f"{name} 的配置有问题：{cfg.problem}")
        pool = self.pool()
        if pool is None:
            raise BadRequest("weaverd 没装 MCP SDK，连不了")
        pool.manager.reconnect(pool.register(cfg, None))

    def mcp_presets(self) -> list[dict]:
        return mcp.presets()

    def mcp_add_preset(self, preset_id: str, values: dict) -> str:
        with self._lock:
            name = mcp.add_preset(self.home, preset_id, values)
        preset = next((p for p in mcp.presets() if p["id"] == preset_id), {})
        if preset.get("login") == "github" and auth.gh_token():
            self.mcp_use_gh(name)                     # 本机 gh 已登录：直接用，不用再登录
        else:
            self._connect([name])
        return name

    def mcp_import(self) -> list[dict]:
        return mcp.import_sources(self.claude_json, self.desktop_json)

    # ------------------------------------------------ skills

    def skills(self) -> dict:
        return skills.list_skills(self.home, self.claude_home)

    def skill(self, name: str) -> dict:
        return skills.read_skill(self.home, self.claude_home, name)

    def skill_create(self, text: str) -> str:
        with self._lock:
            return skills.create_skill(self.home, self.claude_home, text)

    def skill_save(self, name: str, text: str) -> str:
        with self._lock:
            return skills.save_skill(self.home, self.claude_home, name, text)

    def skill_remove(self, name: str) -> None:
        with self._lock:
            skills.remove_skill(self.home, self.claude_home, name)

    # ------------------------------------------------ 权限（信任过的项目；“总是允许”在任务管理器里）

    def permission_projects(self) -> list[dict]:
        return permits.projects(self.home)

    def forget_project(self, root: str) -> None:
        with self._lock:
            permits.forget(self.home, root)

    # ------------------------------------------------ 记忆

    def memory(self, roots: list[str]) -> dict:
        return memories.list_all(self.home, roots)

    def memory_read(self, scope: str, root: str, name: str) -> dict:
        return memories.read(self.home, scope, root, name)

    def memory_save(self, scope: str, root: str, name: str | None, text: str) -> str:
        with self._lock:
            return memories.save(self.home, scope, root, name, text)

    def memory_remove(self, scope: str, root: str, name: str) -> None:
        with self._lock:
            memories.remove(self.home, scope, root, name)
