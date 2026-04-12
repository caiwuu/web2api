"""请求调度：在持有 `schedule_lock` 的前提下选定复用会话或新的 tab/账号组合。"""

from __future__ import annotations

from typing import Any

from core.chat.runtime import ChatRuntimeCoordinator
from core.chat.state import ChatHandlerState, RequestTarget, proxy_key_for_group
from core.plugin.base import PluginRegistry


class ChatRequestScheduler:
    """负责会话复用与目标 tab/browser 分配。"""

    def __init__(
        self,
        state: ChatHandlerState,
        runtime: ChatRuntimeCoordinator,
    ) -> None:
        self._state = state
        self._runtime = runtime

    async def reuse_session_target_locked(
        self,
        plugin: Any,
        type_name: str,
        session_id: str,
    ) -> RequestTarget | None:
        """
        若缓存、账号池、tab 与插件侧会话仍一致，则复用该 session 并占用槽位。

        返回 None 表示应走 `allocate_new_target_locked`；抛错表示应让客户端稍后重试（并发/繁忙）。
        """
        entry = self._state.session_cache.get(session_id)
        if entry is None or entry.type_name != type_name:
            return None

        pair = self._state.pool.get_account_by_id(entry.account_id)
        if pair is None:
            self._runtime.invalidate_session_locked(session_id, entry)
            return None
        group, account = pair

        tab = self._state.browser_manager.get_tab(entry.proxy_key, type_name)
        if (
            tab is None
            or tab.account_id != entry.account_id
            or not plugin.has_session(session_id)
        ):
            self._runtime.invalidate_session_locked(session_id, entry)
            return None

        if not tab.accepting_new:
            self._runtime.invalidate_session_locked(session_id, entry)
            return None
        if session_id in self._state.busy_sessions:
            raise RuntimeError("当前会话正在处理中，请稍后再试")
        if tab.active_requests >= self._state.tab_max_concurrent:
            raise RuntimeError("当前会话所在 tab 繁忙，请稍后再试")

        page = self._state.browser_manager.acquire_tab(
            entry.proxy_key,
            type_name,
            self._state.tab_max_concurrent,
        )
        if page is None:
            raise RuntimeError("当前会话暂不可复用，请稍后再试")

        self._state.session_cache.touch(session_id)  # 续期 LRU
        self._state.busy_sessions.add(session_id)
        context = await self._state.browser_manager.ensure_browser(
            entry.proxy_key,
            group.proxy_pass,
        )
        return RequestTarget(
            proxy_key=entry.proxy_key,
            group=group,
            account=account,
            context=context,
            page=page,
            session_id=session_id,
            full_history=False,
        )

    async def allocate_new_target_locked(
        self,
        type_name: str,
    ) -> RequestTarget:
        """
        为「新站点会话」分配浏览器资源，优先复用轻载 tab，其次开新 tab / 切号 / 新开浏览器。

        调用方需已持有 `schedule_lock`。返回的 `session_id` 恒为 None，由 executor 调插件创建。
        """
        await self._runtime.reconcile_tabs_locked()

        # (active_requests, last_used_at, proxy_key, tab) — 数值越小越优先
        existing_tabs: list[tuple[int, float, Any, Any]] = []
        for proxy_key, entry in self._state.browser_manager.list_browser_entries():
            tab = entry.tabs.get(type_name)
            if (
                tab is not None
                and tab.accepting_new
                and tab.active_requests < self._state.tab_max_concurrent
            ):
                existing_tabs.append(
                    (tab.active_requests, tab.last_used_at, proxy_key, tab)
                )
        if existing_tabs:
            # 选最闲且最近未用的 tab，在已有浏览器上开新会话（full_history=True）
            _, _, proxy_key, tab = min(existing_tabs, key=lambda item: item[:2])
            pair = self._state.pool.get_account_by_id(tab.account_id)
            if pair is None:
                self._runtime.invalidate_tab_sessions_locked(proxy_key, type_name)
                closed = await self._state.browser_manager.close_tab(proxy_key, type_name)
                if closed is not None:
                    self._runtime.apply_closed_tabs_locked([closed])
            else:
                group, account = pair
                page = self._state.browser_manager.acquire_tab(
                    proxy_key,
                    type_name,
                    self._state.tab_max_concurrent,
                )
                if page is not None:
                    context = await self._state.browser_manager.ensure_browser(
                        proxy_key,
                        group.proxy_pass,
                    )
                    return RequestTarget(
                        proxy_key=proxy_key,
                        group=group,
                        account=account,
                        context=context,
                        page=page,
                        session_id=None,
                        full_history=True,
                    )

        # 已有浏览器但尚未开该 type 的 tab：(负载估算, last_used, proxy_key, group)
        open_browser_candidates: list[tuple[int, float, Any, Any]] = []
        for proxy_key, entry in self._state.browser_manager.list_browser_entries():
            if type_name in entry.tabs:
                continue
            group = self._state.pool.get_group_by_proxy_key(proxy_key)
            if group is None:
                continue
            if not self._state.pool.has_available_account_in_group(group, type_name):
                continue
            open_browser_candidates.append(
                (
                    self._state.browser_manager.browser_load(proxy_key),
                    entry.last_used_at,
                    proxy_key,
                    group,
                )
            )
        if open_browser_candidates:
            # 在现成浏览器上新开该 type 的 tab（仍可为新会话）
            _, _, proxy_key, group = min(
                open_browser_candidates,
                key=lambda item: item[:2],
            )
            account = self._state.pool.next_available_account_in_group(group, type_name)
            if account is not None:
                plugin = PluginRegistry.get(type_name)
                if plugin is None:
                    raise ValueError(f"未注册的 type: {type_name}")
                await self._state.browser_manager.open_tab(
                    proxy_key,
                    group.proxy_pass,
                    type_name,
                    self._state.pool.account_id(group, account),
                    plugin.create_page,
                    self._runtime.make_apply_auth_fn(plugin, account),
                )
                page = self._state.browser_manager.acquire_tab(
                    proxy_key,
                    type_name,
                    self._state.tab_max_concurrent,
                )
                if page is None:
                    raise RuntimeError("新建 tab 后仍无法占用请求槽位")
                context = await self._state.browser_manager.ensure_browser(
                    proxy_key,
                    group.proxy_pass,
                )
                return RequestTarget(
                    proxy_key=proxy_key,
                    group=group,
                    account=account,
                    context=context,
                    page=page,
                    session_id=None,
                    full_history=True,
                )

        # 空闲 tab 且同组有其他可用账号：切号复用 tab，避免起新浏览器
        switch_candidates: list[tuple[float, Any, Any]] = []
        for proxy_key, entry in self._state.browser_manager.list_browser_entries():
            tab = entry.tabs.get(type_name)
            if tab is None or tab.active_requests != 0:
                continue
            group = self._state.pool.get_group_by_proxy_key(proxy_key)
            if group is None:
                continue
            if not self._state.pool.has_available_account_in_group(
                group,
                type_name,
                exclude_account_ids={tab.account_id},
            ):
                continue
            switch_candidates.append((tab.last_used_at, proxy_key, group))
        if switch_candidates:
            _, proxy_key, group = min(switch_candidates, key=lambda item: item[0])
            tab = self._state.browser_manager.get_tab(proxy_key, type_name)
            plugin = PluginRegistry.get(type_name)
            if tab is not None and plugin is not None:
                next_account = self._state.pool.next_available_account_in_group(
                    group,
                    type_name,
                    exclude_account_ids={tab.account_id},
                )
                if next_account is not None:
                    self._runtime.invalidate_tab_sessions_locked(proxy_key, type_name)
                    switched = await self._state.browser_manager.switch_tab_account(
                        proxy_key,
                        type_name,
                        self._state.pool.account_id(group, next_account),
                        self._runtime.make_apply_auth_fn(plugin, next_account),
                    )
                    if switched:
                        page = self._state.browser_manager.acquire_tab(
                            proxy_key,
                            type_name,
                            self._state.tab_max_concurrent,
                        )
                        if page is None:
                            raise RuntimeError("切号后仍无法占用请求槽位")
                        context = await self._state.browser_manager.ensure_browser(
                            proxy_key,
                            group.proxy_pass,
                        )
                        return RequestTarget(
                            proxy_key=proxy_key,
                            group=group,
                            account=next_account,
                            context=context,
                            page=page,
                            session_id=None,
                            full_history=True,
                        )

        # 最后手段：为尚未启动浏览器的分组新开浏览器 + tab
        open_groups = {
            proxy_key.fingerprint_id
            for proxy_key in self._state.browser_manager.current_proxy_keys()
        }
        pair = self._state.pool.next_available_pair(
            type_name,
            exclude_fingerprint_ids=open_groups,
        )
        if pair is None:
            raise ValueError(f"没有类别为 {type_name!r} 的可用账号，请稍后再试")
        group, account = pair
        proxy_key = proxy_key_for_group(group)
        plugin = PluginRegistry.get(type_name)
        if plugin is None:
            raise ValueError(f"未注册的 type: {type_name}")
        await self._state.browser_manager.open_tab(
            proxy_key,
            group.proxy_pass,
            type_name,
            self._state.pool.account_id(group, account),
            plugin.create_page,
            self._runtime.make_apply_auth_fn(plugin, account),
        )
        page = self._state.browser_manager.acquire_tab(
            proxy_key,
            type_name,
            self._state.tab_max_concurrent,
        )
        if page is None:
            raise RuntimeError("新浏览器建 tab 后仍无法占用请求槽位")
        context = await self._state.browser_manager.ensure_browser(
            proxy_key,
            group.proxy_pass,
        )
        return RequestTarget(
            proxy_key=proxy_key,
            group=group,
            account=account,
            context=context,
            page=page,
            session_id=None,
            full_history=True,
        )
