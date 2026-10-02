"""Anthropic Messages 协议：Anthropic 官方，以及提供 Anthropic 兼容端点的服务。

和 OpenAI 协议的主要差别（见 design/providers.md 第四节）：
- system 是顶层字段；工具结果放在下一条 user 消息里；user / assistant 必须交替
- 工具调用 id 只能含字母、数字、_、-
- 思考过程是带签名的 thinking 块（或整块加密的 redacted_thinking），要原样回传
- usage.input_tokens 不含缓存部分，要把缓存读写加回去
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

from .base import Accumulator, Emit, ModelError, StreamingModel

FMT = "anthropic"
STOP_MAP = {"end_turn": "end", "stop_sequence": "end", "tool_use": "tool_calls",
            "max_tokens": "truncated", "model_context_window_exceeded": "context_exceeded",
            "refusal": "refusal", "pause_turn": "other"}
RETRYABLE_ERRORS = {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}


def safe_id(call_id: str) -> str:
    """把别家的调用 id 换成 Anthropic 认的字符。同一个 id 总是换成同一个结果，调用和结果两边一致。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", call_id or "call")[:64]


@dataclass
class AnthropicQuirks:
    auth: str = "x-api-key"             # x-api-key（官方）/ bearer（部分兼容服务）
    version: str = "2023-06-01"
    thinking_budget: int = 0            # >0 时开启扩展思考
    cache: bool = True                  # 打缓存断点（最后一个工具、system、最后一条消息）


class AnthropicModel(StreamingModel):
    protocol = "anthropic_messages"

    def __init__(self, *args, quirks: AnthropicQuirks | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.quirks = quirks or AnthropicQuirks()

    # ------------------------------------------------ 统一格式 → 请求

    def _user_part(self, p: dict) -> dict:
        t = p["type"]
        if t == "text":
            return {"type": "text", "text": p["text"]}
        data = self.blobs.get(p["ref"]) if self.blobs else b""
        b64 = base64.b64encode(data).decode()
        if t == "image":
            return {"type": "image", "source": {"type": "base64", "media_type": p["mime"], "data": b64}}
        if t == "file" and p.get("mime") == "application/pdf":
            return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                    "title": p.get("name")}
        if t == "file" and p.get("mime", "").startswith("text/"):
            return {"type": "text", "text": f"[文件 {p.get('name', '')}]\n{data.decode('utf-8', 'replace')}"}
        return {"type": "text", "text": f"[附件 {p.get('name', '')}（{p.get('mime', '')}）无法直接展开]"}

    def _assistant_blocks(self, parts: list[dict]) -> list[dict]:
        blocks = []
        for p in parts:
            if p["type"] == "reasoning":
                if p.get("format") != FMT:       # 别家的思考过程 Anthropic 不认，丢掉
                    continue
                opaque = (p.get("opaque") or [{}])[0]
                if opaque.get("type") == "redacted":
                    blocks.append({"type": "redacted_thinking", "data": opaque.get("data", "")})
                elif opaque.get("signature"):
                    blocks.append({"type": "thinking", "thinking": p.get("text") or "",
                                   "signature": opaque["signature"]})
            elif p["type"] == "text" and p["text"]:
                blocks.append({"type": "text", "text": p["text"]})
            elif p["type"] == "tool_call":
                blocks.append({"type": "tool_use", "id": safe_id(p["id"]), "name": p["name"],
                               "input": p.get("args") or {}})
        return blocks or [{"type": "text", "text": "…"}]     # 不允许空的 assistant 消息

    def messages(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []

        def add(role: str, blocks: list[dict]) -> None:
            if out and out[-1]["role"] == role:  # 同一角色连续出现就合并，保证交替
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            if m["role"] == "user":
                add("user", [self._user_part(p) for p in m["content"]])
            elif m["role"] == "assistant":
                add("assistant", self._assistant_blocks(m["content"]))
            elif m["role"] == "tool":
                c = m["content"]
                if isinstance(c, list):                  # 文字 + 图片（MCP 工具返回的图）：tool_result 里本来就能放图片
                    content = [self._user_part(p) for p in c if isinstance(p, dict)] or "(没有输出)"
                else:
                    content = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                block = {"type": "tool_result", "tool_use_id": safe_id(m["call_id"]), "content": content}
                if m.get("is_error"):
                    block["is_error"] = True
                add("user", [block])
        if out and out[0]["role"] != "user":     # 必须以 user 开头
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "…"}]})
        if self.quirks.cache and messages and messages[-1].get("cache") and out:
            out[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        return out

    def request(self, system, tools, messages, tool_choice, cache_key=None):
        body: dict = {"model": self.name, "max_tokens": self.max_tokens, "stream": True,
                      "messages": self.messages(messages)}
        cc = {"type": "ephemeral"}
        if system:
            body["system"] = [{"type": "text", "text": system, **({"cache_control": cc} if self.quirks.cache else {})}]
        if tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                              "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
                             for t in tools]
            if self.quirks.cache:
                body["tools"][-1]["cache_control"] = cc
            body["tool_choice"] = {"type": tool_choice if tool_choice in ("auto", "none", "any") else "auto"}
        if self.quirks.thinking_budget > 0:
            body["thinking"] = {"type": "enabled", "budget_tokens": self.quirks.thinking_budget}
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                   "anthropic-version": self.quirks.version, **self.extra_headers}
        if self.api_key:
            if self.quirks.auth == "bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers["x-api-key"] = self.api_key
        return f"{self.base_url}/v1/messages", headers, body

    # ------------------------------------------------ 流事件 → 统一片段

    def _usage(self, acc: Accumulator, u: dict) -> None:
        if not u:
            return
        prev = acc.extra.setdefault("usage", {})
        prev.update({k: v for k, v in u.items() if v is not None})
        read = prev.get("cache_read_input_tokens") or 0
        write = prev.get("cache_creation_input_tokens") or 0
        acc.add_usage(input_tokens=(prev.get("input_tokens") or 0) + read + write,
                      output_tokens=prev.get("output_tokens"),
                      cached_tokens=read, cache_write_tokens=write)

    def parse(self, event: str, data: str, acc: Accumulator, emit: Emit) -> bool:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            raise ModelError(f"无法解析的流数据：{data[:200]}", retryable=True) from None
        t = obj.get("type") or event

        if t == "error":
            err = obj.get("error") or {}
            raise ModelError(f"流中错误 {err.get('type')}: {err.get('message')}",
                             retryable=err.get("type") in RETRYABLE_ERRORS)
        if t == "message_start":
            self._usage(acc, (obj.get("message") or {}).get("usage") or {})
        elif t == "content_block_start":
            i, block = obj["index"], obj.get("content_block") or {}
            kind = block.get("type")
            if kind == "text":
                acc.text(i, block.get("text", ""))
                if block.get("text"):
                    emit({"type": "text", "text": block["text"]})
            elif kind == "thinking":
                acc.reasoning(i, block.get("thinking", ""), FMT)
            elif kind == "redacted_thinking":
                acc.reasoning_opaque(i, [{"index": 0, "type": "redacted", "data": block.get("data", "")}], FMT)
            elif kind == "tool_use":
                acc.tool_start(i, block.get("id"), block.get("name"))
                if block.get("input"):           # 有的兼容服务直接给完整参数，不再发增量
                    acc.extra.setdefault("tool_input", {})[i] = block["input"]
                emit({"type": "tool_start", "id": block.get("id"), "name": block.get("name")})
        elif t == "content_block_delta":
            i, d = obj["index"], obj.get("delta") or {}
            kind = d.get("type")
            if kind == "text_delta":
                acc.text(i, d.get("text", ""))
                emit({"type": "text", "text": d.get("text", "")})
            elif kind == "thinking_delta":
                acc.reasoning(i, d.get("thinking", ""), FMT)
                emit({"type": "reasoning", "text": d.get("thinking", "")})
            elif kind == "signature_delta":
                acc.reasoning_opaque(i, [{"index": 0, "signature": d.get("signature", "")}], FMT)
            elif kind == "input_json_delta":
                acc.tool_args(i, d.get("partial_json", ""))
        elif t == "content_block_stop":
            i = obj.get("index")
            given = acc.extra.get("tool_input", {}).get(i)
            block = acc.by_key.get(("tool_call", i))
            if given and block is not None and not block["raw_args"]:
                block["raw_args"] = json.dumps(given, ensure_ascii=False)
        elif t == "message_delta":
            reason = (obj.get("delta") or {}).get("stop_reason")
            if reason:
                acc.stop(STOP_MAP.get(reason, "other"))
            self._usage(acc, obj.get("usage") or {})
        elif t == "message_stop":
            return True
        return False                             # ping 等其他事件忽略
