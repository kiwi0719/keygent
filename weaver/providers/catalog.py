"""模型目录：查模型的上下文窗口。目前只查 OpenRouter 的公开模型列表（不需要 key），缓存一天。"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
TTL = 24 * 3600


def context_window(model: str, base_url: str, cache_dir: str | Path | None = None,
                   fetch=None) -> int | None:
    """查不到就返回 None，由调用方用配置或默认值。"""
    if "openrouter.ai" not in base_url:
        return None
    try:
        models = _openrouter_models(Path(cache_dir) if cache_dir else None, fetch)
    except Exception:
        return None
    m = models.get(model)
    return int(m["context_length"]) if m and m.get("context_length") else None


def _openrouter_models(cache_dir: Path | None, fetch) -> dict:
    cache = cache_dir / "openrouter-models.json" if cache_dir else None
    if cache and cache.exists() and time.time() - cache.stat().st_mtime < TTL:
        return json.loads(cache.read_text())
    if fetch is None:
        def fetch():
            with urllib.request.urlopen(OPENROUTER_MODELS, timeout=15) as r:
                return json.load(r)
    data = {m["id"]: {"context_length": m.get("context_length")} for m in fetch().get("data", [])}
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    return data
