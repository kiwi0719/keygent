"""SSE 读取器：两种协议共用。

- 先按字节缓冲，凑成完整一行再解码：一个中文字符可能被拆在两个网络块里。
- 事件以空行分隔；`:` 开头的是注释（如 OpenRouter 的心跳），跳过。
- 产出 (event, data)：event 没写时为 "message"，data 是多行 data: 拼起来的字符串。
"""
from __future__ import annotations

from typing import Iterable, Iterator


def iter_sse(chunks: Iterable[bytes]) -> Iterator[tuple[str, str]]:
    buf = b""
    event, data = "message", []

    def lines():
        nonlocal buf
        for chunk in chunks:
            buf += chunk
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                raw, buf = buf[:i], buf[i + 1:]
                yield raw.rstrip(b"\r").decode("utf-8", "replace")
        if buf:
            yield buf.rstrip(b"\r").decode("utf-8", "replace")
        yield ""                                   # 结尾补一个空行，把最后一个事件冲出来

    for line in lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
