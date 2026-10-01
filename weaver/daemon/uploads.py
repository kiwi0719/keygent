"""附件：上传存进 blob（按内容哈希），另记一份名字、类型。见 design/daemon.md 第六节。

用到任务里时：复制进工作目录的 附件/ 下，并在输入里说一句放在哪了（Excel 这类模型看不了的，Agent 用工具去读）；
图片、PDF、不长的文本另外作为输入片段直接给模型看。
长文本（粘贴的大段内容、日志）不整份塞进上下文：只告诉模型在哪、多大、开头几行，让它用 grep 找、read_file 按行读。
"""
from __future__ import annotations

import json
import mimetypes
import re
import time
import uuid
from pathlib import Path

from ..stores import DirBlobStore

MAX_SIZE = 50 * 1024 * 1024
INLINE = ("image/", "text/", "application/pdf")
INLINE_MAX = 5 * 1024 * 1024             # 太大的就只放进工作目录，不直接给模型
TEXT_INLINE_MAX = 24 * 1024              # 文本超过这么大就走 grep，不整份放进上下文（约 6–8k token）
PREVIEW_LINES = 12
FOLDER = "附件"


def safe_name(name: str) -> str:
    name = Path(name or "").name.strip()
    name = re.sub(r'[\x00-\x1f/\\:]', "_", name)
    return name.lstrip(".") or "附件"


class Uploads:
    def __init__(self, home: str | Path, blobs: DirBlobStore | None = None):
        self.dir = Path(home) / "uploads"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.blobs = blobs or DirBlobStore(Path(home) / "blobs")

    def put(self, data: bytes, name: str, mime: str = "") -> dict:
        if len(data) > MAX_SIZE:
            raise ValueError(f"附件太大（上限 {MAX_SIZE // 1024 // 1024} MB）")
        name = safe_name(name)
        mime = (mime or "").split(";")[0].strip() or mimetypes.guess_type(name)[0] or "application/octet-stream"
        meta = {"id": "b-" + uuid.uuid4().hex[:10], "name": name, "mime": mime, "size": len(data),
                "ref": self.blobs.put(data), "created": time.time()}
        (self.dir / f"{meta['id']}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta

    def get(self, upload_id: str) -> dict | None:
        if not re.fullmatch(r"b-[0-9a-f]{10}", upload_id or ""):
            return None
        try:
            return json.loads((self.dir / f"{upload_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def content(self, text: str, ids: list[str], workdir: str | Path) -> list[dict]:
        """一句话 + 附件 → 输入片段。附件复制进 <工作目录>/附件/。不认识的 id 抛 KeyError。"""
        metas = []
        for i in ids:
            m = self.get(i)
            if m is None:
                raise KeyError(i)
            metas.append(m)
        parts = [{"type": "text", "text": text}] if text.strip() else []
        if not metas:
            return parts
        folder = Path(workdir) / FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        placed = []
        for m in metas:
            dest = folder / m["name"]
            stem, suffix, n = dest.stem, dest.suffix, 2
            while dest.exists() and dest.read_bytes() != self.blobs.get(m["ref"]):
                dest, n = folder / f"{stem} ({n}){suffix}", n + 1
            dest.write_bytes(self.blobs.get(m["ref"]))
            placed.append(f"{FOLDER}/{dest.name}")
            if m["mime"].startswith("text/") and m["size"] > TEXT_INLINE_MAX:
                parts.append({"type": "text", "text": long_text_note(f"{FOLDER}/{dest.name}", dest.read_bytes()),
                              "note": True, "name": m["name"]})
            elif m["mime"].startswith(INLINE) and m["size"] <= INLINE_MAX:
                kind = "image" if m["mime"].startswith("image/") else "file"
                parts.append({"type": kind, "ref": m["ref"], "mime": m["mime"], "name": m["name"]})
        # note：只给模型看的说明，界面上“你说的话”不显示
        parts.append({"type": "text", "text": "附件已放在工作目录：" + "、".join(placed), "note": True,
                      "files": [m["name"] for m in metas]})
        return parts


def long_text_note(path: str, data: bytes) -> str:
    """长文本附件给模型的说明：在哪、多大、开头几行，以及怎么读。"""
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    head = "\n".join(l if len(l) <= 200 else l[:200] + "…" for l in lines[:PREVIEW_LINES])
    more = f"\n…（后面还有 {len(lines) - PREVIEW_LINES} 行）" if len(lines) > PREVIEW_LINES else ""
    return (f"[长文本 {path}：{len(lines)} 行，{len(data) // 1024} KB，太长没有直接放进来]\n"
            f"需要里面的内容时，先用 grep 在这个文件里按关键词找到位置，再用 read_file 带 offset / limit 只读相关的几段，"
            f"不要整份读进来。开头几行：\n{head}{more}")
