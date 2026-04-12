"""Chat 层共享状态：请求目标（浏览器/tab/账号绑定）与 Handler 跨组件依赖。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import BrowserContext, Page

from core.account.pool import AccountPool
from core.shared.models import OpenAIChatRequest
from core.config.repository import ConfigRepository
from core.config.schema import AccountConfig, ProxyGroupConfig
from core.config.settings import get
from core.constants import TIMEZONE
from core.runtime.browser_manager import BrowserManager
from core.runtime.keys import ProxyKey
from core.runtime.session_cache import SessionCache


def request_messages_as_dicts(req: OpenAIChatRequest) -> list[dict[str, Any]]:
    """转为 session marker helpers 需要的 list[dict]。"""
    out: list[dict[str, Any]] = []
    for message in req.messages:
        payload: dict[str, Any] = {"role": message.role}
        if isinstance(message.content, list):
            payload["content"] = [part.model_dump() for part in message.content]
        else:
            payload["content"] = message.content
        out.append(payload)
    return out


def proxy_key_for_group(group: ProxyGroupConfig) -> ProxyKey:
    """由代理分组配置构造浏览器池用的 `ProxyKey`（指纹/代理/时区等）。"""
    return ProxyKey(
        group.proxy_host,
        group.proxy_user,
        group.fingerprint_id,
        group.use_proxy,
        group.timezone or TIMEZONE,
    )


@dataclass
class RequestTarget:
    """调度结果：后续 `stream_completion` 将在该 tab 上、以该账号上下文执行。"""

    proxy_key: ProxyKey  #: 浏览器实例键（指纹 + 代理等）
    group: ProxyGroupConfig  #: 配置中的代理分组
    account: AccountConfig  #: 本请求绑定的账号
    context: BrowserContext  #: Playwright 浏览器上下文
    page: Page  #: 已占用并发槽位的目标 tab
    session_id: str | None  #: 复用站点会话时非空；新开会话时为 None，由插件创建后写入缓存
    full_history: bool  #: True 时 prompt 取完整 messages；复用会话时为 False（站点侧已有历史）


@dataclass
class ChatHandlerState:
    """`ChatHandler` 与各子组件共享的可变依赖与调度参数。"""

    pool: AccountPool  #: 账号与分组选择
    session_cache: SessionCache  #: session_id → proxy/type/account 映射
    browser_manager: BrowserManager  #: 浏览器/tab 生命周期与槽位
    config_repo: ConfigRepository | None = None  #: 可选；用于热更新、解冻时间写回
    schedule_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  #: 调度/缓存/浏览器操作互斥
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)  #: 维护循环退出信号
    busy_sessions: set[str] = field(default_factory=set)  #: 正被某请求占用的 session_id，避免并发复用
    tab_max_concurrent: int = field(
        default_factory=lambda: int(get("scheduler", "tab_max_concurrent") or 5)
    )  #: 单 tab 同时处理的请求数上限
    gc_interval_seconds: float = field(
        default_factory=lambda: float(
            get("scheduler", "browser_gc_interval_seconds") or 300
        )
    )  #: 维护循环周期间隔（秒）
    tab_idle_seconds: float = field(
        default_factory=lambda: float(get("scheduler", "tab_idle_seconds") or 900)
    )  #: 超过此时长未使用的浏览器可被回收
    resident_browser_count: int = field(
        default_factory=lambda: int(get("scheduler", "resident_browser_count", 1))
    )  #: 启动预热时至少保持「有 tab」的分组数量
