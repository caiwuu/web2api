"""
FastAPI 应用组装：配置加载、账号池、会话缓存、浏览器管理、插件注册、路由挂载。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.account.pool import AccountPool
from core.admin.auth import (
    AdminLoginAttemptStore,
    AdminSessionStore,
    configured_api_keys,
    configured_config_login_lock_seconds,
    configured_config_login_max_failures,
    config_login_enabled,
    ensure_config_secret_hashed,
)
from core.chat.handler import ChatHandler
from core.config.repository import ConfigRepository
from core.config.settings import get, get_bool
from core.constants import CDP_PORT_RANGE, CHROMIUM_BIN
from core.http.anthropic_routes import create_anthropic_router
from core.http.openai_routes import create_openai_router
from core.admin.config_routes import create_config_router
from core.plugin.base import PluginRegistry
from core.plugin.claude import register_claude_plugin
from core.runtime.browser_manager import BrowserManager
from core.runtime.session_cache import SessionCache

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时初始化配置与 ChatHandler，关闭时不做持久化（会话缓存进程内）。"""
    # 注册插件
    register_claude_plugin()
    ensure_config_secret_hashed()

    repo = ConfigRepository()
    repo.init_schema()
    groups = repo.load_groups()

    chromium_bin = (get("browser", "chromium_bin") or "").strip() or CHROMIUM_BIN
    headless = get_bool("browser", "headless", False)
    no_sandbox = get_bool("browser", "no_sandbox", False)
    disable_gpu = get_bool("browser", "disable_gpu", False)
    disable_gpu_sandbox = get_bool("browser", "disable_gpu_sandbox", False)
    download_dir = (get("browser", "download_dir") or "").strip() or None
    cdp_wait_max_attempts = int(get("browser", "cdp_wait_max_attempts") or 90)
    cdp_wait_interval_seconds = float(
        get("browser", "cdp_wait_interval_seconds") or 2.0
    )
    cdp_wait_connect_timeout_seconds = float(
        get("browser", "cdp_wait_connect_timeout_seconds") or 2.0
    )
    port_start = int(get("browser", "cdp_port_start") or 9223)
    port_count = int(get("browser", "cdp_port_count") or 20)
    port_range = (
        list(range(port_start, port_start + port_count))
        if port_count > 0
        else list(CDP_PORT_RANGE)
    )
    api_keys = configured_api_keys()
    pool = AccountPool.from_groups(groups)
    session_cache = SessionCache()
    browser_manager = BrowserManager(
        chromium_bin=chromium_bin,
        headless=headless,
        no_sandbox=no_sandbox,
        disable_gpu=disable_gpu,
        disable_gpu_sandbox=disable_gpu_sandbox,
        port_range=port_range,
        cdp_wait_max_attempts=cdp_wait_max_attempts,
        cdp_wait_interval_seconds=cdp_wait_interval_seconds,
        cdp_wait_connect_timeout_seconds=cdp_wait_connect_timeout_seconds,
        download_dir=download_dir,
    )
    app.state.chat_handler = ChatHandler(
        pool=pool,
        session_cache=session_cache,
        browser_manager=browser_manager,
        config_repo=repo,
    )
    app.state.session_cache = session_cache
    app.state.browser_manager = browser_manager
    app.state.config_repo = repo
    app.state.admin_sessions = AdminSessionStore()
    app.state.admin_login_attempts = AdminLoginAttemptStore(
        max_failures=configured_config_login_max_failures(),
        lock_seconds=configured_config_login_lock_seconds(),
    )
    if not groups:
        logger.warning("数据库无配置，服务已启动但当前无可用账号")
    if api_keys:
        logger.info("API 鉴权已启用，已加载 %d 个 API Key", len(api_keys))
    if config_login_enabled():
        logger.info(
            "配置页登录已启用，失败 %d 次锁定 %d 秒",
            app.state.admin_login_attempts.max_failures,
            app.state.admin_login_attempts.lock_seconds,
        )
    try:
        await app.state.chat_handler.prewarm_resident_browsers()
    except Exception:
        logger.exception("启动预热浏览器失败")
    app.state.maintenance_task = asyncio.create_task(
        app.state.chat_handler.run_maintenance_loop()
    )
    logger.info("服务已就绪，已注册 type: %s", ", ".join(PluginRegistry.all_types()))
    yield
    task = getattr(app.state, "maintenance_task", None)
    handler = getattr(app.state, "chat_handler", None)
    if handler is not None:
        await handler.shutdown()
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            pass
    app.state.chat_handler = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Web2API(Plugin)",
        description="按 type 路由的 OpenAI 兼容接口，baseUrl: http://ip:port/{type}/v1/...",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_openai_router())
    app.include_router(create_anthropic_router())
    app.include_router(create_config_router())
    return app


app = create_app()
