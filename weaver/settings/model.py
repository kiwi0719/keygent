"""模型设置：地址、模型名、Key + 高级项。规则照 Keygent 的 ModelSettingsState（Model/ModelConfig.swift）。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ..errors import BadRequest
from ..providers import PRESETS
from .envfile import EnvFile

ADVANCED = {"context_window": "WEAVER_CONTEXT_WINDOW", "compact_at": "WEAVER_COMPACT_AT",
            "max_running": "WEAVER_MAX_RUNNING", "mcp_timeout": "WEAVER_MCP_TIMEOUT"}
DEFAULTS = {"context_window": "自动（按模型）", "compact_at": "自动", "max_running": "4", "mcp_timeout": "10"}
LABELS = {"context_window": "上下文窗口", "compact_at": "压缩阈值", "max_running": "同时跑几个任务",
          "mcp_timeout": "MCP 连接超时"}
LOCAL = ("localhost", "127.0.0.1", "::1")


def _host(url: str) -> str | None:
    try:
        return (urlparse(url.strip()).hostname or "").lower() or None
    except ValueError:
        return None


def _saved(env: EnvFile) -> tuple[str, str, str | None]:
    """(地址, 模型名, key)，兼容旧写法 OPENROUTER_*。"""
    legacy_key = env.get("OPENROUTER_API_KEY")
    provider = env.get("WEAVER_PROVIDER") or ("openrouter" if legacy_key else None)
    legacy = bool(provider and provider.startswith("openrouter"))
    url = env.get("WEAVER_BASE_URL") or (PRESETS.get(provider, {}).get("base_url", "") if provider else "")
    name = env.get("WEAVER_MODEL") or (env.get("OPENROUTER_MODEL") if legacy else None) or ""
    key = env.get("WEAVER_API_KEY") or (legacy_key if legacy else None)
    return url, name, key


def read(env_path: Path) -> dict:
    env = EnvFile.load(env_path)
    url, name, key = _saved(env)
    return {"url": url, "model": name, "key": ("••••" + key[-4:]) if key else None,
            "advanced": {k: env.get(v) or "" for k, v in ADVANCED.items()}, "defaults": dict(DEFAULTS)}


def save(env_path: Path, data: dict) -> None:
    """检查并写进 .env；不合格抛 BadRequest（给人看的原因）。"""
    env = EnvFile.load(env_path)
    saved_url, _, saved_key = _saved(env)
    url = str(data.get("url") or "").strip()
    name = str(data.get("model") or "").strip()
    typed = str(data.get("key") or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BadRequest("填一下接口地址" if not url else "地址要以 http:// 或 https:// 开头")
    if not name:
        raise BadRequest("填一下模型名")
    host = _host(url)
    reuse = saved_key is not None and host is not None and host == _host(saved_url)
    key = typed or (saved_key if reuse else None)
    local = host in LOCAL or (host or "").endswith(".local")
    if key is None and not local:
        raise BadRequest("填一下 API Key")
    advanced = data.get("advanced") or {}
    if not isinstance(advanced, dict):
        raise BadRequest("advanced 应该是一个对象")
    for k, v in advanced.items():
        if k not in ADVANCED:
            raise BadRequest(f"没有这个高级项：{k}")
        v = str(v or "").strip()
        if v and (not v.isdigit() or int(v) <= 0):
            raise BadRequest(f"{LABELS[k]}要填正整数，或者留空用默认")

    if url != saved_url:                    # 换了地址：让 weaver 按地址重新认是哪家
        env.set("WEAVER_PROVIDER", None)
        env.set("WEAVER_PROTOCOL", None)
    env.set("WEAVER_BASE_URL", url)
    env.set("WEAVER_MODEL", name)
    env.set("WEAVER_API_KEY", key)
    env.set("OPENROUTER_API_KEY", None)     # 旧写法已经并进 WEAVER_*，留着会让人以为改它有用
    env.set("OPENROUTER_MODEL", None)
    for k, v in advanced.items():
        env.set(ADVANCED[k], str(v or "").strip() or None)
    env.write(env_path)
