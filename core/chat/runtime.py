"""浏览器与 tab 运行时协调：配置热更新、预热、维护回收、会话/tab 失效与收尾。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from core.chat.state import ChatHandlerState, proxy_key_for_group
from core.config.repository import ConfigRepository
from core.config.schema import ProxyGroupConfig
from core.plugin.base import BaseSitePlugin, PluginRegistry
from core.plugin.helpers import clear_cookies_for_domain
from core.runtime.browser_manager import ClosedTabInfo
from core.runtime.keys import ProxyKey
from core.runtime.session_cache import SessionEntry

logger = logging.getLogger(__name__)


class ChatRuntimeCoordinator:
    """维护浏览器/tab/session 运行时资源。"""

    def __init__(self, state: ChatHandlerState) -> None:
        self._state = state  # 与 scheduler/executor 共享

    def reload_pool(
        self,
        groups: list[ProxyGroupConfig],
        config_repo: ConfigRepository | None = None,
    ) -> None:
        """配置热更新后替换账号池与 repository。"""
        self._state.pool.reload(groups)
        if config_repo is not None:
            self._state.config_repo = config_repo

    async def refresh_configuration(
        self,
        groups: list[ProxyGroupConfig],
        config_repo: ConfigRepository | None = None,
    ) -> None:
        """配置热更新：替换账号池、清理失效资源，并重新预热常驻浏览器。"""
        async with self._state.schedule_lock:
            self.reload_pool(groups, config_repo)
            await self.prune_invalid_resources_locked()
            await self.reconcile_tabs_locked()
        await self.prewarm_resident_browsers()

    async def prewarm_resident_browsers(self) -> None:
        """预热常驻浏览器：已有浏览器数 >= resident_browser_count 时跳过。"""
        async with self._state.schedule_lock:
            running = self._state.browser_manager.browser_count()
            if running >= self._state.resident_browser_count:
                return
            warmed = running
            for group in self._state.pool.groups():
                if warmed >= self._state.resident_browser_count:
                    break
                available_types = {
                    account.type
                    for account in group.accounts
                    if account.is_available()
                    and PluginRegistry.get(account.type) is not None
                }
                if not available_types:
                    continue
                proxy_key = proxy_key_for_group(group)
                await self._state.browser_manager.ensure_browser(
                    proxy_key, group.proxy_pass
                )
                for type_name in sorted(available_types):
                    if (
                        self._state.browser_manager.get_tab(proxy_key, type_name)
                        is not None
                    ):
                        continue
                    accounts = self._state.pool.available_accounts_in_group(
                        group, type_name
                    )
                    if not accounts:
                        continue
                    account = accounts[0]
                    plugin = PluginRegistry.get(type_name)
                    if plugin is None:
                        continue
                    await self._state.browser_manager.open_tab(
                        proxy_key,
                        group.proxy_pass,
                        type_name,
                        self._state.pool.account_id(group, account),
                        plugin.create_page,
                    )
                warmed += 1

    async def run_maintenance_loop(self) -> None:
        """周期性回收空闲浏览器，并收尾 drained/frozen tab。"""
        while not self._state.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._state.stop_event.wait(),
                    timeout=self._state.gc_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                async with self._state.schedule_lock:
                    await self.reconcile_tabs_locked()
                    closed = await self._state.browser_manager.collect_idle_browsers(
                        idle_seconds=self._state.tab_idle_seconds,
                        resident_browser_count=self._state.resident_browser_count,
                    )
                    self.apply_closed_tabs_locked(closed)
            except Exception:
                logger.exception("维护循环执行失败")

    async def shutdown(self) -> None:
        """停止维护循环并关闭全部浏览器。"""
        self._state.stop_event.set()
        async with self._state.schedule_lock:
            closed = await self._state.browser_manager.close_all()
            self.apply_closed_tabs_locked(closed)

    def report_account_unfreeze(
        self,
        fingerprint_id: str,
        type_name: str,
        unfreeze_at: int,
    ) -> None:
        """记录某 type 的解冻时间并重载池，使后续 acquire 按当前时间判断可用性。"""
        if self._state.config_repo is None:
            return
        self._state.config_repo.update_account_unfreeze_at(
            fingerprint_id,
            type_name,
            unfreeze_at,
        )
        self.reload_pool(self._state.config_repo.load_groups())

    def get_account_runtime_status(self) -> dict[str, dict[str, Any]]:
        """返回当前账号运行时状态，供配置页展示角标。

        key 格式为 ``account_id::type_name``，避免同一 identity 下
        不同 type 共享 account_id 时互相覆盖。
        """
        status: dict[str, dict[str, Any]] = {}
        for proxy_key, entry in self._state.browser_manager.list_browser_entries():
            for type_name, tab in entry.tabs.items():
                key = f"{tab.account_id}::{type_name}"
                status[key] = {
                    "fingerprint_id": proxy_key.fingerprint_id,
                    "type": type_name,
                    "is_active": True,
                    "tab_state": tab.state,
                    "accepting_new": tab.accepting_new,
                    "active_requests": tab.active_requests,
                    "frozen_until": tab.frozen_until,
                }
        return status

    def apply_closed_tabs_locked(self, closed_tabs: list[ClosedTabInfo]) -> None:
        """tab/浏览器关闭后：清 session 缓存并通知插件丢弃对应站点会话。"""
        for info in closed_tabs:
            self._state.session_cache.delete_many(info.session_ids)
            plugin = PluginRegistry.get(info.type_name)
            if plugin is not None:
                plugin.drop_sessions(info.session_ids)

    async def clear_tab_domain_cookies_if_supported(
        self,
        proxy_key: ProxyKey,
        type_name: str,
    ) -> None:
        """关 tab 前清该 type 对应域名的 cookie（仅支持带 site.cookie_domain 的插件）。"""
        entry = self._state.browser_manager.get_browser_entry(proxy_key)
        if entry is None:
            return
        plugin = PluginRegistry.get(type_name)
        if not isinstance(plugin, BaseSitePlugin) or not getattr(plugin, "site", None):
            return
        try:
            await clear_cookies_for_domain(entry.context, plugin.site.cookie_domain)
        except Exception as exc:
            logger.debug("关 tab 前清 cookie 失败 type=%s: %s", type_name, exc)

    async def prune_invalid_resources_locked(self) -> None:
        """关闭配置中已不存在或已无可用账号的浏览器/tab。"""
        for proxy_key, entry in list(
            self._state.browser_manager.list_browser_entries()
        ):
            group = self._state.pool.get_group_by_proxy_key(proxy_key)
            if group is None:
                self.apply_closed_tabs_locked(
                    await self._state.browser_manager.close_browser(proxy_key)
                )
                continue
            # 该 group 下所有 type 都无可用账号 → 关闭整个浏览器
            has_any_available = any(a.is_available() for a in group.accounts)
            if not has_any_available:
                self.apply_closed_tabs_locked(
                    await self._state.browser_manager.close_browser(proxy_key)
                )
                continue
            for type_name in list(entry.tabs.keys()):
                tab = entry.tabs[type_name]
                pair = self._state.pool.get_account_by_id(tab.account_id)
                # 账号被删、类型不匹配或已禁用：失效插件会话，空闲则切号或关 tab
                if (
                    pair is None
                    or pair[0] is not group
                    or pair[1].type != type_name
                    or not pair[1].enabled
                ):
                    self.invalidate_tab_sessions_locked(proxy_key, type_name)
                    if tab.active_requests == 0:
                        switched = False
                        current_group = self._state.pool.get_group_by_proxy_key(
                            proxy_key
                        )
                        if current_group is not None:
                            next_account = (
                                self._state.pool.next_available_account_in_group(
                                    current_group,
                                    type_name,
                                    exclude_account_ids={tab.account_id},
                                )
                            )
                            if next_account is not None:
                                    switched = await self._state.browser_manager.switch_tab_account(
                                        proxy_key,
                                        type_name,
                                        self._state.pool.account_id(
                                            current_group,
                                            next_account,
                                        ),
                                    )
                        if not switched:
                            await self.clear_tab_domain_cookies_if_supported(
                                proxy_key,
                                type_name,
                            )
                            closed = await self._state.browser_manager.close_tab(
                                proxy_key,
                                type_name,
                            )
                            if closed is not None:
                                self.apply_closed_tabs_locked([closed])
                    else:
                        self._state.browser_manager.mark_tab_draining(
                            proxy_key, type_name
                        )

    def invalidate_session_locked(
        self,
        session_id: str,
        entry: SessionEntry | None = None,
    ) -> None:
        """移除单条站点会话：缓存、浏览器注册表与插件内存一致清空。"""
        entry = entry or self._state.session_cache.get(session_id)
        if entry is None:
            return
        self._state.session_cache.delete(session_id)
        self._state.browser_manager.unregister_session(
            entry.proxy_key,
            entry.type_name,
            session_id,
        )
        plugin = PluginRegistry.get(entry.type_name)
        if plugin is not None:
            plugin.drop_session(session_id)

    def invalidate_tab_sessions_locked(
        self,
        proxy_key: ProxyKey,
        type_name: str,
    ) -> None:
        """该 tab 上所有站点会话一并失效（切号、关 tab 前调用）。"""
        tab = self._state.browser_manager.get_tab(proxy_key, type_name)
        if tab is None or not tab.sessions:
            return
        session_ids = list(tab.sessions)
        self._state.session_cache.delete_many(session_ids)
        plugin = PluginRegistry.get(type_name)
        if plugin is not None:
            plugin.drop_sessions(session_ids)
        tab.sessions.clear()

    def revive_tab_if_possible_locked(
        self,
        proxy_key: ProxyKey,
        type_name: str,
    ) -> bool:
        """
        draining/frozen 的 tab 若绑定账号已恢复可用，则重新接受新请求。

        返回 True 表示已恢复为可调度；False 表示仍需 reconcile 走切号或关闭。
        """
        tab = self._state.browser_manager.get_tab(proxy_key, type_name)
        if tab is None or tab.active_requests != 0:
            return False
        if tab.accepting_new:
            return True

        pair = self._state.pool.get_account_by_id(tab.account_id)
        if pair is None:
            return False
        _, account = pair
        if not account.is_available():
            return False
        tab.accepting_new = True
        tab.state = "ready"
        tab.frozen_until = None
        tab.last_used_at = time.time()
        return True

    async def reconcile_tabs_locked(self) -> None:
        """
        收尾所有 non-ready tab：

        - 若原账号已恢复可用，则恢复 tab
        - 否则若同组有其他可用账号，则在 drained 后切号
        - 否则关闭 tab
        """
        for proxy_key, entry in list(
            self._state.browser_manager.list_browser_entries()
        ):
            for type_name in list(entry.tabs.keys()):
                tab = entry.tabs[type_name]
                # 仅处理「不接纳新请求且无在途请求」的 tab
                if tab.accepting_new or tab.active_requests != 0:
                    continue
                if self.revive_tab_if_possible_locked(proxy_key, type_name):
                    continue

                group = self._state.pool.get_group_by_proxy_key(proxy_key)
                if group is None:
                    await self.clear_tab_domain_cookies_if_supported(
                        proxy_key, type_name
                    )
                    closed = await self._state.browser_manager.close_tab(
                        proxy_key,
                        type_name,
                    )
                    if closed is not None:
                        self.apply_closed_tabs_locked([closed])
                    continue

                next_account = self._state.pool.next_available_account_in_group(
                    group,
                    type_name,
                    exclude_account_ids={tab.account_id},
                )
                if next_account is not None:
                    self.invalidate_tab_sessions_locked(proxy_key, type_name)
                    switched = await self._state.browser_manager.switch_tab_account(
                        proxy_key,
                        type_name,
                        self._state.pool.account_id(group, next_account),
                    )
                    if switched:
                        continue

                await self.clear_tab_domain_cookies_if_supported(proxy_key, type_name)
                closed = await self._state.browser_manager.close_tab(
                    proxy_key, type_name
                )
                if closed is not None:
                    self.apply_closed_tabs_locked([closed])
