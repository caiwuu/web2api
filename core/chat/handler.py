"""
聊天请求编排门面：公开配置刷新、维护循环和 OpenAI 语义事件流接口。

内部实现拆为三块：

- `runtime.py`：浏览器/tab/session 运行时维护
- `scheduler.py`：会话复用与目标分配
- `executor.py`：单次请求执行与流式事件包装
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.chat.executor import ChatRequestExecutor
from core.chat.runtime import ChatRuntimeCoordinator
from core.chat.scheduler import ChatRequestScheduler
from core.chat.state import ChatHandlerState
from core.shared.models import OpenAIChatRequest
from core.config.repository import ConfigRepository
from core.config.schema import ProxyGroupConfig
from core.account.pool import AccountPool
from core.runtime.browser_manager import BrowserManager
from core.runtime.session_cache import SessionCache
from core.stream.events import OpenAIStreamEvent


class ChatHandler:
    """编排一次 chat 请求：对外保持稳定 API，内部委托给协作类。"""

    def __init__(
        self,
        pool: AccountPool,
        session_cache: SessionCache,
        browser_manager: BrowserManager,
        config_repo: ConfigRepository | None = None,
    ) -> None:
        # _state：调度锁、busy_sessions、配置项阈值等均挂在此共享对象上
        self._state = ChatHandlerState(
            pool=pool,
            session_cache=session_cache,
            browser_manager=browser_manager,
            config_repo=config_repo,
        )
        self._runtime = ChatRuntimeCoordinator(self._state)  # 浏览器生命周期与会话失效
        self._scheduler = ChatRequestScheduler(self._state, self._runtime)  # 选 tab / 复用 session
        self._executor = ChatRequestExecutor(
            self._state,
            self._runtime,
            self._scheduler,
        )  # 拼 prompt 与插件流式调用

    def reload_pool(
        self,
        groups: list[ProxyGroupConfig],
        config_repo: ConfigRepository | None = None,
    ) -> None:
        """同步替换内存中的分组/账号池（不跑异步回收）；一般配合 `refresh_configuration` 使用。"""
        self._runtime.reload_pool(groups, config_repo)

    async def refresh_configuration(
        self,
        groups: list[ProxyGroupConfig],
        config_repo: ConfigRepository | None = None,
    ) -> None:
        """配置热更新：重载池、修剪失效 tab/浏览器、对齐槽位，并重新预热常驻浏览器。"""
        await self._runtime.refresh_configuration(groups, config_repo)

    async def prewarm_resident_browsers(self) -> None:
        """为前 N 个可用分组启动浏览器并打开各 type 的首个 tab，降低首个请求冷启动延迟。"""
        await self._runtime.prewarm_resident_browsers()

    async def run_maintenance_loop(self) -> None:
        """后台循环：按间隔回收空闲浏览器，并处理 draining/frozen tab 的收尾。"""
        await self._runtime.run_maintenance_loop()

    async def shutdown(self) -> None:
        """请求维护循环退出并关闭所有浏览器实例。"""
        await self._runtime.shutdown()

    def report_account_unfreeze(
        self,
        fingerprint_id: str,
        account_name: str,
        unfreeze_at: int,
    ) -> None:
        """插件上报限流结束时间：持久化解冻时间并重载池，使调度重新允许该账号。"""
        self._runtime.report_account_unfreeze(
            fingerprint_id,
            account_name,
            unfreeze_at,
        )

    def get_account_runtime_status(self) -> dict[str, dict[str, Any]]:
        """返回当前已打开 tab 对应的账号运行时快照（活跃请求、冻结状态等）。"""
        return self._runtime.get_account_runtime_status()

    async def stream_openai_events(
        self,
        type_name: str,
        req: OpenAIChatRequest,
    ) -> AsyncIterator[OpenAIStreamEvent]:
        """执行一次 OpenAI 形态的 chat：产出中间态流式事件（delta + finish），供协议层编码。"""
        async for event in self._executor.stream_openai_events(type_name, req):
            yield event
