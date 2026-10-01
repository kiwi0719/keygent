"""MCP 连接管理：用官方 SDK 连所有服务器，给同步代码用。见 design/mcp.md 第六节。

SDK 是异步的（anyio），而且 anyio 的连接必须在同一个异步任务里打开和关闭。所以：
后台线程跑一个事件循环；每个服务器一个常驻的“守连接”任务（打开连接 → 等关闭信号 → 关闭）；
执行者的线程把调用提交到这个循环里，同步等结果。

服务器按 key 登记：命令行里 key 就是服务器名；常驻服务里所有任务共用一个管理器（weaver/mcp/pool.py），
key 带上配置哈希和工作目录，同样的服务器只起一个。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import auth
from .config import ServerConfig

MAX_OUTPUT = 75_000          # 约 25k token（和 Claude Code 的 MCP 输出上限一样）


@dataclass
class ServerState:
    cfg: ServerConfig
    tools: list = field(default_factory=list)       # SDK 的 Tool 对象
    instructions: str = ""
    resources: bool = False
    error: str = ""
    connected_once: bool = False                    # 连上过：工具清单、说明以那次为准（之后断开、闲置关掉都不变）
    failed_at: float = 0.0                          # 上次连接失败的时间（常驻服务里过一会儿才再试）
    last_used: float = 0.0
    busy: int = 0                                   # 正在进行的调用数：闲置清理不关正在用的
    client: object = None
    connecting: concurrent.futures.Future | None = None
    needs_login: bool = False                       # 远程服务器要（重新）授权（设置页显示“要登录 · ⌘L”）
    login: dict | None = None                       # 进行中 / 刚失败的登录：{"kind", "url" | "user_code", "error"}
    login_task: asyncio.Task | None = None
    ready: asyncio.Event | None = None
    stop: asyncio.Event | None = None
    task: asyncio.Task | None = None


def _content_text(result) -> str:
    """把工具结果的 content 转成文字：文字原样，图片写一句说明，结构化结果转 JSON。"""
    parts = []
    for c in result.content or []:
        kind = getattr(c, "type", "")
        if kind == "text":
            parts.append(c.text)
        elif kind == "image":
            parts.append(f"[图片 {getattr(c, 'mime_type', '')}，未展开]")
        elif kind == "audio":
            parts.append(f"[音频 {getattr(c, 'mime_type', '')}，未展开]")
        elif kind == "resource":
            res = getattr(c, "resource", None)
            parts.append(getattr(res, "text", None) or f"[资源 {getattr(res, 'uri', '')}，未展开]")
        elif kind == "resource_link":
            parts.append(f"[资源链接 {getattr(c, 'uri', '')}]")
    text = "\n".join(p for p in parts if p)
    if not text and getattr(result, "structured_content", None) is not None:
        text = json.dumps(result.structured_content, ensure_ascii=False, indent=2)
    return text or "(没有输出)"


async def _probe(cfg: ServerConfig):
    """给远程服务器发一个 initialize，返回 (HTTP 状态码, 出错时响应的第一行)；连不上返回 (异常的类名, "")。"""
    import httpx2
    body = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "weaver", "version": "0.1"}}}
    try:
        async with httpx2.AsyncClient(headers=cfg.headers, timeout=5) as c:
            r = await c.post(cfg.url, json=body, headers={"Accept": "application/json, text/event-stream"})
            said = (r.text.strip().splitlines() or [""])[0][:120] if r.status_code >= 400 else ""
            return r.status_code, said
    except Exception as e:
        return type(e).__name__, ""


def _open_url(url: str) -> None:
    """用系统默认浏览器打开（macOS 的 open；别的系统用 webbrowser）。"""
    import subprocess
    import sys
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import webbrowser
            webbrowser.open(url)
    except OSError:
        pass


def _cap(text: str) -> str:
    if len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + f"\n…[MCP 输出超过 {MAX_OUTPUT // 1000}K 字，已截断]"
    return text


class McpManager:
    def __init__(self, configs: list[ServerConfig], log_dir: str | Path, connect_timeout: float = 10,
                 call_timeout: float = 120, idle: float | None = None, retry_after: float = 0,
                 tokens: auth.TokenFile | None = None):
        """idle：连接闲置这么多秒就关掉（下次调用时再连），None 表示不关。
        retry_after：连接失败后多少秒内 ensure() 不再重试（调用时照样重连一次）。
        tokens：登录得到的令牌（默认 log_dir 旁边的 mcp-tokens.json）。"""
        self.servers: dict[str, ServerState] = {c.name: ServerState(c) for c in configs}
        self.log_dir = Path(log_dir)
        self.tokens = tokens or auth.TokenFile(self.log_dir.parent / "mcp-tokens.json")
        self.open_url = _open_url                  # 登录时打开授权页（测试里换掉）
        self.connect_timeout, self.call_timeout = connect_timeout, call_timeout
        self.idle, self.retry_after = idle, retry_after
        self._lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="weaver-mcp", daemon=True)
        self.thread.start()
        self._reaper = asyncio.run_coroutine_threadsafe(self._reap(), self.loop) if idle else None

    def add(self, key: str, cfg: ServerConfig) -> ServerState:
        """登记一个服务器（已经有了就用原来的）。不连接。"""
        with self._lock:
            if key not in self.servers:
                self.servers[key] = ServerState(cfg)
            return self.servers[key]

    # ------------------------------------------------ 连接

    def _transport(self, key: str, cfg: ServerConfig):
        from mcp.client.stdio import StdioServerParameters, stdio_client
        if cfg.transport == "stdio":
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log = open(self.log_dir / f"{key}.log", "a", encoding="utf-8")      # 服务器的报错不刷到终端
            # env：SDK 只继承少数安全的环境变量（PATH、HOME 等），再加上配置里明确写的——不会把我们的 key 传过去
            params = StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None, cwd=cfg.cwd)
            return stdio_client(params, errlog=log), log
        import httpx2
        from mcp.client.streamable_http import streamable_http_client
        headers, how = auth.http_auth(cfg, self.tokens)           # gh、设备码令牌、OAuth（过期先刷新）
        http = httpx2.AsyncClient(headers=headers, auth=how)       # 自己传进去的客户端 SDK 不会关，_hold 收尾时关
        return streamable_http_client(cfg.url, http_client=http), http

    async def _hold(self, key: str, st: ServerState) -> None:
        from mcp import Client
        log = None
        try:
            transport, log = self._transport(key, st.cfg)
            async with Client(transport, read_timeout_seconds=self.call_timeout) as client:
                tools, cursor = [], None
                while True:                                   # 工具清单可能分页
                    page = await client.list_tools(cursor=cursor)
                    tools += page.tools
                    cursor = page.next_cursor
                    if not cursor:
                        break
                st.client, st.tools = client, tools
                st.instructions = (client.instructions or "").strip()
                st.resources = getattr(client.server_capabilities, "resources", None) is not None
                st.error, st.connected_once, st.failed_at, st.last_used = "", True, 0.0, time.monotonic()
                st.needs_login = False
                st.ready.set()
                await st.stop.wait()
        except BaseException as e:                            # anyio 的取消也是 BaseException
            if not st.error:
                st.error = ("连接被取消" if isinstance(e, asyncio.CancelledError)
                            else (await self._describe(key, st, e)))[:300]
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            st.client = None
            st.ready.set()
            if hasattr(log, "aclose"):
                try:
                    await log.aclose()
                except Exception:
                    pass
            elif log:
                log.close()

    async def _describe(self, key: str, st: ServerState, e: BaseException) -> str:
        """连不上的原因，写成人话。SDK 会把 HTTP 状态码吞掉（只剩“Server returned an error response”），
        所以远程服务器再探一次拿状态码；本地服务器指向它的日志。"""
        cfg = st.cfg
        while isinstance(e, BaseExceptionGroup) and e.exceptions:
            e = e.exceptions[0]
        cause = e
        while cause is not None and not isinstance(cause, auth.NeedsLogin):    # 可能被 SDK 包了一层
            cause = cause.__cause__ or cause.__context__
        if isinstance(cause, auth.NeedsLogin):
            st.needs_login = True
            return str(cause)
        if isinstance(e, FileNotFoundError) and cfg.transport == "stdio":
            return f"找不到命令 {cfg.command}（装了吗？weaverd 的 PATH 里有吗？）"
        if cfg.transport == "http":
            status, said = await _probe(cfg)
            host = urlparse(cfg.url).hostname or cfg.url
            said = f"：{said}" if said else ""
            if status == 401 and await auth.probe_login(cfg) is not None:
                st.needs_login = True
                return str(auth.NeedsLogin())
            if status in (401, 403):
                return f"服务器拒绝了（HTTP {status}）：令牌不对、过期或没有权限"
            if status == 404:
                return "地址不对（HTTP 404）"
            if isinstance(status, int) and status >= 400:
                return f"服务器出错（HTTP {status}）{said}"
            if status in ("ConnectError", "ConnectTimeout"):
                return f"连不上 {host}：网络不通或地址写错"
        text = (str(e).strip().splitlines() or [""])[0]
        if cfg.transport == "stdio" and "closed" in text.lower():
            return f"服务器进程退出了，看日志 {self.log_dir / (key + '.log')}"
        return f"{type(e).__name__}: {text}" if text else type(e).__name__

    async def _connect(self, key: str, st: ServerState) -> None:
        st.ready, st.stop, st.error = asyncio.Event(), asyncio.Event(), ""
        st.task = asyncio.create_task(self._hold(key, st))
        try:
            await asyncio.wait_for(st.ready.wait(), self.connect_timeout)
        except asyncio.TimeoutError:
            st.error = f"连接超时（{self.connect_timeout:g} 秒）"
            st.stop.set()
            st.task.cancel()
        if st.client is None:
            st.failed_at = time.monotonic()

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def ensure(self, keys, wait: bool = True, retry: bool = False) -> None:
        """把这些服务器连上（已连上的不动；正在连的不重复连，一起等）。
        retry=False 时，刚失败过（retry_after 秒内）的不再试。wait=False 只发起、不等。"""
        futures = []
        with self._lock:
            for key in keys:
                st = self.servers[key]
                if st.client is not None:
                    continue
                if st.connecting is None or st.connecting.done():
                    if not retry and st.failed_at and time.monotonic() - st.failed_at < self.retry_after:
                        continue
                    st.connecting = asyncio.run_coroutine_threadsafe(self._connect(key, st), self.loop)
                futures.append(st.connecting)
        if wait and futures:
            concurrent.futures.wait(futures, timeout=self.connect_timeout + 5)

    # ------------------------------------------------ 登录（design/mcp2.md 第一节）

    def login(self, key: str, how: str, client_id: str = "", wait: float = 15) -> dict:
        """开始登录：how = browser（浏览器授权）/ device（GitHub 设备码）。在后台进行；拿到授权页地址或设备码
        （或者失败）就返回 st.login 的副本。同一个服务器再登录一次会取消上一次。"""
        st = self.servers[key]
        ready = concurrent.futures.Future()

        async def start():
            if st.login_task is not None and not st.login_task.done():
                st.login_task.cancel()
                await asyncio.wait([st.login_task], timeout=5)
            st.login = {"kind": how}
            st.login_task = asyncio.create_task(self._login(key, st, how, client_id, ready))
        self._run(start(), 10)
        try:
            ready.result(wait)
        except concurrent.futures.TimeoutError:
            pass
        return dict(st.login or {})

    async def _login(self, key: str, st: ServerState, how: str, client_id: str, ready) -> None:
        import httpx2

        def announce(**info):
            st.login = {"kind": how, **info}
            if not ready.done():
                ready.set_result(True)
        cb = None
        try:
            if how == "device":
                start = await auth.device_start(client_id)
                announce(user_code=start["user_code"], verification_uri=start["verification_uri"],
                         expires_in=start.get("expires_in", 900))
                self.open_url(start["verification_uri"])
                token = await auth.device_poll(client_id, start)
                self.tokens.put(st.cfg.url, kind="device", tokens={"access_token": token}, expires_at=None)
            else:
                cb = auth.Callback()
                redirect_uri = await cb.start()
                client = (self.tokens.get(st.cfg.url) or {}).get("client") or {}
                if client and redirect_uri not in client.get("redirect_uris", []):
                    self.tokens.drop(st.cfg.url, "client")      # 回调端口变了：重新注册客户端

                async def redirect(url: str) -> None:
                    announce(url=url)
                    self.open_url(url)
                provider = auth.oauth_provider(st.cfg.url, self.tokens, redirect_uri, redirect=redirect,
                                               callback=lambda: cb.wait())
                headers = {k: v for k, v in st.cfg.headers.items() if k.lower() != "authorization"}
                async with httpx2.AsyncClient(headers=headers, auth=provider, timeout=30) as c:
                    r = await c.post(st.cfg.url, json=auth.INIT, headers=auth.ACCEPT)
                if r.status_code in (401, 403):
                    raise RuntimeError(f"授权之后服务器仍然拒绝（HTTP {r.status_code}）")
            st.login, st.needs_login = None, False
            await self._restart(key, st)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            text = (str(e).strip().splitlines() or [""])[0] or type(e).__name__
            st.login = {"kind": how, "error": f"登录没完成：{text}"[:300]}
            st.needs_login = True
        finally:
            if cb is not None:
                cb.close()
            if not ready.done():
                ready.set_result(False)

    async def _restart(self, key: str, st: ServerState) -> None:
        """在事件循环里断开重连（登录完成后用）。"""
        if st.stop is not None:
            st.stop.set()
        if st.task is not None and not st.task.done():
            await asyncio.wait([st.task], timeout=5)
        st.failed_at = 0.0
        await self._connect(key, st)

    def reconnect(self, key: str) -> None:
        """断开（如果连着）再连，不受“刚失败过不重试”限制。不等连上。"""
        st = self.servers[key]

        async def stop():
            if st.stop is not None:
                st.stop.set()
            if st.task is not None and not st.task.done():
                await asyncio.wait([st.task], timeout=5)
        self._run(stop(), 10)
        st.failed_at = 0.0
        self.ensure([key], wait=False, retry=True)

    def start(self) -> None:
        """并行连接所有服务器；连不上的记下原因，不影响其他。"""
        self.ensure(list(self.servers), retry=True)

    async def _reap(self) -> None:
        """闲置太久的连接关掉（stdio 服务器的进程跟着退出）；工具清单留着，下次调用时再连。"""
        while True:
            await asyncio.sleep(min(30.0, self.idle))
            now = time.monotonic()
            for st in list(self.servers.values()):
                if st.client is not None and not st.busy and st.stop and now - st.last_used > self.idle:
                    st.stop.set()

    def close(self) -> None:
        async def stop_all():
            for st in self.servers.values():
                if st.login_task is not None and not st.login_task.done():
                    st.login_task.cancel()
                if st.stop:
                    st.stop.set()
            tasks = [st.task for st in self.servers.values() if st.task]
            if tasks:
                await asyncio.wait(tasks, timeout=5)
        if self._reaper is not None:
            self._reaper.cancel()
        try:
            self._run(stop_all(), 10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.loop.close()                     # 停下之后还要关掉，否则循环内部的 socket 会泄漏

    # ------------------------------------------------ 调用

    def _with_reconnect(self, name: str, make_coro):
        """连接活着时出错是真的错，直接抛；连接断了（服务器挂了）就重连一次再试。"""
        st = self.servers.get(name)
        if st is None:
            raise ValueError(f"没有名为 {name} 的 MCP 服务器")
        last: Exception | None = None
        with self._lock:
            st.busy += 1
            st.last_used = time.monotonic()
        try:
            for _ in range(2):
                if st.client is None:
                    self.ensure([name], retry=True)
                    if st.client is None:
                        raise RuntimeError(f"MCP 服务器 {name} 连不上：{st.error or '未知原因'}")
                try:
                    return self._run(make_coro(st.client), self.call_timeout + 10)
                except Exception as e:
                    last = e
                    if st.task is not None and not st.task.done():
                        raise
            raise last
        finally:
            with self._lock:
                st.busy -= 1
                st.last_used = time.monotonic()

    def call(self, server: str, tool: str, arguments: dict | None) -> tuple[str, bool]:
        try:
            result = self._with_reconnect(server, lambda c: c.call_tool(tool, arguments or {}))
        except Exception as e:
            return f"MCP 调用失败：{type(e).__name__}: {e}", True
        return _cap(_content_text(result)), bool(getattr(result, "is_error", False))

    def list_resources(self, server: str | None = None, labels: dict[str, str] | None = None) -> str:
        """labels：显示名 → key（常驻服务里一个任务只看得到它自己的那几个服务器）。"""
        lines = []
        for name, key in (labels or {k: k for k in self.servers}).items():
            st = self.servers[key]
            if (server and name != server) or not st.resources:
                continue
            try:
                res = self._with_reconnect(key, lambda c: c.list_resources())
            except Exception as e:
                lines.append(f"{name}：列资源失败（{e}）")
                continue
            for r in res.resources:
                desc = f" — {r.description}" if getattr(r, "description", None) else ""
                lines.append(f"{name}: {r.uri}（{r.name}）{desc}")
        return "\n".join(lines) or "没有资源。"

    def read_resource(self, server: str, uri: str) -> str:
        res = self._with_reconnect(server, lambda c: c.read_resource(uri))
        parts = [getattr(c, "text", None) or f"[二进制资源 {getattr(c, 'mime_type', '')}，未展开]" for c in res.contents]
        return _cap("\n".join(parts) or "(资源是空的)")
