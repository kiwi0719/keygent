# Weaver 模型接入层设计：流式 + 多协议

草案 v1 · 2026-10-01

这份文档讲模型接入层怎么同时支持 Anthropic 和 OpenAI 两种接口，以及流式输出怎么做。内核（design/kernel.md）不需要改。

---

## 一、要解决什么问题

现在的 `models.py` 只会说一种“方言”：OpenAI 兼容格式（OpenRouter 走的就是它）。但现在流行的是两种：

| 协议 | 谁在用 |
|---|---|
| **OpenAI Chat Completions** | OpenAI、OpenRouter、DeepSeek、Kimi、通义、vLLM、Ollama…… 几乎所有“OpenAI 兼容”的服务 |
| **Anthropic Messages** | Anthropic 官方，以及提供 Anthropic 兼容端点的服务（不少国产模型也提供） |

两者表达的是同一件事（对话、工具调用、思考过程、停止原因），但字段名、结构、流式格式都不一样。我们要：

1. **内核和账本完全不知道用的是哪家**。账本里存的是我们自己的统一格式。
2. **同一个会话中途可以换模型、换协议**，账本照样能用。
3. **流式输出**：逐字显示，但账本里只记完整的回复。

## 二、核心思路：按“协议”分，不按“厂商”分

适配器按**说哪种方言**来分，而不是按公司来分。OpenRouter、DeepSeek、本地 Ollama 都说 OpenAI 方言，就共用一个适配器，只是地址和 key 不同。

```
                   ┌──────────── 接入层 ────────────┐
内核/执行者 ──► Model.create(system, tools, messages, on_delta)
                   │                                │
                   │  1. 翻译：统一格式 → 方言请求   │ ← 每种协议一份
                   │  2. 发送：HTTP + SSE + 重试     │ ← 共用
                   │  3. 解析：方言流事件 → 统一片段 │ ← 每种协议一份
                   │  4. 累积：片段 → 完整回复       │ ← 共用
                   └────────────────────────────────┘
                              │
                     统一回复 Reply ──► 账本
```

每种协议只需要写两块：**怎么翻译请求**、**怎么解析流事件**。发送、重试、SSE 解析、把片段拼成完整回复，都是共用的。

以后要加 OpenAI 的 Responses API、Gemini，也只是再写这两块。

## 三、流式怎么做

### 1. 片段不进账本

流式的中间片段只推给前端显示。模型回复完整后，才往账本写一笔 `ActionCompleted`。内核看到的永远是一条完整的回复，所以**内核一行不改**。

### 2. 统一的片段类型

不管哪种协议，解析后都变成这几种片段，交给共用的“累积器”：

| 片段 | 含义 |
|---|---|
| `text(index, text)` | 一段正文 |
| `reasoning(index, text)` | 一段思考过程 |
| `reasoning_sig(index, data)` | 思考过程的签名/加密内容，要原样存下 |
| `tool_start(index, id, name)` | 开始一个工具调用 |
| `tool_args(index, text)` | 工具参数的一小段 JSON 字符串 |
| `stop(reason)` | 停止原因 |
| `usage(...)` | 用量 |

累积器按 `index` 把片段拼起来。**工具参数要等整条回复结束后才解析 JSON**，中途只拼字符串。拼完后产出统一回复，和现在的格式一样。

前端收到的是其中适合显示的部分（正文、思考、“正在调用某工具”），外加一个 `abort` 信号。

### 3. 流到一半断了

- 已经显示的半截内容**不算结果**。前端收到 `abort` 信号，把半截内容作废。
- **流还没开始就失败**（连不上、429、5xx）：退避后重试。
- **流已经开始、中途断了**：先发 `abort`，再整条重试，从头重新显示。
- 重试用完仍失败：抛错，执行者记一条失败的 `ActionCompleted`，内核按“模型调用失败”结束。
- 用户按 Ctrl+C：流被打断，账本里只有 `ActionStarted`。之后按取消流程走。

### 4. SSE 的坑（两种协议共用一个读取器）

- 按行读，按 `\n\n` 分事件；**先按字节缓冲，凑成完整一行再解码**，不然中文会被拆坏。
- 跳过注释行（`:` 开头，比如 OpenRouter 的 `: OPENROUTER PROCESSING` 心跳）。
- OpenAI 用 `data: [DONE]` 结束；Anthropic 用 `event: message_stop`。
- **HTTP 200 之后流里也可能来错误**：OpenAI 是一个带 `error` 的 data，Anthropic 是 `event: error`。不能只看状态码。
- Anthropic 的 `overloaded_error` 这类错误算“可重试”。

## 四、两种协议的对照（翻译规则）

### 请求

| 统一格式 | OpenAI Chat Completions | Anthropic Messages |
|---|---|---|
| system | 第一条 `role: system` 消息 | 顶层 `system` 字段 |
| 工具定义 `{name, description, parameters}` | `tools: [{type: function, function: {...}}]` | `tools: [{name, description, input_schema}]` |
| tool_choice `auto` / `none` | `"auto"` / `"none"` | `{type: auto}` / `{type: none}` |
| 用户文字 | `{type: text}` | `{type: text}` |
| 用户图片 | `{type: image_url, image_url: {url: data:…}}` | `{type: image, source: {type: base64, media_type, data}}` |
| 用户 PDF | 转成文字说明（不支持） | `{type: document, source: {…}}` |
| 助手正文 | `content` 字符串 | `{type: text}` 块 |
| 助手思考 | `reasoning_details`（OpenRouter）原样回传 | `{type: thinking, thinking, signature}` / `{type: redacted_thinking, data}` 原样回传 |
| 助手工具调用 | `tool_calls: [{id, function: {name, arguments(字符串)}}]` | `{type: tool_use, id, name, input(对象)}` 块 |
| 工具结果 | 单独一条 `role: tool, tool_call_id` | 放进**下一条 user 消息**的 `{type: tool_result, tool_use_id, content, is_error}` 块 |
| 工具出错 | 没有字段，在文字前加 `[错误]` | 原生 `is_error: true` |
| 输出上限 | `max_tokens`（可选） | `max_tokens`（**必填**） |

Anthropic 的额外规矩：

- **user / assistant 必须交替**。连续的 user 内容（工具结果 + 插话 + 提醒）要合并成一条 user 消息，工具结果块放在最前面。
- 工具调用 id 只能是字母、数字、`_`、`-`。从别的协议换过来的 id 要做一次**确定性的替换**（同一个 id 总是替换成同一个结果），调用和结果两边一起换。
- 工具参数解析失败（`args` 为 null）的调用，回传时 `input` 用 `{}`。

### 回复

| 统一格式 | OpenAI | Anthropic |
|---|---|---|
| stop `end` | `finish_reason: stop` | `stop_reason: end_turn` / `stop_sequence` |
| stop `tool_calls` | `tool_calls` | `tool_use` |
| stop `truncated` | `length` | `max_tokens`、`model_context_window_exceeded` |
| stop `refusal` | `content_filter`，或 message 里有 `refusal` | `refusal` |
| stop `other` | 其他 | `pause_turn` 等（暂不支持服务端工具，先按 other 处理） |
| usage.input_tokens | `prompt_tokens`（已含缓存部分） | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`（Anthropic 的 input_tokens **不含**缓存） |
| usage.cached_tokens | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| usage.output_tokens | `completion_tokens` | `output_tokens` |

流式事件：

| 统一片段 | OpenAI 流 | Anthropic 流 |
|---|---|---|
| `text` | `choices[0].delta.content` | `content_block_delta` / `text_delta` |
| `reasoning` | `delta.reasoning` | `content_block_delta` / `thinking_delta` |
| `reasoning_sig` | `delta.reasoning_details` | `content_block_delta` / `signature_delta`；`content_block_start` 里的 `redacted_thinking` |
| `tool_start` | `delta.tool_calls[i]` 第一次出现（带 id、name） | `content_block_start`，`type: tool_use` |
| `tool_args` | `delta.tool_calls[i].function.arguments` | `content_block_delta` / `input_json_delta.partial_json` |
| `stop` | `choices[0].finish_reason` | `message_delta.delta.stop_reason` |
| `usage` | 最后一块的 `usage`（要在请求里加 `stream_options.include_usage`） | `message_start` 里的输入用量 + `message_delta` 里的输出用量 |

## 五、思考过程跨协议怎么办

两家的思考签名格式完全不同，互相**不认**。所以思考片段要带上格式标记：

```
{type: "reasoning", text?, opaque?, format: "anthropic" | "openrouter" | ...}
```

回传时：**格式和当前协议一致就原样回传；不一致就丢掉签名**（只剩文字的思考也不回传）。代价是换协议后，模型看不到之前的思考过程，但对话本身完整，不会报错。

同理，统一格式里所有“某家专属”的东西都要标来源，翻译时不认识就丢，不能让它导致请求报错。

## 六、配置

`.env` 里选择协议和端点，支持预设：

```
WEAVER_PROVIDER=openrouter        # 预设：openrouter / openai / anthropic / deepseek / ollama / custom
WEAVER_MODEL=anthropic/claude-sonnet-4.5
WEAVER_API_KEY=...
# 只有 custom 需要：
WEAVER_PROTOCOL=openai_chat       # openai_chat / anthropic_messages
WEAVER_BASE_URL=https://...
```

预设就是“协议 + 地址 + 认证方式”的组合：

| 预设 | 协议 | 地址 | 认证头 |
|---|---|---|---|
| openrouter | openai_chat | `https://openrouter.ai/api/v1` | `Authorization: Bearer` |
| openai | openai_chat | `https://api.openai.com/v1` | `Authorization: Bearer` |
| deepseek | openai_chat | `https://api.deepseek.com/v1` | `Authorization: Bearer` |
| ollama | openai_chat | `http://localhost:11434/v1` | 无 |
| anthropic | anthropic_messages | `https://api.anthropic.com/v1` | `x-api-key` + `anthropic-version: 2023-06-01` |

兼容旧配置：只有 `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` 时，自动当作 `openrouter` 预设。

仍然只用标准库（`urllib` 能读流式响应），不引入 SDK。理由：两家 SDK 的统一格式对不上，最后还得自己翻译；手写 SSE 能完全掌控断流和重试。

## 七、代码结构

```
weaver/providers/
  __init__.py        make_model(config)：按预设/协议造出 Model
  base.py            StreamingModel：发送、重试、SSE 读取、累积器、abort
  sse.py             SSE 读取器（按字节缓冲、跳过注释）
  openai_chat.py     OpenAI 方言：翻译请求 + 解析流事件
  anthropic.py       Anthropic 方言：翻译请求 + 解析流事件
```

`Model` 接口只加一个可选参数：

```python
create(system, tools, messages, tool_choice="auto", on_delta=None) -> Reply
# on_delta(delta)：delta 是 {"type": "text"|"reasoning"|"tool_start"|"abort", ...}
```

执行者把 `on_delta` 接到 sink 上，前端收到的是“瞬时消息”（不落账本），和账本事件分开。

## 八、测试

全部离线，不联网：

1. **翻译测试**：同一段统一格式的对话（含图片、思考、并行调用、工具出错、插话），分别翻译成两种请求，逐字段检查。重点是 Anthropic 的合并交替、id 替换、tool_result 位置。
2. **流解析测试**：手写两种协议的 SSE 录制文本，喂给解析器，检查拼出的回复。覆盖：多个工具调用片段交错、中文被拆在两个网络块中间、注释行、流里的 error、最后才到的 usage。
3. **断流测试**：流到一半抛异常 → 发出 abort → 重试后从头来；重试用完 → 抛错。
4. **跨协议测试**：用 OpenAI 格式的回复建一个账本，再翻译成 Anthropic 请求，不报错、签名被丢弃、id 合法。
5. **真实冒烟**（手动）：两种协议各跑一次“读 main.md 第一行”。

## 九、暂不做

OpenAI Responses API、Gemini 原生接口、服务端工具（`pause_turn`）、提示缓存断点（下一步单独做，Anthropic 需要显式 `cache_control`）、按模型自动选择 `max_tokens`。

---

## 进度（2026-10-01）

- 已实现：`weaver/providers/` 下的 `sse.py`、`base.py`（发送、重试、断流 abort、累积器）、`openai_chat.py`、`__init__.py`（预设与配置）。CLI 逐字输出。测试见 `tests/test_providers.py`。
- 实现时的补充：流里只有 `finish_reason` 没有 `[DONE]` 也算正常结束；OpenAI 格式的 `refusal` 字段映射为 stop `refusal`；`stop=end` 但带工具调用时统一成 `tool_calls`。
- 已实现：`anthropic.py`，按第四节的翻译规则。见下文“Anthropic 协议”。

### 方言兼容（2026-10-01）

“OpenAI 兼容”的服务之间差异集中在 `Quirks`（`weaver/providers/openai_chat.py`），由预设给默认值，可用环境变量覆盖：

| 差异 | 处理 | 覆盖用的环境变量 |
|---|---|---|
| 思考过程字段 | 同时认 `reasoning`、`reasoning_content`、`reasoning_details`，以及正文开头的 `<think>…</think>` | `WEAVER_THINK_TAGS=0` 关闭 think 标签识别 |
| 思考过程回传 | 按格式标记决定：OpenRouter 回传 `reasoning_details`；DeepSeek、Kimi 只回传本轮的 `reasoning_content`（未验证）；其他默认不回传 | `WEAVER_ECHO_REASONING`、`WEAVER_ECHO_SCOPE` |
| 缓存用量 | 认 `prompt_tokens_details.cached_tokens` / `prompt_cache_hit_tokens` / `cached_tokens` | — |
| 输出上限字段 | OpenAI 用 `max_completion_tokens`，其他用 `max_tokens` | `WEAVER_MAX_TOKENS_FIELD`、`WEAVER_MAX_TOKENS` |
| 流式用量 | 默认发 `stream_options.include_usage`，个别服务不认可以关 | `WEAVER_STREAM_USAGE=0` |
| 工具调用片段 | 缺 index 按 id 区分；缺 id 补 `call_N`；arguments 是对象就转字符串 | — |
| 停止原因 | 认 `eos`、`end_turn`、`max_tokens`、`sensitive` 等变体 | — |

预设：openrouter、openai、deepseek、kimi、qwen、glm、siliconflow、groq、together、xai、gemini、ollama、lmstudio、vllm、custom。只有 openrouter 用真实 key 跑过。

### Anthropic 协议（2026-10-01）

`weaver/providers/anthropic.py`，测试见 `tests/test_anthropic.py`。

- 地址写法和官方 SDK 一致：不带 `/v1`，请求时拼 `/v1/messages`。
- 预设：`anthropic`（官方，`x-api-key`）、`openrouter-anthropic`（已用真实 key 跑通，Bearer 认证，沿用 `OPENROUTER_*`）、`deepseek-anthropic`、`kimi-anthropic`（未验证）。
- 方言设置 `AnthropicQuirks`：`WEAVER_AUTH=bearer`、`WEAVER_ANTHROPIC_VERSION`、`WEAVER_THINKING_BUDGET`（>0 开启扩展思考）。
- 实现时的补充：空的 assistant 消息补一个占位文字；对话必须以 user 开头；`tool_use` 块在开始时就给出完整 `input`（不发增量）的兼容服务也能处理；`pause_turn` 暂按 `other` 结束。
- 跨协议：同一本账本可以在两种协议间来回切换（测试覆盖）。

### 实测：DeepSeek、Kimi 经 OpenRouter（2026-10-01）

模型：`deepseek/deepseek-v4.1-flash`、`moonshotai/kimi-k3`。每个模型 × 两种协议（`openrouter`、`openrouter-anthropic`），每次两轮：第一轮并行/连续读文件，第二轮同一会话接着问（检验思考过程回传和跨轮）。

- 8 次全部成功：思考过程能显示、工具调用正常、跨轮不报错。说明经 OpenRouter 时，这两家的思考回传由 OpenRouter 处理，我们的 `openrouter` / `openrouter-anthropic` 预设够用。
- **不能说明**：直连 DeepSeek / Kimi 官方接口时 `reasoning_content` 的回传要求。`deepseek`、`kimi` 预设里的 `echo_reasoning_scope=turn` 仍是未验证的。
- 缓存：DeepSeek 两种协议都命中（第二轮 cached≈3.6k–3.8k）。Kimi 默认 `cached=0`，原因是 OpenRouter 默认把 kimi-k3 路由到第三方托管商（实测为 Wafer，只命中 64 token）；固定路由到 Moonshot AI 后命中正常（第二轮 cached≈3.6k–3.8k）。
- 为此加了 `WEAVER_EXTRA_BODY`：原样并进请求体。固定 OpenRouter 路由的写法：
  `WEAVER_EXTRA_BODY={"provider":{"order":["Moonshot AI"],"allow_fallbacks":false}}`
