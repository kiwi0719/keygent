"""工具结果可以是一段文字，也可以是片段列表（文字 + 图片引用，图片存在 BlobStore 里）。见 design/mcp2.md 第五节。

片段的格式和用户附件一样：{"type": "text", "text"} / {"type": "image", "ref": "blob://…", "mime", "name"}。
"""
from __future__ import annotations

import json

IMAGE_BYTES = 4500                   # 估算上下文时一张图按约 1500 token 算（同 kernel.IMAGE_BYTES）


def text_of(output) -> str:
    """给人、给搜索、给估算看的文字：片段列表里的文字接起来，图片写一句。"""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        out = []
        for p in output:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                out.append(p.get("text", ""))
            elif p.get("type") == "image":
                out.append(f"[图片 {p.get('mime', '')}]")
        return "\n".join(x for x in out if x)
    return json.dumps(output, ensure_ascii=False) if output is not None else ""


def images_of(output) -> list[dict]:
    if not isinstance(output, list):
        return []
    return [p for p in output if isinstance(p, dict) and p.get("type") == "image" and p.get("ref")]
