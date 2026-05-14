"""
聊天请求编排：解析 session_id、调度 browser/tab/session、调用插件流式补全，
并在响应末尾附加零宽字符编码的会话 ID。

当前调度模型：

- 一个浏览器对应一个代理组
- 一个浏览器内，一个 type 只有一个 tab
- 一个 tab 绑定一个 account，只有 drained 后才能切号
- 一个 session 绑定到某个 tab/account；复用成功时不传完整历史
- 无法复用时，新建会话并回放完整历史
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, cast

from playwright.async_api import BrowserContext, Page

from core.account.pool import AccountPool
from core.config.repository import ConfigRepository
from core.config.schema import AccountConfig, ProxyGroupConfig
from core.config.settings import get
from core.constants import TIMEZONE
from core.plugin.base import AccountFrozenError, BaseSitePlugin, PluginRegistry
from core.plugin.helpers import clear_cookies_for_domain
from core.runtime.browser_manager import BrowserManager, ClosedTabInfo, TabRuntime
from core.runtime.keys import ProxyKey
from core.runtime.session_cache import SessionCache, SessionEntry

from core.api.conv_parser import parse_conv_uuid_from_messages, session_id_suffix
from core.api.schemas import OpenAIChatRequest, extract_user_content
from core.api.tagged_output import format_tagged_prompt
from core.hub.schemas import OpenAIStreamEvent

logger = logging.getLogger(__name__)


def _request_messages_as_dicts(req: OpenAIChatRequest) -> list[dict[str, Any]]:
    """转为 conv_parser 需要的 list[dict]。"""
    out: list[dict[str, Any]] = []
    for m in req.messages:
        d: dict[str, Any] = {"role": m.role}
        if isinstance(m.content, list):
            d["content"] = [p.model_dump() for p in m.content]
        else:
            d["content"] = m.content
        out.append(d)
    return out


def _proxy_key_for_group(group: ProxyGroupConfig) -> ProxyKey:
    return ProxyKey(
        group.proxy_host,
        group.proxy_user,
        group.fingerprint_id,
        group.use_proxy,
        group.timezone or TIMEZONE,
    )


@dataclass
class _RequestTarget:
    proxy_key: ProxyKey
    group: ProxyGroupConfig
    account: AccountConfig
    context: BrowserContext
    page: Page
    session_id: str | None
    full_history: bool


class ChatHandler:
    """编排一次 chat 请求：会话解析、tab 调度、插件调用。"""

    def __init__(
        self,
        pool: AccountPool,
        session_cache: SessionCache,
        browser_manager: BrowserManager,
        config_repo: ConfigRepository | None = None,
    ) -> None:
        self._pool = pool
        self._session_cache = session_cache
        self._browser_manager = browser_manager
        self._config_repo = config_repo
        self._schedule_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._busy_sessions: set[str] = set()
        self._tab_max_concurrent = int(get("scheduler", "tab_max_concurrent") or 5)
        self._gc_interval_seconds = float(
            get("scheduler", "browser_gc_interval_seconds") or 300
        )
        self._tab_idle_seconds = float(get("scheduler", "tab_idle_seconds") or 900)
        self._resident_browser_count = int(
            get("scheduler", "resident_browser_count", 1)
        )

    def reload_pool(
        self,
        groups: list[ProxyGroupConfig],
        config_repo: ConfigRepository | None = None,
    ) -> None:
        """配置热更新后替换账号池与 repository。"""
        self._pool.reload(groups)
        if config_repo is not None:
            self._config_repo = config_repo

    async def refresh_configuration(
        self,
        groups: list[ProxyGroupConfig],
        config_repo: ConfigRepository | None = None,
    ) -> None:
        """配置热更新：替换账号池、清理失效资源，并重新预热常驻浏览器。"""
        async with self._schedule_lock:
            self.reload_pool(groups, config_repo)
            await self._prune_invalid_resources_locked()
            await self._reconcile_tabs_locked()
        await self.prewarm_resident_browsers()

    async def prewarm_resident_browsers(self) -> None:
        """启动时预热常驻浏览器，并为其下可用 type 建立 tab。"""
        async with self._schedule_lock:
            warmed = 0
            for group in self._pool.groups():
                if warmed >= self._resident_browser_count:
                    break
                available_types = {
                    a.type
                    for a in group.accounts
                    if a.is_available() and PluginRegistry.get(a.type) is not None
                }
                if not available_types:
                    continue
                proxy_key = _proxy_key_for_group(group)
                await self._browser_manager.ensure_browser(proxy_key, group.proxy_pass)
                for type_name in sorted(available_types):
                    if self._browser_manager.get_tab(proxy_key, type_name) is not None:
                        continue
                    account = self._pool.available_accounts_in_group(group, type_name)
                    if not account:
                        continue
                    chosen = account[0]
                    plugin = PluginRegistry.get(type_name)
                    if plugin is None:
                        continue
                    await self._browser_manager.open_tab(
                        proxy_key,
                        group.proxy_pass,
                        type_name,
                        self._pool.account_id(group, chosen),
                        plugin.create_page,
                        self._make_apply_auth_fn(plugin, chosen),
                    )
                warmed += 1

    async def run_maintenance_loop(self) -> None:
        """周期性回收空闲浏览器，并收尾 drained/frozen tab。"""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._gc_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                async with self._schedule_lock:
                    await self._reconcile_tabs_locked()
                    closed = await self._browser_manager.collect_idle_browsers(
                        idle_seconds=self._tab_idle_seconds,
                        resident_browser_count=self._resident_browser_count,
                    )
                    self._apply_closed_tabs_locked(closed)
            except Exception:
                logger.exception("维护循环执行失败")

    async def shutdown(self) -> None:
        """停止维护循环并关闭全部浏览器。"""
        self._stop_event.set()
        async with self._schedule_lock:
            closed = await self._browser_manager.close_all()
            self._apply_closed_tabs_locked(closed)

    def report_account_unfreeze(
        self,
        fingerprint_id: str,
        account_name: str,
        unfreeze_at: int,
    ) -> None:
        """记录账号解冻时间并重载池，使后续 acquire 按当前时间判断可用性。"""
        if self._config_repo is None:
            return
        self._config_repo.update_account_unfreeze_at(
            fingerprint_id, account_name, unfreeze_at
        )
        self.reload_pool(self._config_repo.load_groups())

    def get_account_runtime_status(self) -> dict[str, dict[str, Any]]:
        """返回当前账号运行时状态，供配置页展示角标。"""
        status: dict[str, dict[str, Any]] = {}
        for proxy_key, entry in self._browser_manager.list_browser_entries():
            for type_name, tab in entry.tabs.items():
                status[tab.account_id] = {
                    "fingerprint_id": proxy_key.fingerprint_id,
                    "type": type_name,
                    "is_active": True,
                    "tab_state": tab.state,
                    "accepting_new": tab.accepting_new,
                    "active_requests": tab.active_requests,
                    "frozen_until": tab.frozen_until,
                }
        return status

    def _make_apply_auth_fn(
        self,
        plugin: Any,
        account: AccountConfig,
    ) -> Any:
        async def _apply_auth(context: BrowserContext, page: Page) -> None:
            await plugin.apply_auth(context, page, account.auth)

        return _apply_auth

    def _apply_closed_tabs_locked(self, closed_tabs: list[ClosedTabInfo]) -> None:
        for info in closed_tabs:
            self._session_cache.delete_many(info.session_ids)
            plugin = PluginRegistry.get(info.type_name)
            if plugin is not None:
                plugin.drop_sessions(info.session_ids)

    async def _clear_tab_domain_cookies_if_supported(
        self, proxy_key: ProxyKey, type_name: str
    ) -> None:
        """关 tab 前清该 type 对应域名的 cookie（仅支持带 site.cookie_domain 的插件）。"""
        entry = self._browser_manager.get_browser_entry(proxy_key)
        if entry is None:
            return
        plugin = PluginRegistry.get(type_name)
        if not isinstance(plugin, BaseSitePlugin) or not getattr(plugin, "site", None):
            return
        try:
            await clear_cookies_for_domain(entry.context, plugin.site.cookie_domain)
        except Exception as e:
            logger.debug("关 tab 前清 cookie 失败 type=%s: %s", type_name, e)

    async def _prune_invalid_resources_locked(self) -> None:
        """关闭配置中已不存在的浏览器/tab，避免热更新后继续使用失效资源。"""
        for proxy_key, entry in list(self._browser_manager.list_browser_entries()):
            group = self._pool.get_group_by_proxy_key(proxy_key)
            if group is None:
                self._apply_closed_tabs_locked(
                    await self._browser_manager.close_browser(proxy_key)
                )
                continue
            for type_name in list(entry.tabs.keys()):
                tab = entry.tabs[type_name]
                pair = self._pool.get_account_by_id(tab.account_id)
                if (
                    pair is None
                    or pair[0] is not group
                    or pair[1].type != type_name
                    or not pair[1].enabled
                ):
                    self._invalidate_tab_sessions_locked(proxy_key, type_name)
                    if tab.active_requests == 0:
                        # 与 reconcile 一致：优先同组同一页 re-auth，失败或无可用账号再关 tab
                        switched = False
                        group = self._pool.get_group_by_proxy_key(proxy_key)
                        if group is not None:
                            next_account = self._pool.next_available_account_in_group(
                                group,
                                type_name,
                                exclude_account_ids={tab.account_id},
                            )
                            if next_account is not None:
                                plugin = PluginRegistry.get(type_name)
                                if plugin is not None:
                                    switched = (
                                        await self._browser_manager.switch_tab_account(
                                            proxy_key,
                                            type_name,
                                            self._pool.account_id(group, next_account),
                                            self._make_apply_auth_fn(
                                                plugin,
                                                next_account,
                                            ),
                                        )
                                    )
                        if not switched:
                            await self._clear_tab_domain_cookies_if_supported(
                                proxy_key, type_name
                            )
                            closed = await self._browser_manager.close_tab(
                                proxy_key, type_name
                            )
                            if closed is not None:
                                self._apply_closed_tabs_locked([closed])
                    else:
                        self._browser_manager.mark_tab_draining(proxy_key, type_name)

    def _invalidate_session_locked(
        self,
        session_id: str,
        entry: SessionEntry | None = None,
    ) -> None:
        entry = entry or self._session_cache.get(session_id)
        if entry is None:
            return
        self._session_cache.delete(session_id)
        self._browser_manager.unregister_session(
            entry.proxy_key,
            entry.type_name,
            session_id,
        )
        plugin = PluginRegistry.get(entry.type_name)
        if plugin is not None:
            plugin.drop_session(session_id)

    def _invalidate_tab_sessions_locked(
        self,
        proxy_key: ProxyKey,
        type_name: str,
    ) -> None:
        tab = self._browser_manager.get_tab(proxy_key, type_name)
        if tab is None or not tab.sessions:
            return
        session_ids = list(tab.sessions)
        self._session_cache.delete_many(session_ids)
        plugin = PluginRegistry.get(type_name)
        if plugin is not None:
            plugin.drop_sessions(session_ids)
        tab.sessions.clear()

    def _revive_tab_if_possible_locked(
        self,
        proxy_key: ProxyKey,
        type_name: str,
    ) -> bool:
        tab = self._browser_manager.get_tab(proxy_key, type_name)
        if tab is None or tab.active_requests != 0:
            return False
        if tab.accepting_new:
            return True

        pair = self._pool.get_account_by_id(tab.account_id)
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

    async def _reconcile_tabs_locked(self) -> None:
        """
        收尾所有 non-ready tab：

        - 若原账号已恢复可用，则恢复 tab
        - 否则若同组有其他可用账号，则在 drained 后切号
        - 否则关闭 tab
        """
        for proxy_key, entry in list(self._browser_manager.list_browser_entries()):
            for type_name in list(entry.tabs.keys()):
                tab = entry.tabs[type_name]
                if tab.accepting_new:
                    continue
                if tab.active_requests != 0:
                    continue
                if self._revive_tab_if_possible_locked(proxy_key, type_name):
                    continue

                group = self._pool.get_group_by_proxy_key(proxy_key)
                if group is None:
                    await self._clear_tab_domain_cookies_if_supported(
                        proxy_key, type_name
                    )
                    closed = await self._browser_manager.close_tab(proxy_key, type_name)
                    if closed is not None:
                        self._apply_closed_tabs_locked([closed])
                    continue

                next_account = self._pool.next_available_account_in_group(
                    group,
                    type_name,
                    exclude_account_ids={tab.account_id},
                )
                if next_account is not None:
                    plugin = PluginRegistry.get(type_name)
                    if plugin is None:
                        continue
                    self._invalidate_tab_sessions_locked(proxy_key, type_name)
                    switched = await self._browser_manager.switch_tab_account(
                        proxy_key,
                        type_name,
                        self._pool.account_id(group, next_account),
                        self._make_apply_auth_fn(plugin, next_account),
                    )
                    if switched:
                        continue

                await self._clear_tab_domain_cookies_if_supported(proxy_key, type_name)
                closed = await self._browser_manager.close_tab(proxy_key, type_name)
                if closed is not None:
                    self._apply_closed_tabs_locked([closed])

    async def _reuse_session_target_locked(
        self,
        plugin: Any,
        type_name: str,
        session_id: str,
    ) -> _RequestTarget | None:
        entry = self._session_cache.get(session_id)
        if entry is None or entry.type_name != type_name:
            return None

        pair = self._pool.get_account_by_id(entry.account_id)
        if pair is None:
            self._invalidate_session_locked(session_id, entry)
            return None
        group, account = pair

        tab = self._browser_manager.get_tab(entry.proxy_key, type_name)
        if (
            tab is None
            or tab.account_id != entry.account_id
            or not plugin.has_session(session_id)
        ):
            self._invalidate_session_locked(session_id, entry)
            return None

        if not tab.accepting_new:
            self._invalidate_session_locked(session_id, entry)
            return None
        if session_id in self._busy_sessions:
            raise RuntimeError("当前会话正在处理中，请稍后再试")
        if tab.active_requests >= self._tab_max_concurrent:
            raise RuntimeError("当前会话所在 tab 繁忙，请稍后再试")

        page = self._browser_manager.acquire_tab(
            entry.proxy_key,
            type_name,
            self._tab_max_concurrent,
        )
        if page is None:
            raise RuntimeError("当前会话暂不可复用，请稍后再试")

        self._session_cache.touch(session_id)
        self._busy_sessions.add(session_id)
        context = await self._browser_manager.ensure_browser(
            entry.proxy_key,
            group.proxy_pass,
        )
        return _RequestTarget(
            proxy_key=entry.proxy_key,
            group=group,
            account=account,
            context=context,
            page=page,
            session_id=session_id,
            full_history=False,
        )

    async def _allocate_new_target_locked(
        self,
        type_name: str,
    ) -> _RequestTarget:
        # 先做一次轻量收尾，把已 drained 的 tab 尽快切号/关闭。
        await self._reconcile_tabs_locked()

        # 1. 已打开浏览器里已有该 type 的可服务 tab，直接复用。
        existing_tabs: list[tuple[int, float, ProxyKey, TabRuntime]] = []
        for proxy_key, entry in self._browser_manager.list_browser_entries():
            tab = entry.tabs.get(type_name)
            if (
                tab is not None
                and tab.accepting_new
                and tab.active_requests < self._tab_max_concurrent
            ):
                existing_tabs.append(
                    (tab.active_requests, tab.last_used_at, proxy_key, tab)
                )
        if existing_tabs:
            _, _, proxy_key, tab = min(existing_tabs, key=lambda item: item[:2])
            pair = self._pool.get_account_by_id(tab.account_id)
            if pair is None:
                self._invalidate_tab_sessions_locked(proxy_key, type_name)
                closed = await self._browser_manager.close_tab(proxy_key, type_name)
                if closed is not None:
                    self._apply_closed_tabs_locked([closed])
            else:
                group, account = pair
                page = self._browser_manager.acquire_tab(
                    proxy_key,
                    type_name,
                    self._tab_max_concurrent,
                )
                if page is not None:
                    context = await self._browser_manager.ensure_browser(
                        proxy_key,
                        group.proxy_pass,
                    )
                    return _RequestTarget(
                        proxy_key=proxy_key,
                        group=group,
                        account=account,
                        context=context,
                        page=page,
                        session_id=None,
                        full_history=True,
                    )

        # 2. 已打开浏览器里还没有该 type tab，但该组有可用账号，直接建新 tab。
        open_browser_candidates: list[
            tuple[int, float, ProxyKey, ProxyGroupConfig]
        ] = []
        for proxy_key, entry in self._browser_manager.list_browser_entries():
            if type_name in entry.tabs:
                continue
            group = self._pool.get_group_by_proxy_key(proxy_key)
            if group is None:
                continue
            if not self._pool.has_available_account_in_group(group, type_name):
                continue
            open_browser_candidates.append(
                (
                    self._browser_manager.browser_load(proxy_key),
                    entry.last_used_at,
                    proxy_key,
                    group,
                )
            )
        if open_browser_candidates:
            _, _, proxy_key, group = min(
                open_browser_candidates, key=lambda item: item[:2]
            )
            account = self._pool.next_available_account_in_group(group, type_name)
            if account is not None:
                plugin = PluginRegistry.get(type_name)
                if plugin is None:
                    raise ValueError(f"未注册的 type: {type_name}")
                await self._browser_manager.open_tab(
                    proxy_key,
                    group.proxy_pass,
                    type_name,
                    self._pool.account_id(group, account),
                    plugin.create_page,
                    self._make_apply_auth_fn(plugin, account),
                )
                page = self._browser_manager.acquire_tab(
                    proxy_key,
                    type_name,
                    self._tab_max_concurrent,
                )
                if page is None:
                    raise RuntimeError("新建 tab 后仍无法占用请求槽位")
                context = await self._browser_manager.ensure_browser(
                    proxy_key,
                    group.proxy_pass,
                )
                return _RequestTarget(
                    proxy_key=proxy_key,
                    group=group,
                    account=account,
                    context=context,
                    page=page,
                    session_id=None,
                    full_history=True,
                )

        # 3. 已打开浏览器里该 type tab 已 drained，且同组有备用账号，可在当前 tab 切号。
        switch_candidates: list[tuple[float, ProxyKey, ProxyGroupConfig]] = []
        for proxy_key, entry in self._browser_manager.list_browser_entries():
            tab = entry.tabs.get(type_name)
            if tab is None or tab.active_requests != 0:
                continue
            group = self._pool.get_group_by_proxy_key(proxy_key)
            if group is None:
                continue
            if not self._pool.has_available_account_in_group(
                group,
                type_name,
                exclude_account_ids={tab.account_id},
            ):
                continue
            switch_candidates.append((tab.last_used_at, proxy_key, group))
        if switch_candidates:
            _, proxy_key, group = min(switch_candidates, key=lambda item: item[0])
            tab = self._browser_manager.get_tab(proxy_key, type_name)
            plugin = PluginRegistry.get(type_name)
            if tab is not None and plugin is not None:
                next_account = self._pool.next_available_account_in_group(
                    group,
                    type_name,
                    exclude_account_ids={tab.account_id},
                )
                if next_account is not None:
                    self._invalidate_tab_sessions_locked(proxy_key, type_name)
                    switched = await self._browser_manager.switch_tab_account(
                        proxy_key,
                        type_name,
                        self._pool.account_id(group, next_account),
                        self._make_apply_auth_fn(plugin, next_account),
                    )
                    if switched:
                        page = self._browser_manager.acquire_tab(
                            proxy_key,
                            type_name,
                            self._tab_max_concurrent,
                        )
                        if page is None:
                            raise RuntimeError("切号后仍无法占用请求槽位")
                        context = await self._browser_manager.ensure_browser(
                            proxy_key,
                            group.proxy_pass,
                        )
                        return _RequestTarget(
                            proxy_key=proxy_key,
                            group=group,
                            account=next_account,
                            context=context,
                            page=page,
                            session_id=None,
                            full_history=True,
                        )

        # 4. 开新浏览器。
        open_groups = {
            proxy_key.fingerprint_id
            for proxy_key in self._browser_manager.current_proxy_keys()
        }
        pair = self._pool.next_available_pair(
            type_name,
            exclude_fingerprint_ids=open_groups,
        )
        if pair is None:
            raise ValueError(f"没有类别为 {type_name!r} 的可用账号，请稍后再试")
        group, account = pair
        proxy_key = _proxy_key_for_group(group)
        plugin = PluginRegistry.get(type_name)
        if plugin is None:
            raise ValueError(f"未注册的 type: {type_name}")
        await self._browser_manager.open_tab(
            proxy_key,
            group.proxy_pass,
            type_name,
            self._pool.account_id(group, account),
            plugin.create_page,
            self._make_apply_auth_fn(plugin, account),
        )
        page = self._browser_manager.acquire_tab(
            proxy_key,
            type_name,
            self._tab_max_concurrent,
        )
        if page is None:
            raise RuntimeError("新浏览器建 tab 后仍无法占用请求槽位")
        context = await self._browser_manager.ensure_browser(
            proxy_key, group.proxy_pass
        )
        return _RequestTarget(
            proxy_key=proxy_key,
            group=group,
            account=account,
            context=context,
            page=page,
            session_id=None,
            full_history=True,
        )

    async def _stream_completion(
        self,
        type_name: str,
        req: OpenAIChatRequest,
    ) -> AsyncIterator[str]:
        """
        内部实现：调度 + 插件 stream_completion 字符串流，末尾附加 session_id 零宽编码。
        对外仅通过 stream_openai_events() 暴露事件流。
        """
        plugin = PluginRegistry.get(type_name)
        if plugin is None:
            raise ValueError(f"未注册的 type: {type_name}")

        raw_messages = _request_messages_as_dicts(req)
        conv_uuid = req.resume_session_id or parse_conv_uuid_from_messages(raw_messages)
        logger.info("[chat] type=%s parsed conv_uuid=%s", type_name, conv_uuid)

        has_tools = bool(req.tools)
        tagged_prompt_prefix = (
            format_tagged_prompt(
                req.tools or [],
                allow_parallel_tool_calls=req.parallel_tool_calls is not False,
            )
            if has_tools
            else ""
        )

        debug_path = (
            Path(__file__).resolve().parent.parent.parent
            / "debug"
            / "chat_prompt_debug.json"
        )

        max_retries = 3
        for attempt in range(max_retries):
            target: _RequestTarget | None = None
            active_session_id: str | None = None
            request_id = uuid.uuid4().hex
            try:
                async with self._schedule_lock:
                    if conv_uuid:
                        target = await self._reuse_session_target_locked(
                            plugin,
                            type_name,
                            conv_uuid,
                        )
                    if target is None:
                        target = await self._allocate_new_target_locked(type_name)
                    if target.session_id is not None:
                        active_session_id = target.session_id

                content = extract_user_content(
                    req.messages,
                    has_tools=has_tools,
                    tagged_prompt_prefix=tagged_prompt_prefix,
                    allow_parallel_tool_calls=req.parallel_tool_calls is not False,
                    full_history=target.full_history,
                )
                if not content.strip() and req.attachment_files:
                    content = "Please analyze the attached image."
                elif not content.strip():
                    raise ValueError("messages 中需至少有一条带 content 的 user 消息")

                debug_path.parent.mkdir(parents=True, exist_ok=True)
                debug_path.write_text(
                    json.dumps(
                        {
                            "prompt": content,
                            "full_history": target.full_history,
                            "type": type_name,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                session_id = target.session_id
                if session_id is None:
                    logger.info(
                        "[chat] create_conversation type=%s proxy=%s account=%s",
                        type_name,
                        target.proxy_key.fingerprint_id,
                        self._pool.account_id(target.group, target.account),
                    )
                    session_id = await plugin.create_conversation(
                        target.context,
                        target.page,
                        proxy_url=target.proxy_key.proxy_host
                        if target.proxy_key.use_proxy
                        else "",
                        timezone=target.group.timezone
                        or getattr(target.proxy_key, "timezone", None)
                        or TIMEZONE,
                    )
                    if not session_id:
                        raise RuntimeError("插件创建会话失败")
                    async with self._schedule_lock:
                        account_id = self._pool.account_id(target.group, target.account)
                        self._session_cache.put(
                            session_id,
                            target.proxy_key,
                            type_name,
                            account_id,
                        )
                        self._browser_manager.register_session(
                            target.proxy_key,
                            type_name,
                            session_id,
                        )
                        self._busy_sessions.add(session_id)
                active_session_id = session_id

                logger.info(
                    "[chat] stream_completion type=%s session_id=%s proxy=%s account=%s full_history=%s",
                    type_name,
                    session_id,
                    target.proxy_key.fingerprint_id,
                    self._pool.account_id(target.group, target.account),
                    target.full_history,
                )
                # 根据是否 full_history 选择附件来源：
                # - 复用会话（full_history=False）：仅最后一条 user 的图片（可能为空，则本轮不带图）
                # - 新建/重建会话（full_history=True）：所有历史 user 的图片
                attachments = (
                    req.attachment_files_all_users
                    if target.full_history
                    else req.attachment_files_last_user
                )

                stream = cast(
                    AsyncIterator[str],
                    plugin.stream_completion(
                        target.context,
                        target.page,
                        session_id,
                        content,
                        request_id=request_id,
                        attachments=attachments,
                    ),
                )
                async for chunk in stream:
                    yield chunk
                yield session_id_suffix(session_id)
                return
            except AccountFrozenError as e:
                logger.warning(
                    "账号限流/额度用尽（插件上报），切换资源重试: type=%s proxy=%s err=%s",
                    type_name,
                    target.proxy_key.fingerprint_id if target else None,
                    e,
                )
                async with self._schedule_lock:
                    if target is not None:
                        self.report_account_unfreeze(
                            target.group.fingerprint_id,
                            target.account.name,
                            e.unfreeze_at,
                        )
                        self._browser_manager.mark_tab_draining(
                            target.proxy_key,
                            type_name,
                            frozen_until=e.unfreeze_at,
                        )
                        self._invalidate_tab_sessions_locked(
                            target.proxy_key, type_name
                        )
                if attempt == max_retries - 1:
                    raise RuntimeError(
                        f"已重试 {max_retries} 次仍限流/过载，请稍后再试: {e}"
                    ) from e
                continue
            finally:
                if target is not None:
                    async with self._schedule_lock:
                        if active_session_id is not None:
                            self._busy_sessions.discard(active_session_id)
                        self._browser_manager.release_tab(target.proxy_key, type_name)
                        await self._reconcile_tabs_locked()

    async def stream_openai_events(
        self,
        type_name: str,
        req: OpenAIChatRequest,
    ) -> AsyncIterator[OpenAIStreamEvent]:
        """
        唯一流式出口：以 OpenAIStreamEvent 为中间态。插件产出字符串流，
        在此包装为 content_delta + finish，供协议适配层编码为各协议 SSE。
        """
        async for chunk in self._stream_completion(type_name, req):
            # session marker 也作为 content_delta 透传（对事件消费者而言是普通文本片段）
            yield OpenAIStreamEvent(type="content_delta", content=chunk)
        yield OpenAIStreamEvent(type="finish", finish_reason="stop")
