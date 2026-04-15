# 架构评审：协议扩展与插件扩展

本文档基于当前代码结构，分析可能阻碍**协议扩展**和**插件扩展**的设计缺陷，并给出改进方向。

---

## 一、当前架构概览

```
HTTP 请求
  → 协议适配层 (routes + ProtocolAdapter: openai / anthropic)
  → CanonicalChatRequest
  → CanonicalChatService（转成 OpenAIChatRequest）
  → ChatHandler.stream_completion(provider, OpenAIChatRequest)
  → PluginRegistry.get(provider) → 插件
  → 浏览器 / Tab / 站点
```

- **协议层**：`core/protocol/` — 各协议解析/渲染 + 统一 Canonical 模型 + 桥接到内部请求。
- **插件层**：`core/plugin/` — AbstractPlugin / BaseSitePlugin + PluginRegistry。
- **编排层**：`core/chat/handler.py` — 会话解析、Tab 调度、调用插件流式补全。

---

## 二、协议扩展相关缺陷

### 2.1 Canonical 模型对协议类型封闭

**位置**：`core/protocol/schemas.py`

```python
class CanonicalChatRequest(BaseModel):
    protocol: Literal["openai", "anthropic"]  # 新增协议需改此处
```

**问题**：每增加一种新协议（如 Gemini、Cohere），都要改 Pydantic 的 `Literal`，不利于“仅加新文件、不改旧代码”的扩展方式。

**建议**：改为 `protocol: str`，或使用 `Literal` 与 `Union` 的联合 + 从注册表推导，使新协议通过“新 adapter + 新路由”即可接入，而不必改 schemas。

---

### 2.2 内部流水线强绑定 OpenAI 形态

**位置**：`core/protocol/base.py`、`core/chat/handler.py`

- `CanonicalChatService` 将 **所有** Canonical 请求统一转成 `OpenAIChatRequest`，再交给 `ChatHandler.stream_completion(type_name, OpenAIChatRequest)`。
- `ChatHandler` 和 `extract_user_content`、`format_tagged_prompt` 等全部基于 `OpenAIMessage` / `OpenAIChatRequest`。

**问题**：

- 协议扩展在“入参/出参形态”上是开放的（新 ProtocolAdapter 即可），但在“内部语义”上被锁死在 OpenAI 风格。
- 若未来某协议或某插件需要不同的会话/工具语义（例如 native function calling、不同 history 裁剪策略），当前没有扩展点，只能改 ChatHandler 或加一堆 if/else。

**建议**：

- 中长期考虑让 ChatHandler 接受“内部标准请求”抽象（接口或 dataclass），由 ProtocolAdapter 或 CanonicalChatService 负责“协议 → 内部请求”的转换；当前可保留 `OpenAIChatRequest` 作为默认实现，但入口改为该抽象，便于后续增加其他实现。
- 工具协议相关逻辑（如 `format_tagged_prompt`、解析方式）可抽成“策略”或可插拔实现，以便不同协议/插件选用不同策略。

---

### 2.3 路由与协议适配器需手写并手动挂载

**位置**：`core/app.py`、`core/http/openai_routes.py`、`core/http/anthropic_routes.py`

- 每种协议：单独一个 router 文件、手动 `create_*_router()`、在 `app.py` 里 `include_router(...)`。
- 没有“协议注册表”或“根据注册表自动挂路由”的机制。

**问题**：加一个新协议需要改 2 ～ 3 处（新 adapter、新 router、app.py），容易遗漏且不利于第三方仅通过配置或插件注册就挂上新协议。

**建议**：引入“协议注册表”（类似 PluginRegistry），例如：

- 注册 `(path_prefix, adapter)` 或 `(path_prefix, router_factory)`；
- 在 `create_app()` 中根据注册表统一 `include_router`，这样新增协议只需注册，不必改 app.py。

---

## 三、插件扩展相关缺陷

### 3.1 插件注册写死在应用入口

**位置**：`core/app.py` lifespan

```python
# 注册插件
register_claude_plugin()
```

**问题**：每增加一个插件（如 kimi、gemini_web），都要改 `app.py` 并调用一次 `register_xxx_plugin()`。无法通过配置或 entry_points 做“发现式”注册，不利于多插件、多团队并行开发。

**建议**：

- **方案 A**：在配置（如 `config.yaml`）中列出 `plugins: [claude, kimi]`，启动时按名加载对应模块并执行 `register_*`（或统一 `PluginRegistry.register_from_module(...)`）。
- **方案 B**：使用 setuptools `entry_points`（如 `web2api.plugins`），在 `app.py` 中遍历 entry_points 并注册，这样第三方包只需在 pyproject.toml 声明即可挂载插件，无需改主仓代码。

---

### 3.2 仅有一种“站点范式”的基类

**位置**：`core/plugin/base.py`

- `AbstractPlugin`：最底层接口，协议无关。
- `BaseSitePlugin`：假定 **Cookie 认证 + 站点内 SSE 流式**，子类只需实现 fetch*workspace、create_session、build_completion*\*、parse_sse_event 等。

**问题**：若新站点是 Token 鉴权、或非 SSE（如 WebSocket、长轮询），没有对应的“第二基类”或 mixin，只能从 `AbstractPlugin` 从头实现，通用能力（如 `stream_completion_via_sse`）无法复用，扩展成本高。

**建议**：

- 在保留 BaseSitePlugin 的前提下，把 helpers 里与“Cookie”“SSE”“页面 fetch”相关的能力拆成更小粒度的函数/类（有的已在 helpers 中），并在文档中说明“非 Cookie/非 SSE 插件请实现 AbstractPlugin，可复用 helpers 中 xxx”。
- 若后续出现多例“Token + WebSocket”等模式，可再抽象出 `BaseTokenPlugin` 或类似基类，避免重复造轮子。

---

### 3.3 插件与协议通过 provider/type 字符串耦合

**位置**：路由中的 `provider`、`PluginRegistry.get(provider)`、账号配置中的 `type`。

- 路由：`/openai/{provider}/v1/...`、`/anthropic/{provider}/v1/...`，`provider` 即插件 type。
- 插件只按 type 注册，不声明“我支持哪些协议”；协议层也不声明“我支持哪些 provider”。

**问题**：若未来出现“同一站点既支持 OpenAI 又支持 Anthropic，但另一个站点只支持 OpenAI”的需求，当前设计是“一个 type 全协议通用”，没有“某插件仅部分协议可用”的显式建模，可能需要在路由或 PluginRegistry 层增加“协议 × provider”的可见性（例如插件声明 supported_protocols，或由路由只对已声明支持的协议暴露）。

当前单 type 全协议可用的假设下问题不大，但若要做细粒度控制，这里是扩展点。

---

## 四、交叉关注点

### 4.1 工具协议与协议、插件解耦不足

**位置**：`core/shared/tagged_output.py`、`core/shared/tool_calls.py`、`core/shared/tagged_stream_parser.py`；被 OpenAI/Anthropic 适配器及 ChatHandler 共用。

- 当前工具调用统一走 tagged prompt 注入 + 解析，没有“协议原生 tool_use”或“插件自定义工具格式”的扩展点。

**问题**：若某协议或某插件需要原生 function calling 或不同工具格式，需要改多处（适配器、ChatHandler、extract_user_content 等），易产生分支和重复逻辑。

**建议**：将“是否启用工具、如何拼 prompt、如何解析工具调用”抽象成策略或小接口，由协议适配器或插件通过参数/注册提供，ChatHandler 只依赖“是否有 tools + 如何组消息”的抽象，便于后续支持多种工具形态。

### 4.2 会话 ID 传递方式单一

**位置**：`core/shared/session_markers.py`（零宽字符编码）、各协议适配器在响应末尾附加 marker。

当前仅支持“在响应体末尾用零宽字符带 session_id”。

**问题**：若某协议或客户端要求用 Header、或单独字段传递 session，需要扩展解析与注入点，目前没有统一扩展接口。

**建议**：将会话 ID 的“编码/解码/注入位置”做成可配置或可插拔（例如按协议选择“body 零宽”或“header”），便于后续扩展。

---

## 五、总结表

| 类别      | 缺陷简述                                       | 对扩展的影响                      |
| --------- | ---------------------------------------------- | --------------------------------- |
| 协议      | CanonicalChatRequest.protocol 为 Literal       | 新协议需改 schemas                |
| 协议      | 内部流水线强依赖 OpenAIChatRequest/OpenAI 形态 | 非 OpenAI 语义难以接入            |
| 协议      | 路由与 adapter 手写、手挂                      | 新协议需改多文件，无统一注册      |
| 插件      | 插件在 app.py 里硬编码注册                     | 新插件必须改主仓，无法配置/发现   |
| 插件      | 仅 BaseSitePlugin（Cookie+SSE）一种范式        | 非 Cookie/非 SSE 站点实现成本高   |
| 协议+插件 | tagged 工具协议与协议、插件强耦合             | 原生 tools 或自定义工具格式难扩展 |
| 协议+插件 | 会话 ID 仅零宽一种方式                         | 其他传递方式需改多处              |

---

## 六、建议的改进优先级

1. **高**：插件发现式注册（配置或 entry_points），避免每加一个插件就改 app.py。
2. **高**：协议注册表 + 统一挂路由，新协议只加 adapter 和注册，不改 app.py。
3. **中**：CanonicalChatRequest.protocol 改为开放类型（如 str），或由注册表推导。
4. **中**：ChatHandler 入口抽象为“内部请求接口”，为未来非 OpenAI 形态留扩展点。
5. **低**：tagged 工具协议策略化；会话 ID 传递方式可插拔。

按上述顺序逐步改造，可以在不推翻现有设计的前提下，明显降低协议与插件的扩展成本，并减少对主仓的侵入式修改。
