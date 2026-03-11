# Web2API

> English version: see [`README.en.md`](./README.en.md)

Web2API 是一个**桥接服务**：把网页端的 AI 服务（当前主要是 Claude Web）包装成标准的 **OpenAI / Anthropic 兼容接口**，让你可以继续使用现有的 OpenAI SDK、Cursor 等客户端，而无需改动客户端代码或自己维护浏览器自动化脚本。

## 功能

- 支持图片输入
- 支持流式/非流式输出
- 支持 tools call / ReAct 工具调用
- 支持 OpenAI / Anthropic 双协议（/v1/chat/completions、/v1/messages 等）
- 提供可视化配置页（代理组、账号池、指纹、时区等）

![alt text](assets/README/image.png)
![alt text](assets/README/image-2.png)
![alt text](assets/README/image-1.png)
![alt text](assets/README/image-3.png)

## 代理与账号准备（个人推荐）

- **家宽代理获取地址**：使用 Cliproxy，进入后选择「流量代理 - 账密模式」，会话类型选择 **Sticky IP** (IP 纯净度没问题的可以使用自己的 IP)
  链接：[`Cliproxy`](https://share.cliproxy.com/share/ftdzwxt2n)
- **IP 纯度检测**：用于检测当前出口 IP 纯度与归属地  
  链接：[`https://ping0.cc/ip/`](https://ping0.cc/ip/)
- **Claude 普号获取**：可以自己注册，也可以购买成品号  
  成品号：[`https://aizhp.site/item/12`](https://aizhp.site/item/12)

## 快速开始

### Docker 启动（Linux 推荐）

> ⚠️ M 芯片 macOS 无法使用 Docker 启动，请选择源码启动

**从源码构建：**

```bash
git clone https://github.com/caiwuu/web2api.git
cd web2api
# 修改 config.yaml
docker compose up -d --build
```

**或直接拉取镜像：**

```bash
mkdir web2api && cd web2api
mkdir -p docker-data && curl -sL -o docker-data/config.yaml https://raw.githubusercontent.com/caiwuu/web2api/master/docker/config.container.yaml
# 修改 docker-data/config.yaml
docker run -d --name web2api --restart unless-stopped --platform linux/amd64 --shm-size=1g \
  -p 9000:9000 -v "$(pwd)/docker-data:/data" ghcr.io/caiwuu/web2api:latest
```

启动后：API `http://127.0.0.1:9000`，配置页 `http://127.0.0.1:9000/config`，持久化目录 `./docker-data`。

### 源码启动

**环境要求**：Python 3.12+、[uv](https://github.com/astral-sh/uv)、[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium)、可用代理、Claude `sessionKey`。

```bash
git clone https://github.com/caiwuu/web2api.git
cd web2api
uv sync
```

**Linux 无显示器**：建议用 Xvfb 虚拟屏幕，兼容性比 headless 更稳：

```bash
sudo apt install -y xvfb
xvfb-run -a -s "-screen 0 1920x1080x24" uv run python main.py
```

**配置**：修改根目录 [config.yaml](config.yaml)，至少确认 `browser.chromium_bin` 正确。详细说明见 [docs/config.md](docs/config.md)。

**启动：**

```bash
uv run python main.py
```

**配置账号**：访问 `http://127.0.0.1:9000/login`（若未配置 `auth.config_secret` 则配置页不开放），登录后到 `/config` 填入 fingerprint_id、账号 name、type=claude、auth.sessionKey，以及代理（如需要）。

**发第一条请求：**

```bash
curl -s "http://127.0.0.1:9000/claude/v1/chat/completions" \
  -H "Authorization: Bearer your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "s4", "stream": false, "messages": [{"role":"user","content":"你好"}]}'
```

## 图片输入

支持 OpenAI `image_url` 和 Anthropic `image`（base64）。格式：png/jpeg/webp/gif，单张最大 10MB，最多 5 张。远程 URL 会由服务端下载后上传到 Claude。

## 客户端注意

项目会把会话 ID 以**不可见字符**附在 assistant 回复末尾。用 OpenAI SDK / Cursor 通常不用管；若自己保存聊天记录，不要把零宽字符清洗掉，否则下一轮可能无法复用会话。

## FAQ

**为什么不直接封装网络包？** 因为登录态、前端协议、风控、会话复用都依赖真实浏览器环境。详见 [docs/faq.md](docs/faq.md)。

## API 示例

更多示例见 [docs/request_samples/all_api_curl_tests.sh](docs/request_samples/all_api_curl_tests.sh)。

## 调试用 Mock

```bash
uv run python main_mock.py
```

将 config.yaml 中 `claude.start_url` 和 `claude.api_base` 改为 `http://127.0.0.1:8002/mock`。

## 项目结构

- [main.py](main.py) / [main_mock.py](main_mock.py)：服务入口
- [core/app.py](core/app.py)：应用组装
- [core/api/](core/api)：OpenAI / Anthropic 兼容接口
- [core/plugin/](core/plugin)：站点插件
- [core/runtime/](core/runtime)：浏览器、tab、会话调度

架构文档：[docs/architecture.md](docs/architecture.md)、[docs/page-pool-scheme.md](docs/page-pool-scheme.md)

## 开发检查

```bash
uv run ruff check .
```

## 安全提醒

请勿提交到公开仓库：`db.sqlite3`、代理账号密码、`sessionKey`、抓包数据、真实对话。不要在同一个代理组下堆很多同类型账号，建议分散到不同 IP 降低风控风险。
