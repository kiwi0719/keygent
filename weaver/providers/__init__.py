"""模型接入层：按协议分适配器，按预设选地址和认证。见 design/providers.md。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .base import Accumulator, ModelError, StreamingModel
from .anthropic import AnthropicModel, AnthropicQuirks
from .openai_chat import OpenAIChatModel, Quirks

# 预设：协议 + 地址 + 方言差异。标了“未验证”的方言设置是按文档推断的，没用真实 key 跑过。
PRESETS: dict[str, dict] = {
    "openrouter": {"protocol": "openai_chat", "base_url": "https://openrouter.ai/api/v1",
                   "quirks": {"echo_reasoning": {"openrouter"}, "cache_marks": "claude"}},
    "openai":     {"protocol": "openai_chat", "base_url": "https://api.openai.com/v1",
                   "quirks": {"max_tokens_field": "max_completion_tokens", "prompt_cache_key": True}},
    # 未验证：思考模式下带工具调用时，本轮的 reasoning_content 需要回传
    "deepseek":   {"protocol": "openai_chat", "base_url": "https://api.deepseek.com/v1",
                   "quirks": {"echo_reasoning": {"reasoning_content"}, "echo_reasoning_scope": "turn"}},
    "kimi":       {"protocol": "openai_chat", "base_url": "https://api.moonshot.cn/v1",
                   "quirks": {"echo_reasoning": {"reasoning_content"}, "echo_reasoning_scope": "turn"}},
    "qwen":       {"protocol": "openai_chat", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "glm":        {"protocol": "openai_chat", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "siliconflow": {"protocol": "openai_chat", "base_url": "https://api.siliconflow.cn/v1"},
    "groq":       {"protocol": "openai_chat", "base_url": "https://api.groq.com/openai/v1"},
    "together":   {"protocol": "openai_chat", "base_url": "https://api.together.xyz/v1"},
    "xai":        {"protocol": "openai_chat", "base_url": "https://api.x.ai/v1"},
    "gemini":     {"protocol": "openai_chat", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "ollama":     {"protocol": "openai_chat", "base_url": "http://localhost:11434/v1", "keyless": True},
    "lmstudio":   {"protocol": "openai_chat", "base_url": "http://localhost:1234/v1", "keyless": True},
    "vllm":       {"protocol": "openai_chat", "base_url": "http://localhost:8000/v1", "keyless": True},
    # Anthropic 协议的地址不带 /v1，请求时拼 /v1/messages（和官方 SDK 的 base_url 写法一致）
    "anthropic":  {"protocol": "anthropic_messages", "base_url": "https://api.anthropic.com"},
    # 已验证：OpenRouter 的 Anthropic 兼容端点，用 OpenRouter 的 key，Bearer 认证
    "openrouter-anthropic": {"protocol": "anthropic_messages", "base_url": "https://openrouter.ai/api",
                             "quirks": {"auth": "bearer"}},
    # 未验证：以下为各家文档里的 Anthropic 兼容端点
    "deepseek-anthropic": {"protocol": "anthropic_messages", "base_url": "https://api.deepseek.com/anthropic"},
    "kimi-anthropic":     {"protocol": "anthropic_messages", "base_url": "https://api.moonshot.cn/anthropic"},
}
PROTOCOLS = {"openai_chat": OpenAIChatModel, "anthropic_messages": AnthropicModel}


_ENDPOINTS = ("/chat/completions", "/v1/messages", "/messages")


def guess_provider(url: str) -> tuple[str, str, str]:
    """只给了地址时，认出是哪家预设（好带上它的方言设置）。返回 (预设名, base_url, 协议)。

    用户可能贴的是完整接口地址（…/chat/completions、…/v1/messages），先去掉。
    同一家有两种协议时（OpenRouter、DeepSeek、Kimi），路径里带 anthropic 的算 Anthropic 协议。
    认出了就用预设的地址（预设是对的，用户写的可能少了 /v1）；认不出就是 custom，原样用用户的地址。
    """
    u = url.strip().rstrip("/")
    for suffix in _ENDPOINTS:
        if u.endswith(suffix):
            u = u[: -len(suffix)].rstrip("/")
            break
    anthropic = "anthropic" in u.lower()
    if anthropic and u.endswith("/v1"):          # Anthropic 协议请求时自己拼 /v1/messages
        u = u[:-3]
    netloc = urlparse(u).netloc.lower()
    for name, p in PRESETS.items():
        if p["base_url"] == u:
            return name, u, p["protocol"]
    for name, p in PRESETS.items():
        if urlparse(p["base_url"]).netloc == netloc and (p["protocol"] == "anthropic_messages") == anthropic:
            return name, p["base_url"], p["protocol"]
    return "custom", u, "anthropic_messages" if anthropic else "openai_chat"


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _anthropic_quirks(base: dict, env: dict) -> AnthropicQuirks:
    q = dict(base)
    if env.get("WEAVER_AUTH"):
        q["auth"] = env["WEAVER_AUTH"]
    if env.get("WEAVER_ANTHROPIC_VERSION"):
        q["version"] = env["WEAVER_ANTHROPIC_VERSION"]
    if env.get("WEAVER_CACHE"):
        q["cache"] = env["WEAVER_CACHE"].lower() not in ("0", "false", "no")
    if env.get("WEAVER_THINKING_BUDGET"):
        q["thinking_budget"] = int(env["WEAVER_THINKING_BUDGET"])
    return AnthropicQuirks(**q)


def _quirks(base: dict, env: dict) -> Quirks:
    """预设的方言设置，再用环境变量覆盖。"""
    q = dict(base)
    if env.get("WEAVER_MAX_TOKENS_FIELD"):
        q["max_tokens_field"] = env["WEAVER_MAX_TOKENS_FIELD"]
    if env.get("WEAVER_STREAM_USAGE"):
        q["stream_usage"] = env["WEAVER_STREAM_USAGE"].lower() not in ("0", "false", "no")
    if "WEAVER_ECHO_REASONING" in env:
        q["echo_reasoning"] = {x.strip() for x in env["WEAVER_ECHO_REASONING"].split(",") if x.strip()}
    if env.get("WEAVER_ECHO_SCOPE"):
        q["echo_reasoning_scope"] = env["WEAVER_ECHO_SCOPE"]
    if env.get("WEAVER_CACHE_MARKS"):
        q["cache_marks"] = env["WEAVER_CACHE_MARKS"]
    if env.get("WEAVER_THINK_TAGS"):
        q["think_tags"] = env["WEAVER_THINK_TAGS"].lower() not in ("0", "false", "no")
    return Quirks(**q)


def make_model(env: dict | None = None, blobs=None, **kwargs) -> StreamingModel:
    """按配置造出模型。兼容旧配置：只有 OPENROUTER_* 时当作 openrouter 预设。"""
    env = dict(os.environ if env is None else env)
    provider = env.get("WEAVER_PROVIDER") or ("openrouter" if env.get("OPENROUTER_API_KEY") else "")
    if not provider and env.get("WEAVER_BASE_URL"):
        # 只给了地址（Keygent 的设置就是这样）：按地址认是哪家
        provider, env["WEAVER_BASE_URL"], protocol = guess_provider(env["WEAVER_BASE_URL"])
        if provider == "custom":
            env.setdefault("WEAVER_PROTOCOL", protocol)
    if not provider:
        raise ValueError(f"请在 .env 里设置 WEAVER_BASE_URL（接口地址），或 WEAVER_PROVIDER（{' / '.join(PRESETS)} / custom）")
    if provider == "custom":
        preset = {"protocol": env.get("WEAVER_PROTOCOL", "openai_chat"), "base_url": "", "keyless": True}
    elif provider in PRESETS:
        preset = PRESETS[provider]
    else:
        raise ValueError(f"未知的 WEAVER_PROVIDER：{provider}，可选 {' / '.join(PRESETS)} / custom")
    protocol = preset["protocol"]
    base_url = env.get("WEAVER_BASE_URL") or preset["base_url"]
    if not base_url:
        raise ValueError("custom 需要 WEAVER_BASE_URL")
    if protocol not in PROTOCOLS:
        raise ValueError(f"协议 {protocol} 还没实现（目前支持：{', '.join(PROTOCOLS)}）")

    legacy = provider.startswith("openrouter")
    key = env.get("WEAVER_API_KEY") or (env.get("OPENROUTER_API_KEY") if legacy else None)
    model = env.get("WEAVER_MODEL") or (env.get("OPENROUTER_MODEL") if legacy else None)
    if not model:
        raise ValueError("请在 .env 里设置 WEAVER_MODEL")
    if not key and not preset.get("keyless"):
        raise ValueError("请在 .env 里设置 WEAVER_API_KEY")
    if env.get("WEAVER_MAX_TOKENS"):
        kwargs.setdefault("max_tokens", int(env["WEAVER_MAX_TOKENS"]))
    if env.get("WEAVER_EXTRA_BODY"):            # 例：{"provider": {"order": ["Moonshot AI"]}}
        try:
            kwargs.setdefault("extra_body", json.loads(env["WEAVER_EXTRA_BODY"]))
        except json.JSONDecodeError as e:
            raise ValueError(f"WEAVER_EXTRA_BODY 不是合法的 JSON：{e}") from None
    make_quirks = _anthropic_quirks if protocol == "anthropic_messages" else _quirks
    return PROTOCOLS[protocol](model, base_url, key, blobs=blobs,
                               quirks=make_quirks(preset.get("quirks", {}), env), **kwargs)


__all__ = ["make_model", "guess_provider", "load_env", "ModelError", "Accumulator", "StreamingModel", "OpenAIChatModel", "Quirks",
           "AnthropicModel", "AnthropicQuirks", "PRESETS"]
