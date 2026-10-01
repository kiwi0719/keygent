"""OpenAI Chat Completions 协议：OpenAI、OpenRouter、DeepSeek、Kimi、通义、vLLM、Ollama 等共用。

“兼容”的服务之间有不少方言差异，集中在 Quirks 里配置，解析时尽量宽容地都认：
- 思考过程：`reasoning`（OpenRouter、vLLM、Ollama）/ `reasoning_content`（DeepSeek、Kimi、通义、SiliconFlow）
  / `reasoning_details`（OpenRouter 的签名条目）/ 正文开头的 `<think>…</think>`（部分本地模型）
- 缓存用量：`prompt_tokens_details.cached_tokens` / `prompt_cache_hit_tokens`（DeepSeek）/ `cached_tokens`（Kimi）
- 工具调用：缺 index、缺 id、arguments 直接给对象，都能拼对
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from .base import RETRY_STATUS, Accumulator, Emit, ModelError, StreamingModel

STOP_MAP = {
    "stop": "end", "eos": "end", "eos_token": "end", "end_turn": "end", "stop_sequence": "end",
    "tool_calls": "tool_calls", "function_call": "tool_calls", "tool_use": "tool_calls",
    "length": "truncated", "max_tokens": "truncated", "model_length": "truncated",
    "content_filter": "refusal", "refusal": "refusal", "sensitive": "refusal",
}

# 思考过程在账本里的格式标记：回传时只把当前服务认得的格式传回去
FMT_DETAILS = "openrouter"              # reasoning_details 签名条目
FMT_CONTENT = "reasoning_content"       # 纯文字的 reasoning_content / reasoning / <think>


@dataclass
class Quirks:
    """各家“OpenAI 兼容”服务的方言差异。"""
    max_tokens_field: str = "max_tokens"          # OpenAI 新模型要 max_completion_tokens
    stream_usage: bool = True                     # 是否发 stream_options.include_usage（少数服务不认）
    echo_reasoning: set[str] = field(default_factory=set)   # 回传哪些格式的思考过程
    echo_reasoning_scope: str = "all"             # all：所有历史回复都回传；turn：只回传最后一条用户消息之后的
    think_tags: bool = True                       # 正文开头的 <think>…</think> 当作思考过程
    cache_marks: str = "never"                    # 显式缓存标记 cache_control：never / always / claude（模型名含 claude 才标）
    prompt_cache_key: bool = False                # 带 prompt_cache_key（OpenAI 官方）


class ThinkSplitter:
    """把正文开头的 <think>…</think> 分出来当思考过程。流式安全：标签被拆在两个片段里也能认出。"""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.state, self.buf = "start", ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        """输入一段正文，返回 [(kind, text)]，kind 为 text 或 reasoning。"""
        out: list[tuple[str, str]] = []
        self.buf += text
        while self.buf:
            if self.state == "start":
                s = self.buf.lstrip()
                if not s:
                    return out
                if s.startswith(self.OPEN):
                    self.state, self.buf = "inside", s[len(self.OPEN):]
                elif self.OPEN.startswith(s):
                    return out                    # 还不够判断，等下一段
                else:
                    self.state = "after"
            elif self.state == "inside":
                i = self.buf.find(self.CLOSE)
                if i >= 0:
                    if i:
                        out.append(("reasoning", self.buf[:i]))
                    self.state, self.buf = "after", self.buf[i + len(self.CLOSE):].lstrip("\n")
                    continue
                keep = next((n for n in range(len(self.CLOSE) - 1, 0, -1) if self.buf.endswith(self.CLOSE[:n])), 0)
                if len(self.buf) > keep:
                    out.append(("reasoning", self.buf[:len(self.buf) - keep]))
                self.buf = self.buf[len(self.buf) - keep:]
                return out
            else:
                out.append(("text", self.buf))
                self.buf = ""
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        if not rest:
            return []
        return [("reasoning" if self.state == "inside" else "text", rest)]


def _first(*vals):
    """第一个不是 None 的值（0 也算有值）。"""
    return next((v for v in vals if v is not None), None)


def parse_usage(u: dict) -> dict:
    """各家用量字段归一。input_tokens 含缓存部分。"""
    cached = _first((u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                    u.get("prompt_cache_hit_tokens"), u.get("cached_tokens"))
    return {"input_tokens": _first(u.get("prompt_tokens"), u.get("input_tokens")),
            "output_tokens": _first(u.get("completion_tokens"), u.get("output_tokens")),
            "cached_tokens": cached,
            "cache_write_tokens": (u.get("prompt_tokens_details") or {}).get("cache_write_tokens") or None,
            "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "cost": u.get("cost")}


class OpenAIChatModel(StreamingModel):
    protocol = "openai_chat"

    def __init__(self, *args, quirks: Quirks | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.quirks = quirks or Quirks()

    # ------------------------------------------------ 统一格式 → 请求

    def _user_part(self, p: dict) -> dict:
        t = p["type"]
        if t == "text":
            return {"type": "text", "text": p["text"]}
        data = self.blobs.get(p["ref"]) if self.blobs else b""
        if t == "image":
            b64 = base64.b64encode(data).decode()
            return {"type": "image_url", "image_url": {"url": f"data:{p['mime']};base64,{b64}"}}
        if t == "audio":
            return {"type": "input_audio",
                    "input_audio": {"data": base64.b64encode(data).decode(), "format": p["mime"].split("/")[-1]}}
        if t == "file" and p.get("mime", "").startswith("text/"):
            return {"type": "text", "text": f"[文件 {p.get('name', '')}]\n{data.decode('utf-8', 'replace')}"}
        return {"type": "text", "text": f"[附件 {p.get('name', '')}（{p.get('mime', '')}）无法直接展开]"}

    @property
    def cache_marks(self) -> bool:
        mode = self.quirks.cache_marks
        return mode == "always" or (mode == "claude" and "claude" in self.name.lower())

    def _user(self, parts: list[dict]) -> dict:
        converted = [self._user_part(p) for p in parts]
        # 纯文字用字符串，兼容性最好；要打缓存标记时统一用片段列表，保证每轮格式一致（前缀不变）
        if all(p["type"] == "text" for p in converted) and not self.cache_marks:
            return {"role": "user", "content": "\n\n".join(p["text"] for p in converted)}
        return {"role": "user", "content": converted}

    def _assistant(self, parts: list[dict], echo: bool) -> dict:
        msg: dict = {"role": "assistant",
                     "content": "".join(p["text"] for p in parts if p["type"] == "text") or None}
        calls = [{"id": p["id"], "type": "function",
                  "function": {"name": p["name"],
                               "arguments": p.get("raw_args") or json.dumps(p.get("args") or {})}}
                 for p in parts if p["type"] == "tool_call"]
        if calls:
            msg["tool_calls"] = calls
        if echo:
            fmts = self.quirks.echo_reasoning
            thoughts = [p for p in parts if p["type"] == "reasoning"]
            details = [d for p in thoughts if p.get("format") == FMT_DETAILS for d in (p.get("opaque") or [])]
            if FMT_DETAILS in fmts and details:
                msg["reasoning_details"] = details
            text = "".join(p.get("text") or "" for p in thoughts)
            if FMT_CONTENT in fmts and text:
                msg["reasoning_content"] = text
        return msg

    def messages(self, system: str, messages: list[dict]) -> list[dict]:
        marks = self.cache_marks
        cc = {"type": "ephemeral"}
        out = [{"role": "system", "content": [{"type": "text", "text": system, "cache_control": cc}]
                if marks else system}]
        last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
        for i, m in enumerate(messages):
            if m["role"] == "user":
                out.append(self._user(m["content"]))
            elif m["role"] == "assistant":
                echo = self.quirks.echo_reasoning_scope == "all" or i > last_user
                out.append(self._assistant(m["content"], echo))
            elif m["role"] == "tool":
                text = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False)
                text = ("[错误] " + text) if m.get("is_error") else text
                out.append({"role": "tool", "tool_call_id": m["call_id"],
                            "content": [{"type": "text", "text": text}] if marks else text})
        if marks and messages and messages[-1].get("cache") and isinstance(out[-1].get("content"), list):
            last = out[-1]["content"][-1]
            last["cache_control"] = cc                   # 断点打在最后一条消息的最后一个片段上
        return out

    def request(self, system, tools, messages, tool_choice, cache_key=None):
        body = {"model": self.name, "messages": self.messages(system, messages), "stream": True,
                self.quirks.max_tokens_field: self.max_tokens}
        if self.quirks.stream_usage:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = tool_choice
        if self.quirks.prompt_cache_key and cache_key:
            body["prompt_cache_key"] = cache_key
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return f"{self.base_url}/chat/completions", headers, body

    # ------------------------------------------------ 流事件 → 统一片段

    def _reasoning(self, acc: Accumulator, emit: Emit, text: str) -> None:
        acc.reasoning("r", text, FMT_CONTENT)
        emit({"type": "reasoning", "text": text})

    def _content(self, acc: Accumulator, emit: Emit, text: str) -> None:
        pieces = acc.extra.setdefault("think", ThinkSplitter()).feed(text) if self.quirks.think_tags \
            else [("text", text)]
        for kind, piece in pieces:
            if kind == "reasoning":
                self._reasoning(acc, emit, piece)
            else:
                acc.text("t", piece)
                emit({"type": "text", "text": piece})

    def _tool_calls(self, acc: Accumulator, emit: Emit, calls: list[dict]) -> None:
        seen: dict[str, int] = acc.extra.setdefault("tool_ids", {})
        for tc in calls:
            fn = tc.get("function") or {}
            cid, idx = tc.get("id"), tc.get("index")
            if idx is None:                      # 有的服务不给 index：按 id 区分，没有 id 就接在上一个后面
                if cid and cid not in seen:
                    idx = len(seen)
                elif cid:
                    idx = seen[cid]
                else:
                    idx = acc.extra.get("last_tool", 0)
            if cid:
                seen.setdefault(cid, idx)
            acc.extra["last_tool"] = idx
            new = ("tool_call", idx) not in acc.by_key
            acc.tool_start(idx, cid, fn.get("name"))
            if new:
                emit({"type": "tool_start", "id": cid, "name": fn.get("name")})
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):   # 有的服务直接给对象
                args = json.dumps(args, ensure_ascii=False)
            if args:
                acc.tool_args(idx, args)

    def parse(self, event: str, data: str, acc: Accumulator, emit: Emit) -> bool:
        if data.strip() == "[DONE]":
            self._flush(acc, emit)
            return True
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            raise ModelError(f"无法解析的流数据：{data[:200]}", retryable=True) from None
        if obj.get("error") or event == "error":
            err = obj.get("error", obj)
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            retry = code in RETRY_STATUS or any(w in str(msg).lower()
                                                for w in ("overload", "rate limit", "timeout", "busy"))
            raise ModelError(f"流中错误 {code}: {msg}", retryable=retry)

        usage = obj.get("usage") or (obj.get("x_groq") or {}).get("usage")
        if usage:
            acc.add_usage(**parse_usage(usage))

        for choice in obj.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            d = choice.get("delta") or choice.get("message") or {}
            thought = d.get("reasoning_content") or d.get("reasoning")
            if isinstance(thought, str) and thought:
                self._reasoning(acc, emit, thought)
            if d.get("reasoning_details"):
                acc.reasoning_opaque("r", d["reasoning_details"], FMT_DETAILS)
            if isinstance(d.get("content"), str) and d["content"]:
                self._content(acc, emit, d["content"])
            if d.get("refusal"):
                acc.text("t", d["refusal"])
                acc.extra["refused"] = True
                emit({"type": "text", "text": d["refusal"]})
            if d.get("tool_calls"):
                self._tool_calls(acc, emit, d["tool_calls"])
            if choice.get("finish_reason"):
                self._flush(acc, emit)
                reason = STOP_MAP.get(choice["finish_reason"], "other")
                acc.stop("refusal" if acc.extra.get("refused") else reason)
        return False

    def _flush(self, acc: Accumulator, emit: Emit) -> None:
        splitter = acc.extra.get("think")
        for kind, piece in (splitter.flush() if splitter else []):
            if kind == "reasoning":
                self._reasoning(acc, emit, piece)
            else:
                acc.text("t", piece)
                emit({"type": "text", "text": piece})
