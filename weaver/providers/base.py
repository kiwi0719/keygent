"""接入层的共用部分：发送、重试、断流处理、把流式片段拼成完整回复。

每种协议只需要继承 StreamingModel，写两件事：
  request(system, tools, messages, tool_choice) -> (url, headers, body)   翻译请求
  parse(event, data, acc, emit) -> bool                                  解析一个流事件，结束时返回 True
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


CONTEXT_PATTERNS = ("context_length_exceeded", "context length", "context window", "prompt is too long",
                    "too many tokens", "input is too long", "reduce the length", "maximum context",
                    "exceeds the context", "request too large for model")


def is_context_error(text: str) -> bool:
    """各家“上下文超长”报错的写法不一，按关键词认。"""
    t = text.lower()
    return any(p in t for p in CONTEXT_PATTERNS)


class ModelError(Exception):
    """模型调用失败。retryable 表示值得重试；kind=context 表示上下文超长（交给内核去压缩）。"""

    def __init__(self, message: str, retryable: bool = False, kind: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind or ("context" if is_context_error(message) else "")
        if self.kind == "context":
            self.retryable = False       # 重发一模一样的请求没用


def parse_args(raw: str) -> dict | None:
    """工具参数：整条回复结束后才解析。失败返回 None，交给内核补错误。"""
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return args if isinstance(args, dict) else None


# ------------------------------------------------------------------ 发送


def urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> Iterable[bytes]:
    """发请求，按网络块产出响应字节。HTTP 错误转成 ModelError。"""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:2000]
        raise ModelError(f"HTTP {e.code}: {detail}", retryable=e.code in RETRY_STATUS) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ModelError(f"网络错误：{e}", retryable=True) from None

    def chunks():
        try:
            with resp:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        return
                    yield chunk
        except (TimeoutError, ConnectionError, OSError) as e:
            raise ModelError(f"流中断：{e}", retryable=True) from None
    return chunks()


# ------------------------------------------------------------------ 累积器


def _merge_opaque(items: list[dict], new: list[dict]) -> None:
    """合并流式到达的签名/加密条目：按 index 对齐，字符串字段拼接，其他字段覆盖。"""
    for n, item in enumerate(new):
        idx = item.get("index", n)
        target = next((x for x in items if x.get("index", -1) == idx), None)
        if target is None:
            items.append(dict(item, index=idx))
            continue
        for key, val in item.items():
            if val is None or key == "index":
                continue
            if key in ("text", "summary", "data", "signature") and isinstance(val, str) \
                    and isinstance(target.get(key), str):
                target[key] += val
            else:
                target[key] = val


class Accumulator:
    """把统一片段拼成完整回复。各块按第一次出现的顺序排列。"""

    def __init__(self):
        self.blocks: list[dict] = []
        self.by_key: dict[tuple[str, object], dict] = {}
        self.stop_reason: str | None = None
        self.usage: dict = {}
        self.extra: dict = {}           # 协议解析器自己的流内状态

    def _block(self, kind: str, index, **init) -> dict:
        key = (kind, index)
        if key not in self.by_key:
            self.by_key[key] = {"type": kind, **init}
            self.blocks.append(self.by_key[key])
        return self.by_key[key]

    def text(self, index, text: str) -> None:
        self._block("text", index, text="")["text"] += text

    def reasoning(self, index, text: str, fmt: str) -> None:
        b = self._block("reasoning", index, text="", opaque=None, format=fmt)
        b["text"] += text

    def reasoning_opaque(self, index, items: list[dict], fmt: str) -> None:
        b = self._block("reasoning", index, text="", opaque=None, format=fmt)
        b["format"] = fmt               # 有签名时，格式以签名为准（回传时要对得上）
        b["opaque"] = b["opaque"] or []
        _merge_opaque(b["opaque"], items)

    def tool_start(self, index, call_id: str | None, name: str | None) -> None:
        b = self._block("tool_call", index, id="", name="", raw_args="")
        b["id"] = call_id or b["id"]
        b["name"] = name or b["name"]

    def tool_args(self, index, text: str) -> None:
        self._block("tool_call", index, id="", name="", raw_args="")["raw_args"] += text

    def stop(self, reason: str) -> None:
        self.stop_reason = reason

    def add_usage(self, **usage) -> None:
        self.usage.update({k: v for k, v in usage.items() if v is not None})

    def build(self) -> dict:
        content = []
        for b in self.blocks:
            if b["type"] == "text" and not b["text"]:
                continue
            if b["type"] == "reasoning":
                b = {**b, "text": b["text"] or None}
            if b["type"] == "tool_call":
                b = {**b, "id": b["id"] or f"call_{len(content)}", "args": parse_args(b["raw_args"])}
            content.append(b)
        stop = self.stop_reason or "other"
        if stop == "end" and any(b["type"] == "tool_call" for b in content):
            stop = "tool_calls"
        usage = {"input_tokens": 0, "output_tokens": 0, **self.usage}
        return {"content": content, "stop": stop, "usage": usage}


# ------------------------------------------------------------------ 流式模型


Emit = Callable[[dict], None]


class StreamingModel:
    protocol = "?"

    def __init__(self, name: str, base_url: str, api_key: str | None = None, blobs=None,
                 max_tokens: int = 8192, retries: int = 3, timeout: float = 300,
                 extra_headers: dict | None = None, extra_body: dict | None = None,
                 transport=urllib_transport, sleep=time.sleep):
        self.name, self.base_url, self.api_key, self.blobs = name, base_url.rstrip("/"), api_key, blobs
        self.max_tokens, self.retries, self.timeout = max_tokens, retries, timeout
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}      # 原样并进请求体，如 OpenRouter 的 provider 路由
        self.transport, self.sleep = transport, sleep

    # 子类实现
    def request(self, system: str, tools: list[dict], messages: list[dict], tool_choice: str,
                cache_key: str | None = None):
        raise NotImplementedError

    def parse(self, event: str, data: str, acc: Accumulator, emit: Emit) -> bool:
        raise NotImplementedError

    def create(self, system: str, tools: list[dict], messages: list[dict], tool_choice: str = "auto",
               on_delta: Emit | None = None, cache_key: str | None = None) -> dict:
        from .sse import iter_sse
        url, headers, body = self.request(system, tools, messages, tool_choice, cache_key)
        body = {**body, **self.extra_body}
        data = json.dumps(body, ensure_ascii=False).encode()
        for attempt in range(self.retries + 1):
            shown = False

            def emit(delta: dict) -> None:
                nonlocal shown
                shown = True
                if on_delta:
                    on_delta(delta)

            try:
                acc = Accumulator()
                finished = False
                for event, payload in iter_sse(self.transport(url, headers, data, self.timeout)):
                    if self.parse(event, payload, acc, emit):
                        finished = True
                        break
                if not finished and acc.stop_reason is None:   # 没有结束标记，也没有停止原因：断流
                    raise ModelError("流意外结束，没有收到结束标记", retryable=True)
                return acc.build()
            except ModelError as e:
                if shown and on_delta:         # 已经显示的半截内容作废
                    on_delta({"type": "abort", "reason": str(e), "retrying": e.retryable and attempt < self.retries})
                if not e.retryable or attempt == self.retries:
                    raise
                self.sleep(min(2 ** attempt, 20))
        raise AssertionError("unreachable")
