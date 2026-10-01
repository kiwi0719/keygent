"""MCP 远程服务器的登录。见 design/mcp2.md 第一节。

三条路：
- 支持自动注册客户端的服务器：浏览器授权（SDK 的 OAuthClientProvider；本机开一个只接一次的回调端口）
- GitHub + 本机 gh 已登录：配置写 "auth": "gh"，每次连接时现取 `gh auth token`
- GitHub 没有 gh：设备码（需要一个注册好的 GitHub OAuth App 的 Client ID）

令牌存在 ~/.weaver/mcp-tokens.json（0600），按服务器地址。平时连接只用已有的令牌（过期了先刷新）；
要重新授权时不弹浏览器，抛 NeedsLogin——只有用户按 ⌘L（或 weaver mcp login）才走授权。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..settings.envfile import write_private

CALLBACK_PORT = 33418            # 回调端口优先用它（注册客户端时写进 redirect_uri，固定下来才不用每次重新注册）
LOGIN_TIMEOUT = 600.0
INIT = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "weaver", "version": "0.1"}}}
ACCEPT = {"Accept": "application/json, text/event-stream"}

# GitHub 设备码（测试里换成假的）
GITHUB_DEVICE_CODE = "https://github.com/login/device/code"
GITHUB_DEVICE_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_SCOPES = "repo read:org read:user gist notifications project"
# 用户在自己 GitHub 账号下注册的 Weaver OAuth App（开启 Device Flow）。Client ID 是公开标识，不是密码。
# 还没注册时为空：GitHub 只能走 gh；也可以用环境变量 WEAVER_GITHUB_CLIENT_ID 指定。
GITHUB_CLIENT_ID = ""


def github_client_id() -> str:
    import os
    return os.environ.get("WEAVER_GITHUB_CLIENT_ID") or GITHUB_CLIENT_ID
DONE_PAGE = ("<!doctype html><meta charset=utf-8><title>Weaver</title>"
             "<body style='font:16px -apple-system;padding:48px'><h2>已登录</h2><p>可以关掉这一页，回到 Weaver。</p>")


class NeedsLogin(Exception):
    """要（重新）授权：平时连接时不弹浏览器，让用户去设置页按 ⌘L。"""

    def __init__(self, message: str = "要登录（设置 › MCP 里按 ⌘L）"):
        super().__init__(message)


def is_github(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "api.githubcopilot.com" or host.endswith(".githubcopilot.com")


# ------------------------------------------------ 令牌文件

class TokenFile:
    """{服务器地址: {"kind": "oauth"|"device", "tokens": {...}, "expires_at": 秒, "client": {...}, "saved": 秒}}"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, url: str) -> dict | None:
        with self._lock:
            return self._read().get(url)

    def put(self, url: str, **fields) -> None:
        with self._lock:
            data = self._read()
            data[url] = {**data.get(url, {}), **fields, "saved": time.time()}
            write_private(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def drop(self, url: str, *fields: str) -> None:
        """不给 fields：整条删掉；给了：只删这几项。"""
        with self._lock:
            data = self._read()
            if url not in data:
                return
            if fields:
                for f in fields:
                    data[url].pop(f, None)
            else:
                data.pop(url)
            write_private(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def bearer(self, url: str) -> str | None:
        """设备码登录拿到的令牌（直接放进请求头的那种）。"""
        rec = self.get(url) or {}
        return (rec.get("tokens") or {}).get("access_token") if rec.get("kind") == "device" else None


class FileTokenStorage:
    """SDK 的 TokenStorage：令牌、注册得到的客户端信息存进 TokenFile。另外记下绝对过期时间（SDK 只存 expires_in，
    重启后不知道令牌什么时候过期，过期了会直接要求重新授权而不是先刷新）。"""

    def __init__(self, tokens: TokenFile, url: str):
        self.tokens, self.url = tokens, url

    def expires_at(self) -> float | None:
        return (self.tokens.get(self.url) or {}).get("expires_at")

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        rec = self.tokens.get(self.url) or {}
        return OAuthToken.model_validate(rec["tokens"]) if rec.get("kind") == "oauth" and rec.get("tokens") else None

    async def set_tokens(self, tokens) -> None:
        exp = time.time() + tokens.expires_in if tokens.expires_in else None
        self.tokens.put(self.url, kind="oauth", tokens=tokens.model_dump(mode="json", exclude_none=True),
                        expires_at=exp)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        rec = self.tokens.get(self.url) or {}
        return OAuthClientInformationFull.model_validate(rec["client"]) if rec.get("client") else None

    async def set_client_info(self, client_info) -> None:
        self.tokens.put(self.url, client=client_info.model_dump(mode="json", exclude_none=True))


def _provider_class():
    from mcp.client.auth import OAuthClientProvider

    class Provider(OAuthClientProvider):
        """读回令牌时把过期时间也告诉 SDK（见 FileTokenStorage）。"""

        async def _initialize(self) -> None:
            await super()._initialize()
            exp = self.context.storage.expires_at()
            if exp and self.context.current_tokens:
                self.context.token_expiry_time = exp
    return Provider


async def _no_redirect(url: str) -> None:
    raise NeedsLogin()


async def _no_callback():
    raise NeedsLogin()


def oauth_provider(url: str, tokens: TokenFile, redirect_uri: str, redirect=None, callback=None):
    """redirect / callback 不给 = 平时连接：令牌不能用、刷新也不行时抛 NeedsLogin，不弹浏览器。"""
    from mcp.shared.auth import OAuthClientMetadata
    meta = OAuthClientMetadata(client_name="Weaver", redirect_uris=[redirect_uri],
                               grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                               token_endpoint_auth_method="none")
    return _provider_class()(server_url=url, client_metadata=meta, storage=FileTokenStorage(tokens, url),
                             redirect_handler=redirect or _no_redirect, callback_handler=callback or _no_callback)


# ------------------------------------------------ gh

def gh_path() -> str | None:
    return shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if Path(p).exists()),
                                      None)


def gh_token() -> str | None:
    """本机 gh 登录的令牌；没装、没登录、超时都返回 None。"""
    gh = gh_path()
    if not gh:
        return None
    try:
        r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    token = r.stdout.strip()
    return token if r.returncode == 0 and token else None


# ------------------------------------------------ 连接时用什么认证

def http_auth(cfg, tokens: TokenFile) -> tuple[dict, object | None]:
    """平时连接：返回 (请求头, httpx 的 auth)。"""
    headers = dict(cfg.headers)
    if str(cfg.raw.get("auth", "")) == "gh":
        token = gh_token()
        if not token:
            raise NeedsLogin("要登录：本机的 gh 没登录（终端里运行 gh auth login），或者在设置 › MCP 里按 ⌘L 换别的方式")
        headers["Authorization"] = f"Bearer {token}"
        return headers, None
    rec = tokens.get(cfg.url) or {}
    if rec.get("kind") == "device" and tokens.bearer(cfg.url):
        headers["Authorization"] = f"Bearer {tokens.bearer(cfg.url)}"
        return headers, None
    if rec.get("kind") == "oauth":
        client = rec.get("client") or {}
        redirect_uri = (client.get("redirect_uris") or [f"http://127.0.0.1:{CALLBACK_PORT}/callback"])[0]
        return headers, oauth_provider(cfg.url, tokens, redirect_uri)
    return headers, None


# ------------------------------------------------ 要不要登录、能怎么登录

async def probe_login(cfg) -> dict | None:
    """不带令牌连一次：401 且声明了授权信息 → {"auth_server", "registration", "device"}；否则 None。"""
    import httpx2
    try:
        async with httpx2.AsyncClient(timeout=8, headers={k: v for k, v in cfg.headers.items()
                                                          if k.lower() != "authorization"}) as c:
            r = await c.post(cfg.url, json=INIT, headers=ACCEPT)
            if r.status_code != 401:
                return None
            www = r.headers.get("www-authenticate", "")
            meta_url = None
            if "resource_metadata=" in www:
                meta_url = www.split("resource_metadata=", 1)[1].split(",")[0].strip().strip('"')
            if not meta_url:
                return {"auth_server": None, "registration": False, "device": False} if "bearer" in www.lower() else None
            prm = (await c.get(meta_url)).json()
            server = (prm.get("authorization_servers") or [None])[0]
            if not server:
                return {"auth_server": None, "registration": False, "device": False}
            p = urlparse(server)
            path = p.path.rstrip("/")
            asm = None
            for u in (f"{p.scheme}://{p.netloc}/.well-known/oauth-authorization-server{path}",
                      f"{p.scheme}://{p.netloc}/.well-known/openid-configuration{path}"):
                resp = await c.get(u)
                if resp.status_code == 200:
                    asm = resp.json()
                    break
            asm = asm or {}
            return {"auth_server": server, "registration": bool(asm.get("registration_endpoint")),
                    "device": bool(asm.get("device_authorization_endpoint"))}
    except Exception:
        return None


# ------------------------------------------------ 浏览器授权的回调

class Callback:
    """127.0.0.1 上只接一次的回调：浏览器跳回来时拿到 code 和 state。"""

    def __init__(self):
        self.server: asyncio.base_events.Server | None = None
        self.result: asyncio.Future | None = None
        self.port = 0

    async def start(self) -> str:
        from mcp.shared.auth import AuthorizationCodeResult
        self.result = asyncio.get_running_loop().create_future()

        async def handle(reader, writer):
            try:
                line = (await reader.readline()).decode("latin-1")
                while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
                target = line.split(" ")[1] if len(line.split(" ")) > 1 else "/"
                q = parse_qs(urlparse(target).query)
                ok = urlparse(target).path == "/callback" and ("code" in q or "error" in q)
                body = (DONE_PAGE if "code" in q else
                        f"<!doctype html><meta charset=utf-8><p>登录没完成：{q.get('error_description', q.get('error', ['?']))[0]}")
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
                             f"{len(body.encode())}\r\nConnection: close\r\n\r\n{body}".encode())
                await writer.drain()
                if ok and not self.result.done():
                    if "code" in q:
                        self.result.set_result(AuthorizationCodeResult(
                            code=q["code"][0], state=(q.get("state") or [None])[0], iss=(q.get("iss") or [None])[0]))
                    else:
                        self.result.set_exception(RuntimeError(f"授权被拒绝：{q['error'][0]}"))
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        for port in (CALLBACK_PORT, 0):
            try:
                self.server = await asyncio.start_server(handle, "127.0.0.1", port)
                break
            except OSError:
                continue
        self.port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}/callback"

    async def wait(self, timeout: float = LOGIN_TIMEOUT):
        try:
            return await asyncio.wait_for(asyncio.shield(self.result), timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"等了 {int(timeout // 60)} 分钟没有完成授权") from None

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None


# ------------------------------------------------ 设备码（GitHub）

async def device_start(client_id: str) -> dict:
    import httpx2
    async with httpx2.AsyncClient(timeout=15) as c:
        r = await c.post(GITHUB_DEVICE_CODE, data={"client_id": client_id, "scope": GITHUB_SCOPES},
                         headers={"Accept": "application/json"})
        data = r.json()
    if "device_code" not in data:
        raise RuntimeError(f"GitHub 没给设备码：{data.get('error_description') or data.get('error') or r.status_code}")
    return data


async def device_poll(client_id: str, start: dict) -> str:
    """一直问到拿到令牌；用户拒绝、码过期抛 RuntimeError。"""
    import httpx2
    interval = float(start.get("interval", 5))
    deadline = time.monotonic() + float(start.get("expires_in", 900))
    async with httpx2.AsyncClient(timeout=15) as c:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            r = await c.post(GITHUB_DEVICE_TOKEN, headers={"Accept": "application/json"},
                             data={"client_id": client_id, "device_code": start["device_code"],
                                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            data = r.json()
            if data.get("access_token"):
                return data["access_token"]
            err = data.get("error")
            if err == "slow_down":
                interval += 5
            elif err != "authorization_pending":
                raise RuntimeError({"access_denied": "你在 GitHub 上拒绝了授权",
                                    "expired_token": "码过期了，再按一次 ⌘L"}.get(err, f"GitHub 登录失败：{err}"))
    raise RuntimeError("码过期了，再按一次 ⌘L")
