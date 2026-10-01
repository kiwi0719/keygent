"""把 MCP 服务器变成 Weaver 的工具和背景信息。见 design/mcp.md 第四、五节。

- 工具总数 ≤ 30：直接列成工具 mcp__服务器__工具（会话开始时定下来，整个会话不变）。
- > 30：延迟模式，只给 mcp_search + mcp_call 两个固定工具——工具清单在请求最前面，中途增删会让整个缓存前缀失效。
- 有服务器声明 resources 时加 mcp_list_resources、mcp_read_resource。
- 服务器的 instructions、连不上的原因、（延迟模式下）工具一览，作为背景信息（context 机制）。

工具清单和背景信息都是造它那一刻的快照：之后服务器断开、闲置被关、重连，都不改变（缓存前缀不断）。
manager 可以是 McpManager（命令行：key 就是服务器名），也可以是 McpView（常驻服务：一个任务看到的那几个）。
"""
from __future__ import annotations

import hashlib
import json
import re

from ..context import ContextProvider
from ..tools import Tool, _spec

DIRECT_LIMIT = 30
MAX_DESC = 1000
SEARCH_LIMIT = 10


def tool_name(server: str, tool: str) -> str:
    """mcp__服务器__工具：只留字母、数字、_、-；超过 64 个字符就截短加短哈希（Cline 的做法）。"""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")
    if len(name) > 64:
        name = name[:55] + "_" + hashlib.sha256(name.encode()).hexdigest()[:8]
    return name


def _readonly(t) -> bool:
    ann = getattr(t, "annotations", None)
    return bool(ann and getattr(ann, "read_only_hint", False))


def _schema(t) -> dict:
    s = dict(getattr(t, "input_schema", None) or {})
    s.setdefault("type", "object")
    s.setdefault("properties", {})
    return s


class McpTools(ContextProvider):
    kind = "mcp"
    first_head = ""
    update_head = "MCP 服务器的情况有变化，以下是最新的说明。"

    def __init__(self, manager, notes: list[str] | None = None):
        """notes：背景信息里另外要说的（常驻服务：配置有问题、项目级还没信任的服务器）。"""
        self.manager = manager
        self.notes = list(notes or [])
        # 所有工具：工具名 → (服务器名, 原工具名, SDK 的 Tool 对象)
        self.entries: dict[str, tuple[str, str, object]] = {}
        self.failed: dict[str, str] = {}                 # 连不上的服务器 → 原因
        self.login: set[str] = set()                     # 要登录的服务器
        self.instructions: dict[str, str] = {}
        self.resources = False
        for name, st in manager.servers.items():
            if not st.connected_once:
                self.failed[name] = st.error or "未知原因"
                if getattr(st, "needs_login", False):
                    self.login.add(name)
                continue
            self.instructions[name] = st.instructions
            self.resources = self.resources or st.resources
            for t in st.tools:
                self.entries[tool_name(name, t.name)] = (name, t.name, t)
        self.deferred = len(self.entries) > DIRECT_LIMIT

    # ------------------------------------------------ 权限要用的信息

    def rule(self, name: str, args: dict | None = None) -> str:
        """allow / ask。mcp_call 看它要调的工具；只读标记 + 服务器的 trust 配置决定放不放行。"""
        if name in ("mcp_search", "mcp_list_resources", "mcp_read_resource"):
            return "allow"
        if name == "mcp_call":
            name = tool_name((args or {}).get("server", ""), (args or {}).get("tool", ""))
        entry = self.entries.get(name)
        if entry is None:
            return "ask"
        server, _, t = entry
        trust = self.manager.servers[server].cfg.trust
        if trust == "all" or (_readonly(t) and trust in ("", "readonly")):
            return "allow"
        return "ask"

    # ------------------------------------------------ 工具

    def tools(self, readonly_only: bool = False) -> dict[str, Tool]:
        """readonly_only：给 explore 子 Agent 用，只要标了只读的。"""
        out: dict[str, Tool] = {}
        if self.deferred:
            out["mcp_search"] = Tool(_spec(
                "mcp_search", "按关键字查找 MCP 工具，返回服务器名、工具名、说明和参数 schema。找到后用 mcp_call 调用。",
                {"query": {"type": "string", "description": "关键字，匹配工具名和说明"}}, ["query"]),
                lambda query: self.search(query, readonly_only), readonly=True)
            out["mcp_call"] = Tool(_spec(
                "mcp_call", "调用一个 MCP 工具（先用 mcp_search 查到它的参数 schema）。",
                {"server": {"type": "string"}, "tool": {"type": "string"},
                 "arguments": {"type": "object", "description": "按工具的参数 schema 填"}}, ["server", "tool"]),
                lambda server, tool, arguments=None: self._call(server, tool, arguments, readonly_only),
                readonly=False, parallel=lambda a: _readonly(self._lookup(a.get("server", ""), a.get("tool", ""))))
        else:
            for name, (server, tool, t) in self.entries.items():
                if readonly_only and not _readonly(t):
                    continue
                desc = f"[MCP 服务器 {server}] " + (getattr(t, "description", "") or "")
                out[name] = Tool({"name": name, "description": desc[:MAX_DESC], "parameters": _schema(t)},
                                 (lambda s, n: lambda **kw: self._call(s, n, kw))(server, tool),
                                 readonly=_readonly(t), parallel=(lambda ro: lambda a: ro)(_readonly(t)))
        if self.resources:
            out["mcp_list_resources"] = Tool(_spec(
                "mcp_list_resources", "列出 MCP 服务器提供的资源（数据、文档）。",
                {"server": {"type": "string", "description": "只看某个服务器；不填看全部"}}, []),
                lambda server=None: self.manager.list_resources(server), readonly=True)
            out["mcp_read_resource"] = Tool(_spec(
                "mcp_read_resource", "读取一个 MCP 资源的内容。",
                {"server": {"type": "string"}, "uri": {"type": "string"}}, ["server", "uri"]),
                lambda server, uri: self.manager.read_resource(server, uri), readonly=True)
        return out

    def _lookup(self, server: str, tool: str):
        entry = self.entries.get(tool_name(server, tool))
        return entry[2] if entry else None

    def _call(self, server: str, tool: str, arguments, readonly_only: bool = False):
        t = self._lookup(server, tool)
        if t is None:
            return f"没有这个 MCP 工具：{server} / {tool}。用 mcp_search 查一下。", {"is_error": True}
        if readonly_only and not _readonly(t):
            return "explore 子 Agent 只能调用标了只读的 MCP 工具。", {"is_error": True}
        text, is_error = self.manager.call(server, tool, arguments if isinstance(arguments, dict) else {})
        return text, {"is_error": is_error}

    def search(self, query: str, readonly_only: bool = False) -> str:
        q = query.lower()
        hits = [(s, n, t) for s, n, t in self.entries.values()
                if (q in n.lower() or q in (getattr(t, "description", "") or "").lower())
                and (not readonly_only or _readonly(t))]
        if not hits:
            return f"没有和“{query}”相关的 MCP 工具。"
        lines = []
        for s, n, t in hits[:SEARCH_LIMIT]:
            lines.append(f"- server={s} tool={n}{'（只读）' if _readonly(t) else ''}：{(getattr(t, 'description', '') or '')[:300]}\n"
                         f"  参数：{json.dumps(_schema(t), ensure_ascii=False)}")
        more = f"\n（还有 {len(hits) - SEARCH_LIMIT} 个，换个更具体的关键字）" if len(hits) > SEARCH_LIMIT else ""
        return "\n".join(lines) + more

    # ------------------------------------------------ 背景信息

    def render(self) -> str:
        lines = []
        for name in self.manager.servers:
            if name in self.login:
                lines.append(f"- {name}：要登录，登录前它的工具用不了。请用户在 Weaver 的设置 › MCP 里选中它按 ⌘L 登录"
                             f"（命令行：weaver mcp login {name}）")
            elif name in self.failed:
                lines.append(f"- {name}：连接失败（{self.failed[name]}），它的工具用不了")
            elif self.instructions.get(name):
                lines.append(f"- {name}：{self.instructions[name][:2000]}")
        lines += self.notes
        if self.deferred:
            lines.append(f"\nMCP 工具共 {len(self.entries)} 个，没有直接列出；用 mcp_search 按关键字查，再用 mcp_call 调用。一览：")
            for s, n, t in self.entries.values():
                lines.append(f"- {s}/{n}：{(getattr(t, 'description', '') or '').splitlines()[0][:80] if getattr(t, 'description', '') else ''}")
        return ("## MCP 服务器\n" + "\n".join(lines)) if lines else ""


class McpView:
    """一个任务看到的那几个服务器（常驻服务里所有任务共用一个 McpManager）。接口和 McpManager 一样，按显示名调用。"""

    def __init__(self, manager, keys: dict[str, str]):
        self.manager, self.keys = manager, dict(keys)        # 显示名 → 管理器里的 key

    @property
    def servers(self) -> dict:
        return {name: self.manager.servers[key] for name, key in self.keys.items()}

    def call(self, server: str, tool: str, arguments: dict | None) -> tuple[str, bool]:
        if server not in self.keys:
            return f"没有名为 {server} 的 MCP 服务器", True
        return self.manager.call(self.keys[server], tool, arguments)

    def list_resources(self, server: str | None = None) -> str:
        return self.manager.list_resources(server, self.keys)

    def read_resource(self, server: str, uri: str) -> str:
        if server not in self.keys:
            raise ValueError(f"没有名为 {server} 的 MCP 服务器")
        return self.manager.read_resource(self.keys[server], uri)
