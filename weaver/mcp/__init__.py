"""MCP：用官方 SDK（可选依赖）接外部工具服务器。见 design/mcp.md。

没装 SDK 时只有 config 能用（读配置、给提示）；manager / tools 在用到时才导入 SDK。
"""
from __future__ import annotations

from .config import McpConfig, ServerConfig


def sdk_available() -> bool:
    try:
        import mcp  # noqa: F401  （这是官方 SDK，不是本包）
        return True
    except ImportError:
        return False


__all__ = ["McpConfig", "ServerConfig", "sdk_available"]
