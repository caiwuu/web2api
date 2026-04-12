# Anthropic 与 OpenAI：入参 / 出参系统性对照

本文从 **官方 API 形态** 归纳两类协议的请求与响应；文末简述 **本仓库适配器**（`core/protocol/anthropic.py`、`core/protocol/openai.py`）实际解析与输出的子集。

---

## 1. 总览

| 维度 | Anthropic Messages | OpenAI Chat Completions |
|------|-------------------|-------------------------|
| 典型端点 | `POST /v1/messages` | `POST /v1/chat/completions` |
| 认证头 | `x-api-key`、`anthropic-version`（等） | `Authorization: Bearer …`（等） |
| 对话角色 | `messages[].role` 仅 `user` \| `assistant`；系统用顶层 `system` | `messages[].role` 含 `system` \| `user` \| `assistant` \| `tool` |
| 工具结果位置 | 放在 **`user`** 消息的 `tool_result` 块 | **`role: "tool"`** 消息，`tool_call_id` |
| 工具定义字段名 | `input_schema` | `function.parameters`（JSON Schema） |
| 流式传输 | SSE，`event:` + `data:` | SSE，`data: {JSON}`，末行 `data: [DONE]` |

---

## 2. Anthropic：入参（请求体）

### 2.1 根级字段（常用）

| 字段 | 说明 |
|------|------|
| `model` | 必填，模型 ID |
| `max_tokens` | 必填，本次回复最大生成 token |
| `messages` | 必填，见 §2.2 |
| `system` | 可选，字符串或内容块数组 |
| `metadata` | 可选，如 `user_id` |
| `temperature` / `top_p` / `top_k` | 可选，采样 |
| `stop_sequences` | 可选，字符串数组 |
| `stream` | 可选，是否 SSE |
| `stream_options` | 可选，如流式 `include_usage`（依版本） |
| `tools` | 可选，工具列表 |
| `tool_choice` | 可选，`{ "type": "auto" \| "any" \| "none" \| "tool", … }` |
| `thinking` | 可选，扩展思考配置 |
| `output_config` / `service_tier` | 可选，依版本与账号 |

更细的内容块与完整示例见 [anthropic-messages.md](./anthropic-messages.md)。

### 2.2 `messages[]` 与 `content` 块类型（摘要）

- **`role`**：`user` \| `assistant`。
- **`content`**：字符串，或块数组：`text`、`image`、`document`、`tool_use`（助手）、`tool_result`（用户侧，对应先前 `tool_use.id`）等。

### 2.3 `tools[]`（自定义工具）

- `name`、`description`、`input_schema`（JSON Schema 对象）。

---

## 3. Anthropic：出参（响应）

### 3.1 非流式（HTTP 200，JSON）

典型顶层字段：

| 字段 | 说明 |
|------|------|
| `id` | 消息 ID，如 `msg_…` |
| `type` | 常为 `"message"` |
| `role` | `"assistant"` |
| `model` | 使用的模型 |
| `content` | **数组**：`text`、`tool_use`、`thinking` 等块 |
| `stop_reason` | 如 `end_turn`、`max_tokens`、`tool_use` 等 |
| `stop_sequence` | 若因自定义停止序列结束则有值，否则常 `null` |
| `usage` | `input_tokens`、`output_tokens`（及缓存等扩展字段依版本） |

**错误**：HTTP 4xx/5xx，体多为 `{ "type": "error", "error": { "type", "message" } }` 形态（以官方为准）。

### 3.2 流式（SSE）

事件类型（与官方一致，名称可能随版本增加）包括但不限于：

- `message_start`：含 `message` 元数据（id、model、空 `content` 等）
- `content_block_start` / `content_block_delta` / `content_block_stop`：文本、思考、`tool_use`（含 `input_json_delta`）等
- `message_delta`：如 `stop_reason`、`usage` 增量
- `message_stop`：流结束
- `ping`（长连接保活，若有）

每行一般为 `event: <type>` + `data: <json>`。

### 3.3 本仓库非流式响应形态（`AnthropicProtocolAdapter.render_non_stream`）

在统一内部流事件之上拼装为：

- `id`、`type: "message"`、`role: "assistant"`、`model`、`content`（块数组）、`stop_reason`、`stop_sequence: null`
- `usage`: 当前实现为占位 `input_tokens` / `output_tokens` 的 `0`

工具开启时，`content` 可含 `thinking`、`tool_use` 或 `text` 等，与解析后的 tagged 输出一致。

### 3.4 本仓库流式响应形态

通过 `_AnthropicTaggedRenderer` 输出 SSE：含 `message_start`、`content_block_*`、`message_delta`、`message_stop`；`tool_use` 块内含 `input_json_delta`（`partial_json`）。

---

## 4. OpenAI：入参（请求体）

### 4.1 Chat Completions 根级字段（官方常见集合）

下列为生态中**常见**字段；是否可用取决于模型与账户（以 [官方文档](https://platform.openai.com/docs/api-reference/chat/create) 为准）。

| 字段 | 说明 |
|------|------|
| `model` | 必填 |
| `messages` | 必填，见 §4.2 |
| `stream` | 是否流式 |
| `stream_options` | 如 `include_usage` |
| `temperature` | 采样温度 |
| `top_p` | nucleus sampling |
| `n` | 生成条数 |
| `max_tokens` / `max_completion_tokens` | 长度上限（名称随模型代际变化） |
| `stop` | 停止序列（字符串或数组） |
| `presence_penalty` / `frequency_penalty` | 重复惩罚 |
| `logit_bias` | logit 偏置 |
| `tools` | 工具定义 |
| `tool_choice` | `auto` / `required` / `none` / `{ "type": "function", "name": "…" }` 等 |
| `parallel_tool_calls` | 是否并行多工具调用 |
| `response_format` | JSON 模式 / JSON Schema 等 |
| `user` | 终端用户标识 |
| `modalities` / `audio` 等 | 多模态扩展（依模型） |

### 4.2 `messages[]`（角色与内容）

| `role` | 典型用途 |
|--------|----------|
| `system` | 系统提示 |
| `user` | 用户输入；多模态时为 `content` 数组（`text`、`image_url` 等） |
| `assistant` | 模型回复；可含 `tool_calls` |
| `tool` | 工具执行结果，需 `tool_call_id` |

单条消息常见字段：`role`、`content`（字符串或 part 数组）、`name`（可选）、`tool_calls`（assistant）、`tool_call_id`（tool）。

### 4.3 `tools[]`（`type: "function"`）

每项通常包含：

- `type`: `"function"`
- `function.name`、`function.description`、`function.parameters`（JSON Schema）
- 部分场景有 `strict` 等扩展字段。

### 4.4 本仓库入参形态（`OpenAIChatRequest`）

`parse_request` 使用 Pydantic 校验，**显式字段**主要包括：

- `model`、`messages`、`stream`
- `tools`、`tool_choice`、`parallel_tool_calls`

`messages` 中 `OpenAIMessage` 支持：`role`、`content`（字符串或 `OpenAIContentPart` 列表）、`tool_calls`、`tool_call_id`；**额外字段** `model_config = extra: allow`，可透传其它键。

内部另设 **`resume_session_id`**、附件列表等（`exclude=True`），由适配层填充，不来自客户端 JSON 的固定模式。

---

## 5. OpenAI：出参（响应）

### 5.1 非流式 `chat.completion`

| 字段 | 说明 |
|------|------|
| `id` | 如 `chatcmpl-…` |
| `object` | `"chat.completion"` |
| `created` | Unix 时间戳 |
| `model` | 模型名 |
| `choices` | 数组，元素含 `index`、`message`、`finish_reason`、`logprobs` 等 |
| `message` | `role`、`content`；若有工具则含 `tool_calls` |
| `tool_calls[]` | `id`、`type`、`function.name`、`function.arguments`（字符串） |
| `usage` | `prompt_tokens`、`completion_tokens`、`total_tokens` 等 |
| `system_fingerprint` 等 | 依版本可选 |

`finish_reason` 常见：`stop`、`length`、`tool_calls`、`content_filter` 等。

### 5.2 流式 `chat.completion.chunk`

重复多行 `data: {JSON}`，每行一个增量：

- `choices[0].delta`：`content`、`role`、`tool_calls` 片段等
- 最后一帧常带 `finish_reason`
- 结束：`data: [DONE]`

### 5.3 错误

常见：`{ "error": { "message", "type", "param", "code" } }`，HTTP 4xx/5xx。

### 5.4 本仓库非流式 / 流式（`OpenAIProtocolAdapter`）

- **非流式**：`id`、`object: "chat.completion"`、`created`、`model`、`choices[0].message`（`role` + `content`，或经 `build_tool_calls_response` 生成 `tool_calls`）、`finish_reason`。
- **流式**：`chat.completion.chunk`，`delta.content` 或 `delta.tool_calls`；工具流中可能对 thinking 使用 `<redacted_thinking>…</redacted_thinking>` 包在文本增量里；结束 `finish_reason` + `data: [DONE]`。
- **错误**：`{ "error": { "message", "type" } }`（`invalid_request_error` / `server_error`）。

---

## 6. 概念对照（跨协议）

| 概念 | Anthropic | OpenAI |
|------|-----------|--------|
| 助手调用工具 | `content` 中 `type: "tool_use"` | `message.tool_calls` |
| 工具结果 | `user` + `tool_result`（`tool_use_id`） | `role: "tool"` + `tool_call_id` + `content` |
| 参数模式 | `input_schema` | `function.parameters` |
| 停止原因字段 | `stop_reason` | `choices[].finish_reason` |
| 用量 | `usage`（input/output tokens） | `usage`（prompt/completion/total） |

---

## 7. 延伸阅读

- Anthropic 请求细节与长示例：[anthropic-messages.md](./anthropic-messages.md)
- 官方： [Anthropic Messages](https://docs.anthropic.com/en/api/messages)、[OpenAI Chat Completions](https://platform.openai.com/docs/api-reference/chat/create)
