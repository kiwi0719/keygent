"""HTTP 接口 + SSE 事件流。字段和示例见 design/api.md，结构见 design/daemon.md 第六节。

标准库 ThreadingHTTPServer：每个请求一个线程，SSE 连接一直挂着。只绑 127.0.0.1，每个请求带 Bearer token。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .events import EventBus
from .manager import BadRequest, Conflict, NotFound, TaskManager
from .uploads import MAX_SIZE

log = logging.getLogger("weaverd")
VERSION = "0.1"
MAX_JSON = 1024 * 1024
PING = 15.0


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


ERRORS = {BadRequest: (400, "bad_request"), NotFound: (404, "not_found"), Conflict: (409, "conflict")}


class Handler(BaseHTTPRequestHandler):
    server: "DaemonServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):            # 不往 stderr 打访问日志
        log.debug("%s " + fmt, self.address_string(), *args)

    # ------------------------------------------------ 分发

    ROUTES = [
        ("GET", r"/v1/status", "status"),
        ("GET", r"/v1/tasks", "list_tasks"),
        ("POST", r"/v1/tasks", "create_task"),
        ("GET", r"/v1/tasks/([\w-]+)", "get_task"),
        ("PATCH", r"/v1/tasks/([\w-]+)", "rename_task"),
        ("DELETE", r"/v1/tasks/([\w-]+)", "archive_task"),
        ("POST", r"/v1/tasks/([\w-]+)/input", "input"),
        ("POST", r"/v1/tasks/([\w-]+)/cancel", "cancel"),
        ("GET", r"/v1/tasks/([\w-]+)/changes", "file_diff"),
        ("POST", r"/v1/tasks/([\w-]+)/undo", "undo"),
        ("GET", r"/v1/tasks/([\w-]+)/agents/([\w-]+)", "agent"),
        ("POST", r"/v1/tasks/([\w-]+)/restore", "restore"),
        ("GET", r"/v1/waits", "list_waits"),
        ("POST", r"/v1/waits/([\w-]+)", "answer"),
        ("POST", r"/v1/blobs", "upload"),
        ("GET", r"/v1/blobs/([0-9a-f]{64})", "blob"),
        ("GET", r"/v1/prompts", "prompts"),
        ("GET", r"/v1/events", "events"),
        ("GET", r"/v1/search", "search"),
        # 设置页（design/settings.md）。具体路径排在 /mcp/{名字} 前面
        ("GET", r"/v1/settings/model", "settings_model"),
        ("PUT", r"/v1/settings/model", "settings_save_model"),
        ("GET", r"/v1/settings/mcp", "settings_mcp"),
        ("POST", r"/v1/settings/mcp", "settings_mcp_add"),
        ("POST", r"/v1/settings/mcp/([^/]+)/reconnect", "settings_mcp_reconnect"),
        ("POST", r"/v1/settings/mcp/([^/]+)/login", "settings_mcp_login"),
        ("POST", r"/v1/settings/mcp/parse", "settings_mcp_parse"),
        ("GET", r"/v1/settings/mcp/presets", "settings_mcp_presets"),
        ("POST", r"/v1/settings/mcp/presets/([^/]+)", "settings_mcp_add_preset"),
        ("GET", r"/v1/settings/mcp/import", "settings_mcp_import"),
        ("PUT", r"/v1/settings/mcp/([^/]+)", "settings_mcp_replace"),
        ("DELETE", r"/v1/settings/mcp/([^/]+)", "settings_mcp_remove"),
        ("GET", r"/v1/settings/skills", "settings_skills"),
        ("POST", r"/v1/settings/skills", "settings_skill_create"),
        ("GET", r"/v1/settings/skills/([^/]+)", "settings_skill"),
        ("PUT", r"/v1/settings/skills/([^/]+)", "settings_skill_save"),
        ("DELETE", r"/v1/settings/skills/([^/]+)", "settings_skill_remove"),
        ("GET", r"/v1/settings/permissions", "settings_permissions"),
        ("DELETE", r"/v1/settings/permissions/rules", "settings_revoke_rule"),
        ("DELETE", r"/v1/settings/permissions/projects", "settings_forget_project"),
        ("GET", r"/v1/settings/memory", "settings_memory"),
        ("POST", r"/v1/settings/memory", "settings_memory_create"),
        ("GET", r"/v1/settings/memory/([^/]+)", "settings_memory_read"),
        ("PUT", r"/v1/settings/memory/([^/]+)", "settings_memory_save"),
        ("DELETE", r"/v1/settings/memory/([^/]+)", "settings_memory_remove"),
    ]

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        self.query = parse_qs(url.query)
        try:
            self._auth()
            matched = False
            for m, pattern, name in self.ROUTES:
                hit = re.fullmatch(pattern, url.path)
                if hit:
                    matched = True
                    if m == method:
                        return getattr(self, "h_" + name)(*hit.groups())
            if matched:
                raise HttpError(405, "bad_request", f"不支持 {method} {url.path}")
            raise HttpError(404, "not_found", f"没有这个接口：{url.path}")
        except HttpError as e:
            self._error(e.status, e.code, e.message)
        except tuple(ERRORS) as e:
            status, code = next(v for k, v in ERRORS.items() if isinstance(e, k))   # 子类（如 BrokenConfig）也认
            self._error(status, code, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.exception("处理 %s %r 出错", method, self.path)          # %r：路径里的控制字符不进日志
            self._error(500, "internal", f"{type(e).__name__}: {(str(e).splitlines() or [''])[0]}")

    def _auth(self) -> None:
        got = self.headers.get("Authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {self.server.token}".encode()):
            self._drain()
            raise HttpError(401, "unauthorized", "token 不对或没带")

    # ------------------------------------------------ 读写

    def _drain(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        if 0 < n <= MAX_SIZE:
            self.rfile.read(n)
        elif n:
            self.close_connection = True

    def _body(self, limit: int = MAX_JSON) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.close_connection = True
            raise HttpError(413, "too_large", f"请求太大（上限 {limit // 1024 // 1024} MB）")
        return self.rfile.read(n) if n else b""

    def _json(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise HttpError(400, "bad_request", "请求体不是合法的 JSON") from None
        if not isinstance(data, dict):
            raise HttpError(400, "bad_request", "请求体应该是一个 JSON 对象")
        return data

    def _send(self, status: int, data=None) -> None:
        body = b"" if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if data is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        try:
            self._send(status, {"error": {"code": code, "message": message}})
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _str(data: dict, key: str, required: bool = False) -> str:
        v = data.get(key)
        if v is None and not required:
            return ""
        if not isinstance(v, str):
            raise HttpError(400, "bad_request", f"{key} 应该是字符串")
        return v

    @staticmethod
    def _ids(data: dict) -> list[str]:
        v = data.get("attachments") or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise HttpError(400, "bad_request", "attachments 应该是附件 id 的列表")
        return v

    # ------------------------------------------------ 接口

    @property
    def mgr(self) -> TaskManager:
        return self.server.manager

    def h_status(self):
        self._send(200, {**self.mgr.status(), "version": VERSION})

    def _flag(self, key: str) -> bool:
        return (self.query.get(key) or ["0"])[0] in ("1", "true")

    def h_list_tasks(self):
        self._send(200, {"tasks": self.mgr.archived() if self._flag("archived") else self.mgr.list()})

    def h_create_task(self):
        d = self._json()
        opts = {k: d[k] for k in ("max_steps", "token_budget") if isinstance(d.get(k), int) and d[k] > 0}
        if d.get("prompt") is not None:              # MCP prompt 代替 text（启动器里 / 选的，design/mcp2.md 第四节）
            self._send(201, self.mgr.create_from_prompt(self._obj(d, "prompt"), workdir=self._str(d, "workdir") or None,
                                                        **opts))
            return
        self._send(201, self.mgr.create(self._str(d, "text"), workdir=self._str(d, "workdir") or None,
                                        attachments=self._ids(d), **opts))

    def h_prompts(self):
        workdir = (self.query.get("workdir") or [""])[0] or None
        self._send(200, {"prompts": self.mgr.prompts(workdir)})

    def h_blob(self, digest):
        """工具结果里的图片（步骤的 images），App 取来显示缩略图。只给 BlobStore 里有的。"""
        blobs = self.mgr.blobs
        try:
            data = blobs.get("blob://sha256-" + digest) if blobs is not None else None
        except OSError:
            data = None
        if data is None:
            raise HttpError(404, "not_found", "没有这个文件")
        self.send_response(200)
        mime = (self.query.get("mime") or [""])[0]
        self.send_header("Content-Type", mime if re.fullmatch(r"image/[a-z0-9.+-]+", mime) else "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def h_get_task(self, task_id):
        self._send(200, self.mgr.archived_detail(task_id) if self._flag("archived") else self.mgr.detail(task_id))

    def h_file_diff(self, task_id):
        path = (self.query.get("path") or [""])[0]
        if not path:
            raise HttpError(400, "bad_request", "path 不能为空")
        self._send(200, self.mgr.file_diff(task_id, path, archived=self._flag("archived")))

    def h_undo(self, task_id):
        self._send(200, {"changes": self.mgr.undo(task_id, self._str(self._json(), "path", required=True))})

    def h_agent(self, task_id, sub):
        self._send(200, self.mgr.agent(task_id, sub, archived=self._flag("archived")))

    def h_restore(self, task_id):
        self._body()
        self._send(200, self.mgr.restore(task_id))

    def h_rename_task(self, task_id):
        self._send(200, self.mgr.rename(task_id, self._str(self._json(), "title", required=True)))

    def h_archive_task(self, task_id):
        self.mgr.archive(task_id)
        self._send(204)

    def h_input(self, task_id):
        d = self._json()
        self._send(202, self.mgr.input(task_id, self._str(d, "text"), attachments=self._ids(d)))

    def h_cancel(self, task_id):
        self._body()
        self._send(202, self.mgr.cancel(task_id))

    def h_list_waits(self):
        self._send(200, {"waits": self.mgr.waits()})

    def h_answer(self, wait_id):
        d = self._json()
        args, values = d.get("args"), d.get("values")
        if args is not None and not isinstance(args, dict):
            raise HttpError(400, "bad_request", "args 应该是一个对象")
        if values is not None and not isinstance(values, dict):
            raise HttpError(400, "bad_request", "values 应该是一个对象")
        self._send(200, self.mgr.answer(wait_id, self._str(d, "decision", required=True), args=args,
                                        note=self._str(d, "note"), values=values))

    def h_upload(self):
        if self.server.manager.uploads is None:
            raise HttpError(400, "bad_request", "这个服务不支持附件")
        data = self._body(MAX_SIZE)
        name = unquote(self.headers.get("X-Filename") or "附件")
        meta = self.mgr.uploads.put(data, name, self.headers.get("Content-Type") or "")
        self._send(201, {k: meta[k] for k in ("id", "name", "mime", "size")})

    def h_search(self):
        corpus = getattr(self.mgr, "corpus", None)
        if corpus is None:
            raise HttpError(400, "bad_request", "这个服务不支持搜索")
        q = (self.query.get("q") or [""])[0]
        if not q.strip():
            raise HttpError(400, "bad_request", "q 不能为空")
        archived = (self.query.get("archived") or ["0"])[0] in ("1", "true")
        limit = min(int((self.query.get("limit") or ["20"])[0] or 20), 100)
        self._send(200, {"results": corpus.search(q, archived=archived, limit=limit)})

    # ------------------------------------------------ 设置页

    @property
    def settings(self):
        if self.server.settings is None:
            raise HttpError(404, "not_found", "这个服务没开设置接口")
        return self.server.settings

    @staticmethod
    def _obj(data: dict, key: str) -> dict:
        v = data.get(key)
        if not isinstance(v, dict):
            raise HttpError(400, "bad_request", f"{key} 应该是一个对象")
        return v

    def h_settings_model(self):
        self._send(200, self.settings.model())

    def h_settings_save_model(self):
        self._send(200, self.settings.save_model(self._json()))

    def h_settings_mcp(self):
        self._send(200, self.settings.mcp_list())

    def h_settings_mcp_add(self):
        data = self._json()
        added = self.settings.mcp_add(self._obj(data, "servers"), bool(data.get("overwrite")))
        self._send(201, {"added": added})

    def h_settings_mcp_parse(self):
        self._send(200, self.settings.mcp_parse(self._str(self._json(), "text", required=True)))

    def h_settings_mcp_presets(self):
        self._send(200, {"presets": self.settings.mcp_presets()})

    def h_settings_mcp_add_preset(self, preset_id):
        data = self._json()
        values = data.get("values") or {}
        if not isinstance(values, dict):
            raise HttpError(400, "bad_request", "values 应该是一个对象")
        self._send(201, {"added": [self.settings.mcp_add_preset(unquote(preset_id), values)]})

    def h_settings_mcp_import(self):
        self._send(200, {"sources": self.settings.mcp_import()})

    def h_settings_mcp_replace(self, name):
        data = self._json()
        rename = self._str(data, "rename") or None
        self._send(200, {"name": self.settings.mcp_replace(unquote(name), self._obj(data, "config"), rename)})

    def h_settings_mcp_remove(self, name):
        self.settings.mcp_remove(unquote(name))
        self._send(204)

    def h_settings_mcp_reconnect(self, name):
        self.settings.mcp_reconnect(unquote(name))
        self._send(204)

    def h_settings_mcp_login(self, name):
        self._send(200, self.settings.mcp_login(unquote(name)))

    def h_settings_skills(self):
        self._send(200, self.settings.skills())

    def h_settings_skill_create(self):
        self._send(201, {"name": self.settings.skill_create(self._str(self._json(), "text", required=True))})

    def h_settings_skill(self, name):
        self._send(200, self.settings.skill(unquote(name)))

    def h_settings_skill_save(self, name):
        text = self._str(self._json(), "text", required=True)
        self._send(200, {"name": self.settings.skill_save(unquote(name), text)})

    def h_settings_skill_remove(self, name):
        self.settings.skill_remove(unquote(name))
        self._send(204)

    # ------------------------------------------------ 设置 › 权限、记忆

    def h_settings_permissions(self):
        from ..settings.permits import BUILTIN
        self._send(200, {"rules": self.mgr.always_rules(), "projects": self.settings.permission_projects(),
                         "builtin": BUILTIN})

    def h_settings_revoke_rule(self):
        d = self._json()
        self.mgr.revoke_rule(self._str(d, "task", required=True), self._str(d, "key", required=True))
        self._send(204)

    def h_settings_forget_project(self):
        self.settings.forget_project(self._str(self._json(), "root", required=True))
        self._send(204)

    def _roots(self) -> list[str]:
        """记忆页列哪些项目：所有任务（含归档）的工作目录，最近的在前。"""
        tasks = self.mgr.list() + self.mgr.archived()
        return [t["workdir"] for t in sorted(tasks, key=lambda t: -t["updated"])]

    def _scope(self) -> tuple[str, str]:
        return (self.query.get("scope") or ["user"])[0], (self.query.get("root") or [""])[0]

    def h_settings_memory(self):
        self._send(200, self.settings.memory(self._roots()))

    def h_settings_memory_create(self):
        d = self._json()
        scope, root = self._str(d, "scope") or "user", self._str(d, "root")
        self._send(201, {"name": self.settings.memory_save(scope, root, None, self._str(d, "text", required=True))})

    def h_settings_memory_read(self, name):
        self._send(200, self.settings.memory_read(*self._scope(), unquote(name)))

    def h_settings_memory_save(self, name):
        text = self._str(self._json(), "text", required=True)
        self._send(200, {"name": self.settings.memory_save(*self._scope(), unquote(name), text)})

    def h_settings_memory_remove(self, name):
        self.settings.memory_remove(*self._scope(), unquote(name))
        self._send(204)

    def h_events(self):
        raw = (self.query.get("after") or [self.headers.get("Last-Event-ID") or ""])[0]
        try:
            after = int(raw) if raw != "" else None
        except ValueError:
            raise HttpError(400, "bad_request", "after 应该是整数") from None
        bus = self.server.bus
        sub, backlog, reset = bus.subscribe(after)
        self.close_connection = True
        app = (self.headers.get("X-Weaver-Client") or "").lower() != "cli"   # 命令行不负责系统提醒
        self.server.client_joined(app, +1)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            last = after or 0
            if reset:
                self._sse(None, "reset", {})
                last = bus.cursor
            for cur, kind, data in backlog:
                self._sse(cur, kind, data)
                last = cur
            while not self.server.stopping.is_set():
                try:
                    item = sub.q.get(timeout=PING)
                except queue.Empty:
                    self._sse(None, "ping", {})
                    continue
                if item is None or sub.dropped:
                    break                           # 被断开（太慢 / 服务关闭）：客户端重连补齐
                cur, kind, data = item
                if cur is not None and cur <= last:
                    continue                        # 补发里已经发过了
                self._sse(cur, kind, data)
                if cur is not None:
                    last = cur
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(sub)
            self.server.client_joined(app, -1)

    def _sse(self, cursor, kind: str, data: dict) -> None:
        head = f"id: {cursor}\n" if cursor is not None else ""
        self.wfile.write(f"{head}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()


class DaemonServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, manager: TaskManager, port: int = 0, token: str | None = None, settings=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.manager = manager
        self.settings = settings                  # weaver.settings.service.Settings；None 时设置接口回 404
        self.bus = EventBus(manager)
        self.token = token or "wv_" + secrets.token_urlsafe(32)
        self.stopping = threading.Event()
        self._apps = 0
        self._apps_lock = threading.Lock()

    def client_joined(self, app: bool, delta: int) -> None:
        if app:
            with self._apps_lock:
                self._apps += delta

    @property
    def app_clients(self) -> int:
        """连着事件流的 App（命令行不算）：有的话系统通知由它来发。"""
        return self._apps

    @property
    def port(self) -> int:
        return self.server_address[1]

    def write_info(self, path: str | Path) -> Path:
        """端口和 token 写进 daemon.json：先建成 0600 再写，任何时候别人都读不到。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid(), "version": VERSION,
                       "started": time.time()}, f)
        os.replace(tmp, p)
        return p

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, name="weaverd-http", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self.stopping.set()
        self.bus.close()
        self.shutdown()
        self.server_close()
