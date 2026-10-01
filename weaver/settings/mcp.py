"""用户级 MCP 服务器（~/.weaver/mcp.json）：增删改、粘贴解析、从 Claude 导入、常用服务器。

只认 mcpServers 这一层；文件里别的顶层字段、每个服务器里我们不认识的字段原样保留。
删除 = 移进 mcp-archive.json。两个文件都可能有令牌，一律 0600。
加锁由调用方（service.Settings）负责。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from ..errors import BadRequest, Conflict, NotFound
from .envfile import EnvFile, write_private

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PRESETS = Path(__file__).with_name("presets.json")


class BrokenConfig(Conflict):
    """mcp.json 不是合法 JSON：不覆盖用户手写的内容，让他先修好。"""


def _file(home: Path) -> Path:
    return Path(home) / "mcp.json"


def _read(home: Path) -> dict:
    f = _file(home)
    try:
        text = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        where = f"第 {e.lineno} 行 第 {e.colno} 列 " if hasattr(e, "lineno") else ""
        raise BrokenConfig(f"mcp.json 读不了：{where}{getattr(e, 'msg', e)}。先手动修好它（{f}）") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise BrokenConfig(f"mcp.json 读不了：最外层应该是一个对象，mcpServers 也是（{f}）")
    return data


def _write(home: Path, data: dict) -> None:
    write_private(_file(home), json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load(home: Path) -> dict[str, dict]:
    return dict(_read(home).get("mcpServers") or {})


def _check(name: str, config) -> None:
    if not NAME.match(str(name)):
        raise BadRequest(f"服务器名字“{name}”不行：只能用字母、数字、_ 和 -，最长 64 个字")
    if not isinstance(config, dict):
        raise BadRequest(f"{name}：配置应该是一个对象")
    if not config.get("command") and not config.get("url"):
        raise BadRequest(f"{name}：缺少 command（本地服务器）或 url（远程服务器）")


def add(home: Path, servers: dict, overwrite: bool = False) -> list[str]:
    if not isinstance(servers, dict) or not servers:
        raise BadRequest("servers 应该是 {名字: 配置}，至少一个")
    for name, config in servers.items():
        _check(name, config)
    data = _read(home)
    current = data.setdefault("mcpServers", {})
    same = [n for n in servers if n in current]
    if same and not overwrite:
        raise Conflict(f"已经有同名的服务器：{'、'.join(same)}")
    current.update(servers)
    _write(home, data)
    return list(servers)


def replace(home: Path, name: str, config: dict, rename: str | None = None) -> str:
    data = _read(home)
    current = data.setdefault("mcpServers", {})
    if name not in current:
        raise NotFound(f"没有叫 {name} 的 MCP 服务器")
    new = (rename or name).strip()
    _check(new, config)
    if new != name and new in current:
        raise Conflict(f"已经有同名的服务器：{new}")
    # 改名时保持原来的位置（列表顺序不跳）
    data["mcpServers"] = {(new if k == name else k): (config if k == name else v) for k, v in current.items()}
    _write(home, data)
    return new


def remove(home: Path, name: str) -> None:
    data = _read(home)
    current = data.setdefault("mcpServers", {})
    if name not in current:
        raise NotFound(f"没有叫 {name} 的 MCP 服务器")
    archive = Path(home) / "mcp-archive.json"
    try:
        old = json.loads(archive.read_text(encoding="utf-8"))
        if not isinstance(old, list):
            old = []
    except (OSError, ValueError):
        old = []
    old.append({"name": name, "config": current.pop(name), "archived": time.time()})
    write_private(archive, json.dumps(old, ensure_ascii=False, indent=2) + "\n")
    _write(home, data)


# ------------------------------------------------ 粘贴解析

def _is_server(v) -> bool:
    return isinstance(v, dict) and bool(v.get("command") or v.get("url"))


def guess_name(config: dict) -> str:
    """单个服务器没有名字时猜一个：远程按域名，本地按包名或命令名。"""
    if config.get("url"):
        labels = (urlparse(str(config["url"])).hostname or "server").split(".")
        while len(labels) > 2 and labels[0] in ("api", "mcp", "www"):
            labels = labels[1:]
        name = labels[0]
    else:
        name = ""
        for a in reversed([str(a) for a in config.get("args") or []]):
            if a.startswith("-") or a.startswith((".", "/", "~")) or not re.match(r"^[@\w][\w@/.\-]*$", a):
                continue
            pkg = re.sub(r"(?<=.)@[^/]*$", "", a)                      # 去掉 @版本
            scope, _, base = pkg.rpartition("/")
            base = re.sub(r"\.(py|js|mjs|ts)$", "", base)
            base = re.sub(r"^(mcp-server-|server-)|(-mcp-server|-mcp|-server)$", "", base)
            name = scope.lstrip("@") if base in ("mcp", "server", "") and scope else base
            break
        name = name or Path(str(config.get("command") or "server")).name
    return re.sub(r"[^A-Za-z0-9_-]", "-", name).strip("-_")[:64] or "server"


def parse_text(text: str) -> dict[str, dict]:
    """认三种写法：{"mcpServers": {…}}（还有 VS Code 的 {"servers": {…}}）、{名字: 配置}、单个服务器。"""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise BadRequest(f"不是合法的 JSON：{getattr(e, 'msg', e)}") from None
    if isinstance(data, dict):
        for key in ("mcpServers", "servers"):
            if isinstance(data.get(key), dict) and data[key] and all(_is_server(v) for v in data[key].values()):
                return dict(data[key])
        if _is_server(data):
            return {guess_name(data): data}
        if data and all(_is_server(v) for v in data.values()):
            return dict(data)
    raise BadRequest("没认出 MCP 服务器配置：要有 command（本地服务器）或 url（远程服务器）")


# ------------------------------------------------ 从 Claude 导入

def import_sources(claude_json: Path, desktop_json: Path) -> list[dict]:
    out = []
    for label, path in (("Claude Code", claude_json), ("Claude 桌面版", desktop_json)):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = {k: v for k, v in (data.get("mcpServers") or {}).items() if _is_server(v)} \
            if isinstance(data, dict) else {}
        if servers:
            out.append({"from": label, "path": str(path), "servers": servers})
    return out


# ------------------------------------------------ 常用服务器

def presets() -> list[dict]:
    return json.loads(PRESETS.read_text(encoding="utf-8"))


def add_preset(home: Path, preset_id: str, values: dict) -> str:
    p = next((p for p in presets() if p["id"] == preset_id), None)
    if p is None:
        raise NotFound(f"没有这个常用服务器：{preset_id}")
    values = {k: str(v).strip() for k, v in (values or {}).items()}
    missing = [n["label"] for n in p["needs"] if not values.get(n["id"])]
    if missing:
        raise BadRequest(f"要填：{'、'.join(missing)}")
    if p["name"] in load(home):
        raise Conflict(f"已经有同名的服务器：{p['name']}")
    config = json.loads(json.dumps(p["config"]))
    env_needs = [n for n in p["needs"] if n["kind"] == "env"]
    for n in p["needs"]:
        if n["kind"] == "arg":
            config.setdefault("args", []).append(str(Path(values[n["id"]]).expanduser()))
    if env_needs:                     # 令牌写进 .env（配置里只写 ${变量名}），本进程也立刻生效
        env_path = Path(home) / ".env"
        env = EnvFile.load(env_path)
        for n in env_needs:
            env.set(n["id"], values[n["id"]])
            os.environ[n["id"]] = values[n["id"]]
        env.write(env_path)
    add(home, {p["name"]: config})
    return p["name"]
