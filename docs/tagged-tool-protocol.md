# Tagged Tool Output Protocol

## 1. 目标

为 `web2api` 场景定义一套比行式 ReAct 更容易解析的模型输出协议。

适用前提：

- 上游站点模型只支持纯文本输出
- 下游需要渲染为 OpenAI / Anthropic / Cursor 等协议
- 服务端不直接执行工具，而是将模型输出转换为协议层的 `tool_calls` / `tool_use`

核心思路：

- 站点模型只输出一套**强约束标签 DSL**
- 服务端先解析为**协议无关的中间语义**
- 最后由各协议 renderer 按各自标准输出

---

## 2. 为什么不用行式 ReAct

当前行式 ReAct（`Thought / Action / Action Input / Final Answer`）的主要问题：

- 标记边界弱，流式时容易遇到半截 `Action:` / `Final Answer:` 前缀
- `Action` 与 `Final Answer` 的优先级需要额外规则兜底
- `Action Input` 依赖换行和 JSON 结束位置，解析器状态复杂
- `Thinking` 展示和工具调用控制语义混在一起

标签 DSL 的目标是把这些歧义去掉。

---

## 3. 模型输出 DSL

### 3.1 合法标签

- `<think>...</think>`
- `<tool_calls>...</tool_calls>`
- `<final_answer>...</final_answer>`

### 3.2 单轮响应约束

每次模型响应的控制语义由第一个终结块决定：

1. 任意数量的 `<think>`
2. 第一个 `<tool_calls>`、`<tool_call>` 或 `<final_answer>` 作为本轮终结块

终结块之后的其余内容会被忽略。

### 3.3 形式文法

```ebnf
response        := ws? think_block* ws? terminal_block trailing*
think_block     := "<think>" think_text "</think>"
terminal_block  := tool_calls_block | tool_call_block | final_answer_block
tool_calls_block := "<tool_calls>" json_array "</tool_calls>"
tool_call_block := "<tool_call>" json_object "</tool_call>"
final_answer_block := "<final_answer>" answer_text "</final_answer>"
trailing        := any_text
ws              := (" " | "\n" | "\t" | "\r")*
```

### 3.4 `tool_calls` 内容格式

`<tool_calls>` 内必须是严格 JSON 数组，数组中的每一项格式固定为：

```json
[
  {
    "name": "Read",
    "arguments": {
      "path": "/path/to/file.py"
    }
  }
]
```

约束：

- 数组不能为空
- 每个元素的 `name` 必填，类型为字符串
- 每个元素的 `arguments` 必填，类型优先要求为对象
- 为兼容旧站点，服务端仍接受单个 `<tool_call>{...}</tool_call>`，但标准输出统一推荐 `<tool_calls>[...]</tool_calls>`
- 标签名大小写敏感，统一使用小写

### 3.5 示例

工具调用：

```xml
<think>我需要先读取文件内容</think>
<tool_calls>[{"name":"Read","arguments":{"path":"/path/to/file.py"}}]</tool_calls>
```

最终回答：

```xml
<think>我已经获得足够信息</think>
<final_answer>这里是给用户的最终回复</final_answer>
```

无思考块也合法：

```xml
<final_answer>这里是给用户的最终回复</final_answer>
```

---

## 4. 解析规则

### 4.1 解析原则

- `<think>` 仅承载展示语义，不参与工具控制判断
- `<tool_calls>`、`<tool_call>` 与 `<final_answer>` 都可作为终结块；以第一个完整终结块为准
- 服务端只在读到闭合标签后确认块结束
- `tool_calls` / `tool_call` 的 JSON 在闭合标签前可先视为原始文本缓存，闭合后再做 JSON 解析
- 多个 `<think>` 会按出现顺序拼接

### 4.2 非法输出处理

以下情况视为协议错误：

- 标签外出现非空文本
- `<tool_calls>` / `<tool_call>` 内不是合法 JSON
- JSON 缺少 `name` 或 `arguments`
- 在第一个终结块之前出现标签外非空文本

建议错误策略：

- 非流式：返回结构化错误，附原始文本
- 流式：发出 `error` 事件并结束
- 调试日志始终保留原始响应，便于 prompt 调优

---

## 5. 中间语义模型

建议区分两层：

1. 块语义：适合非流式、日志、协议 renderer 输入
2. 流式事件：适合边收边渲染

### 5.1 块语义

建议复用或对齐 `core/protocol/schemas.py` 中的 `CanonicalContentBlock` 思路。

推荐块结构：

```json
[
  { "type": "thinking", "text": "我需要先读取文件内容" },
  { "type": "tool_use", "name": "Read", "input": { "path": "/path/to/file.py" } },
  { "type": "tool_use", "name": "Read", "input": { "path": "/path/to/other.py" } }
]
```

或：

```json
[
  { "type": "thinking", "text": "我已经获得足够信息" },
  { "type": "text", "text": "这里是给用户的最终回复" }
]
```

说明：

- `thinking`：来自 `<think>`
- `tool_use`：来自 `<tool_calls>` 或 `<tool_call>`
- `text`：来自 `<final_answer>`

### 5.2 流式事件

不建议直接使用 `thinking_start` / `thinking_end` 且同时携带完整内容，因为流式时容易重复。

推荐统一为块事件：

```json
{ "type": "message_start" }
{ "type": "block_start", "block_type": "thinking" }
{ "type": "block_delta", "block_type": "thinking", "text": "我需要先" }
{ "type": "block_delta", "block_type": "thinking", "text": "读取文件内容" }
{ "type": "block_end", "block_type": "thinking" }
{ "type": "tool_call", "name": "Read", "arguments": { "path": "/path/to/file.py" } }
{ "type": "message_stop", "stop_reason": "tool_use" }
```

最终回答：

```json
{ "type": "message_start" }
{ "type": "block_start", "block_type": "thinking" }
{ "type": "block_delta", "block_type": "thinking", "text": "我已经获得足够信息" }
{ "type": "block_end", "block_type": "thinking" }
{ "type": "block_start", "block_type": "text" }
{ "type": "block_delta", "block_type": "text", "text": "这里是给用户的最终回复" }
{ "type": "block_end", "block_type": "text" }
{ "type": "message_stop", "stop_reason": "end_turn" }
```

推荐事件模型：

```json
{ "type": "message_start" }
{ "type": "block_start", "block_type": "thinking" | "text" }
{ "type": "block_delta", "block_type": "thinking" | "text", "text": "..." }
{ "type": "block_end", "block_type": "thinking" | "text" }
{ "type": "tool_call", "id": "call_xxx", "name": "Read", "arguments": { "path": "..." } }
{ "type": "message_stop", "stop_reason": "tool_use" | "end_turn" }
{ "type": "error", "error": "..." }
```

---

## 6. 协议映射

## 6.1 OpenAI

### 非流式

`thinking + tool_call`：

- `message.tool_calls`：由 `tool_call` 生成
- `message.content`：可选
- 若目标客户端是 Cursor，建议将 `thinking` 渲染为 `<think>...</think>`
- `finish_reason`：`"tool_calls"`

`thinking + final_answer`：

- `message.content`：`<think>...</think>\n\n最终答案`
- `finish_reason`：`"stop"`

### 流式

建议映射：

- `thinking` 块 -> `delta.content` 中输出 `<think>`、正文、`</think>`
- `tool_call` -> `delta.tool_calls`
- `text` 块 -> `delta.content`
- `message_stop(tool_use)` -> `finish_reason: "tool_calls"`
- `message_stop(end_turn)` -> `finish_reason: "stop"`

说明：

- 对 OpenAI 兼容客户端，`<think>` 是显示层约定，不属于 OpenAI 标准字段
- 因此 thinking 仅建议通过 `content` 文本承载

## 6.2 Anthropic

### 非流式

`thinking + tool_call`：

```json
{
  "content": [
    { "type": "thinking", "thinking": "..." },
    { "type": "tool_use", "id": "toolu_xxx", "name": "Read", "input": { "path": "..." } }
  ],
  "stop_reason": "tool_use"
}
```

`thinking + final_answer`：

```json
{
  "content": [
    { "type": "thinking", "thinking": "..." },
    { "type": "text", "text": "最终答案" }
  ],
  "stop_reason": "end_turn"
}
```

### 流式

建议映射：

- `thinking` -> `content_block_start(type=thinking)` + `thinking_delta` + `content_block_stop`
- `tool_call` -> `content_block_start(type=tool_use)` + `input_json_delta` + `content_block_stop`
- `text` -> `content_block_start(type=text)` + `text_delta` + `content_block_stop`

注意：

- 若追求严格 Anthropic 兼容，`thinking` 块的 `signature_delta` 无法从 web 站点生成
- 因此可提供两种模式：
- `compat_mode=ui`：直接输出 `thinking` block，便于前端展示
- `compat_mode=strict`：将 `thinking` 降级为普通 `text`，避免依赖 Anthropic thinking 扩展语义

## 6.3 Cursor

推荐策略：

- 走 OpenAI 兼容协议时，将 `thinking` 渲染成 `<think>...</think>` 文本
- 走 Anthropic 协议时，可优先渲染为 `thinking` block
- 不要让站点模型直接输出面向 Cursor 的协议细节；Cursor 适配应只存在于 renderer

---

## 7. Prompt 约束建议

建议系统 prompt 明确要求：

- 只允许输出 `<think>`、`<tool_calls>`、`<final_answer>` 三种标签
- 每次响应只能有一个终结块
- 不允许标签外文本
- `<tool_calls>` 内必须输出严格 JSON 数组
- 完成 `</tool_calls>` 或 `</final_answer>` 后立即停止
- 不要输出 `Observation`

推荐模板：

```text
You are a tool-capable assistant.

You must respond using only the following XML-like tags:
- <think>...</think>
- <tool_calls>[{"name":"ToolName","arguments":{...}}]</tool_calls>
- <final_answer>...</final_answer>

Rules:
- You may output one or more <think> blocks.
- You must then output exactly one terminal block: either <tool_calls> or <final_answer>.
- Do not output any text outside these tags.
- In <tool_calls>, the content must be a valid JSON array. Each item must have keys "name" and "arguments".
- If you need only one tool, still use an array with one item.
- After </tool_calls> or </final_answer>, stop immediately.
- Never output <observation>; the system will provide tool results in the next turn.
```

---

## 8. 与当前实现的关系

当前实现已经切换为：

- `core/shared/tagged_output.py`：标签协议 prompt 与非流式解析
- `core/shared/tagged_stream_parser.py`：字符级标签流式解析器
- `core/protocol/openai.py`：消费 tagged events 并输出 OpenAI tool_calls/content
- `core/protocol/anthropic.py`：消费 tagged events 并输出 Anthropic blocks/SSE

旧的行式 ReAct 已下线，不再保留兼容 fallback。

---

## 9. 推荐落地顺序

1. 使用新的 prompt，让站点模型输出标签 DSL
2. 解析为协议无关中间事件
3. 由 OpenAI / Anthropic renderer 负责各自协议输出

---

## 10. 结论

对 `web2api` 场景，推荐的主协议不是行式 ReAct，而是：

```xml
<think>...</think>
<tool_calls>[{"name":"...","arguments":{...}}]</tool_calls>
```

或：

```xml
<think>...</think>
<final_answer>...</final_answer>
```

这套格式相比当前方案的优势：

- 边界明确
- 更适合流式解析
- 更容易映射到 OpenAI / Anthropic
- `thinking` 展示与工具控制语义分离
- 更适合作为长期的协议无关中间层输入
