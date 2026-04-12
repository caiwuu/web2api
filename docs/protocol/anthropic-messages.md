# Anthropic Messages API：`POST /v1/messages` 入参结构

**端点**：`POST https://api.anthropic.com/v1/messages`  
**认证**：请求头 `x-api-key`、`anthropic-version`（及可选的 `anthropic-beta`）。

以下描述 **JSON 请求体**；字段名与是否必填以 [官方 Reference](https://docs.anthropic.com/en/api/messages) 为准，Beta 能力需配合 `anthropic-beta` / SDK `betas`。

---

## 1. 根对象（Request Body）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `model` | 是 | string | 模型 ID |
| `max_tokens` | 是 | number | 单次回复最多生成的 token 数（上限因模型而异） |
| `messages` | 是 | array | 对话消息列表（见 §2） |
| `system` | 否 | string \| array | 系统提示：纯文本，或内容块数组（可与 prompt caching 等配合） |
| `metadata` | 否 | object | 例如 `{ "user_id": "<id>" }` |
| `temperature` | 否 | number | 采样温度 |
| `top_p` | 否 | number | nucleus sampling |
| `top_k` | 否 | number | top-k |
| `stop_sequences` | 否 | string[] | 命中则停止生成 |
| `stream` | 否 | boolean | `true` 时 SSE 流式响应 |
| `stream_options` | 否 | object | 流式时选项（如 `include_usage`，依版本文档） |
| `tools` | 否 | array | 工具定义（见 §4） |
| `tool_choice` | 否 | object \| string | 工具选择策略（见 §5） |
| `thinking` | 否 | object | 扩展思考（Extended Thinking），如 `{ "type": "enabled", "budget_tokens": <n> }` |
| `output_config` | 否 | object | 结构化输出等（依版本） |
| `service_tier` | 否 | string | 服务层级（若账号/模型支持） |

**约定**：标准 Messages API **没有** `messages[].role: "system"`；系统提示只用顶层 `system`。工具结果放在 **`role: "user"`** 的消息里，使用 `tool_result` 内容块（见 §3.5）。

---

## 2. `messages[]` 单条消息

```json
{
  "role": "user | assistant",
  "content": "字符串 或 内容块数组"
}
```

- **`role`**：只能是 `user` 或 `assistant`。
- **`content`**：
  - **字符串**：等价于单段文本；
  - **数组**：多模态/工具等多块内容（§3）。

常见多轮结构：`user` ↔ `assistant` 交替。含工具时：`user` → `assistant`（含 `tool_use`）→ `user`（含 `tool_result`）→ `assistant` …

---

## 3. 内容块 `content[]`（`type` 分情况）

### 3.1 `text`

```json
{ "type": "text", "text": "..." }
```

可选：`cache_control`（prompt caching，如 `ephemeral` + `ttl`）、`citations` 相关字段（若启用对应能力）。

### 3.2 `image`

**Base64：**

```json
{
  "type": "image",
  "source": {
    "type": "base64",
    "media_type": "image/jpeg | image/png | image/gif | image/webp",
    "data": "<base64>"
  }
}
```

**URL（模型/SDK 支持时）：**

```json
{
  "type": "image",
  "source": {
    "type": "url",
    "url": "https://..."
  }
}
```

### 3.3 `document`（如 PDF，视模型/权限）

```json
{
  "type": "document",
  "source": {
    "type": "base64",
    "media_type": "application/pdf",
    "data": "<base64>"
  }
}
```

### 3.4 `tool_use`（通常在 **`assistant`** 消息中）

```json
{
  "type": "tool_use",
  "id": "toolu_...",
  "name": "tool_name",
  "input": {}
}
```

### 3.5 `tool_result`（在 **`user`** 消息中）

```json
{
  "type": "tool_result",
  "tool_use_id": "<与 assistant 中 tool_use.id 一致>",
  "content": "字符串 或 内容块数组",
  "is_error": false
}
```

- `content` 可为纯文本，或 `text` / `image` 等块数组。
- `is_error: true` 表示工具执行失败。

### 3.6 思考类块（`thinking` / `redacted_thinking` 等）

扩展思考开启时，**响应**中可出现思考块；若需在多轮中回传，以当前模型文档为准（是否允许、格式约束）。

---

## 4. `tools[]` 工具定义

**自定义工具（常见）：**

```json
{
  "name": "get_weather",
  "description": "...",
  "input_schema": {
    "type": "object",
    "properties": {},
    "required": []
  }
}
```

**内置 / Beta 工具**（如 computer use、web search、code execution、MCP 等）通常带额外字段（如 `type`），并需对应 Beta 头与文档版本。

---

## 5. `tool_choice`

常见形态（与 SDK 简写可能略有差异，以官方为准）：

| 含义 | 示例 |
|------|------|
| 自动（默认） | `{ "type": "auto" }` |
| 必须调用某工具 | `{ "type": "tool", "name": "<name>" }` |
| 必须调用某类工具 | `{ "type": "any" }`（名称以文档为准） |
| 禁用工具 | `{ "type": "none" }` |

并行调用相关开关（若存在）以官方参数名为准。

---

## 6. 流式 `stream: true`

请求体字段与非流式相同；响应为 **SSE**，事件类型包括 `message_start`、`content_block_start`、`content_block_delta` 等（见流式文档）。

---

## 7. 与本项目 `AnthropicProtocolAdapter` 的对应关系

实现位于 `core/protocol/anthropic.py`：

- **会读取**的顶层字段：`model`、`messages`、`system`、`stream`、`tools`、`tool_choice`，以及 **`parallel_tool_calls`**（布尔，用于内部 `OpenAIChatRequest`，非 Anthropic 官方字段）。
- **内容块**：解析 `text`、`thinking`、`image`（`source.type == base64`）、`tool_use`、`tool_result`；`tool_result` 内嵌 `content` 再按块解析。
- **角色**：若 `role` 为 `user` 且含 `tool_result` 块，内部会映射为 **`tool`** 角色以对接统一后端。
- **工具**：`tools[].name` / `description` / `input_schema` 映射为 OpenAI 形态的 `function`/`parameters`。
- **Session**：从 `system` 与消息文本中解析 session 标记（`session_markers`），用于会话续接。

若网关行为与官方 API 有差异，以本适配器代码为准。

---

## 8. 最小示例

```json
{
  "model": "claude-sonnet-4-20250514",
  "max_tokens": 1024,
  "messages": [
    { "role": "user", "content": "Hello" }
  ]
}
```

---

## 9. 最完整示例（单请求覆盖常见字段）

下面是一份**尽量叠齐**字段的示例：同一请求里同时出现「系统块 + 缓存提示」「多模态用户消息」「助手 `tool_use`」「用户 `tool_result`（字符串与块数组两种）」「含 `is_error` 的工具结果」「采样/停止序列/流式/扩展思考/工具定义与 `tool_choice`」等。

**使用前请自行核对**：① 模型是否支持扩展思考、PDF、URL 图片、流式 `include_usage` 等；② `thinking` 与部分参数/模型组合可能互斥，以官方文档为准；③ `tool_use_id` 必须与**上一段助手消息里对应**的 `tool_use.id` 一致；④ Base64、PDF 为占位，需换成真实数据。

```json
{
  "model": "claude-sonnet-4-20250514",
  "max_tokens": 8192,
  "metadata": {
    "user_id": "user_abc123"
  },
  "system": [
    {
      "type": "text",
      "text": "You are a careful assistant. Prefer tools when facts are needed.",
      "cache_control": {
        "type": "ephemeral",
        "ttl": "5m"
      }
    }
  ],
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Compare this 1x1 PNG with the linked photo, and summarize the attached PDF title page."
        },
        {
          "type": "image",
          "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
          }
        },
        {
          "type": "image",
          "source": {
            "type": "url",
            "url": "https://example.com/sample.jpg"
          }
        },
        {
          "type": "document",
          "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": "JVBERi0xLjQK..."
          }
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "tool_use",
          "id": "toolu_01AbCdEfGhIjKlMnOpQrStUvWx",
          "name": "get_weather",
          "input": {
            "city": "San Francisco",
            "unit": "celsius"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_01AbCdEfGhIjKlMnOpQrStUvWx",
          "is_error": false,
          "content": [
            {
              "type": "text",
              "text": "Current weather: 18°C, partly cloudy."
            }
          ]
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "text",
          "text": "It is about 18°C and partly cloudy in San Francisco."
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "tool_use",
          "id": "toolu_02YzXwVuTsRqPoNmLkJiHgFeDcBa",
          "name": "get_weather",
          "input": {
            "city": "InvalidCity%%%",
            "unit": "celsius"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_02YzXwVuTsRqPoNmLkJiHgFeDcBa",
          "is_error": true,
          "content": "Geocoding failed: unknown city."
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "tool_use",
          "id": "toolu_03PlainStringOk",
          "name": "echo",
          "input": {
            "message": "ping"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_03PlainStringOk",
          "is_error": false,
          "content": "Plain string tool_result is also valid when you do not need blocks."
        }
      ]
    }
  ],
  "temperature": 0.7,
  "top_p": 0.95,
  "top_k": 40,
  "stop_sequences": ["\n\nHuman:", "<<END>>"],
  "stream": true,
  "stream_options": {
    "include_usage": true
  },
  "thinking": {
    "type": "enabled",
    "budget_tokens": 2048
  },
  "tools": [
    {
      "name": "get_weather",
      "description": "Get current weather for a city name or coordinates.",
      "input_schema": {
        "type": "object",
        "properties": {
          "city": {
            "type": "string",
            "description": "City name in English"
          },
          "unit": {
            "type": "string",
            "enum": ["celsius", "fahrenheit"],
            "description": "Temperature unit"
          }
        },
        "required": ["city"]
      }
    },
    {
      "name": "echo",
      "description": "Echo a short message (demo tool for plain-string tool_result).",
      "input_schema": {
        "type": "object",
        "properties": {
          "message": {
            "type": "string"
          }
        },
        "required": ["message"]
      }
    }
  ],
  "tool_choice": {
    "type": "auto"
  },
  "service_tier": "standard"
}
```

**网关 / 本项目扩展**（非 Anthropic 官方必填）：若走 `web2api` 的 Anthropic 兼容层，可在同级增加 `"parallel_tool_calls": false` 以传入内部 `OpenAIChatRequest`（见 §7）。

**未写入大 JSON 的字段**（避免与版本强绑定）：`output_config`（结构化 JSON 输出）、Beta 专用 `tools` 条目（如 `type: "code_execution_..."`）、`betas` / 请求头 `anthropic-beta` 等——需要时按官方文档单独拼接。
