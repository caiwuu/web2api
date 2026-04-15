# Web2API 架构说明

新架构在**新文件**中实现，不修改现有 `config_db`、`web2api`、`multi_web2api` 等。

## 目录结构

- **config**：数据模型（`ProxyGroupConfig`、`AccountConfig`，含 `name`/`type`/`auth` JSON）与持久化（独立 DB `account_pool.sqlite3`）。
- **account**：`AccountPool`，按 type 轮询 `acquire(type)`。
- **runtime**：`ProxyKey`、`SessionCache`（session_id → 定位 page/context）、`BrowserManager`（进程与 CDP、按 type 缓存 page）。
- **plugin**：`AbstractPlugin`、`PluginRegistry`、`ClaudePlugin`（ensure_page / create_conversation / stream_completion）。
- **chat**：`ChatHandler` 与调度/执行协作类，负责会话复用、prompt 拼装、插件调用。
- **http**：OpenAI / Anthropic 路由、依赖与协议路由公共逻辑。
- **admin**：配置页鉴权与配置管理路由。
- **stream**：内部流事件模型与 SSE 编码工具。
- **shared**：跨层共享的 OpenAI 模型、session marker、tagged 工具协议包装。

## 启动

```bash
uv run python main.py
```

服务监听 `http://127.0.0.1:8001`。baseUrl 为 `http://ip:port/{type}`，例如：

- `GET  http://127.0.0.1:8001/claude/v1/models`
- `POST http://127.0.0.1:8001/claude/v1/chat/completions`

## 配置（数据库）

使用独立 SQLite：`account_pool.sqlite3`。

- **proxy_group**：proxy_host, proxy_user, proxy_pass, fingerprint_id, timezone。
- **account**：proxy_group_id, name, type, auth（JSON）。Claude 插件要求 auth 含 `sessionKey` 或 `session_key`。

**配置页**：启动服务后访问 **http://127.0.0.1:8001/config/**，可添加/编辑代理组与账号（name、type、auth JSON），保存后立即生效。

**API**：`GET /api/config` 获取配置，`PUT /api/config` 更新配置（请求体为与 GET 相同的 JSON 数组）。

也可用代码初始化示例数据：

```python
from core.config.repository import ConfigRepository
from core.config.schema import AccountConfig, ProxyGroupConfig

repo = ConfigRepository()
repo.init_schema()
repo.save_groups([
    ProxyGroupConfig(
        proxy_host="sg.arxlabs.io:3010",
        proxy_user="your_proxy_user",
        proxy_pass="your_proxy_pass",
        fingerprint_id="4567",
        timezone="America/Chicago",
        accounts=[
            AccountConfig(name="claude-01", type="claude", auth={"sessionKey": "YOUR_CLAUDE_SESSION_KEY"}),
        ],
    ),
])
```

## 会话复用

请求 content 中可带 `<!-- conv_uuid=xxx -->`，服务端若缓存中有该会话则复用。响应内容**最前面**会返回同格式注释，便于客户端下次请求携带。

## 扩展新 type

1. 实现 `core.plugin.base.AbstractPlugin`（create_page、create_conversation、stream_completion）。
2. 在应用启动前 `PluginRegistry.register(YourPlugin())`。
3. 在 account 中为该 type 添加账号，请求时使用 `/{your_type}/v1/chat/completions`。

## 常量

- CDP 端口使用 **9223**（与现有 9222 错开）。见 `core.constants`。
