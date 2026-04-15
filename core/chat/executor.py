"""单次 chat 请求执行：调度目标 tab → 拼 prompt → 调插件流式接口 → 包装为 OpenAI 事件。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import cast

from core.chat.runtime import ChatRuntimeCoordinator
from core.chat.scheduler import ChatRequestScheduler
from core.chat.state import (
    ChatHandlerState,
    RequestTarget,
    request_messages_as_dicts,
)
from core.chat.prompt_builder import extract_user_content
from core.shared.session_markers import (
    parse_conv_uuid_from_messages,
    session_id_suffix,
)
from core.shared.models import OpenAIChatRequest
from core.shared.tagged_output import format_tagged_prompt
from core.constants import TIMEZONE
from core.plugin.base import AccountFrozenError, PluginRegistry
from core.stream.events import OpenAIStreamEvent

logger = logging.getLogger(__name__)


class ChatRequestExecutor:
    """执行单次 chat 请求，负责 prompt 组装与插件流式调用。"""

    def __init__(
        self,
        state: ChatHandlerState,
        runtime: ChatRuntimeCoordinator,
        scheduler: ChatRequestScheduler,
    ) -> None:
        self._state = state  # 共享锁、会话缓存、浏览器管理器
        self._runtime = runtime  # 会话失效、tab 协调、维护钩子
        self._scheduler = scheduler  # 复用 session 或分配新 tab/账号

    async def stream_completion(
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

        raw_messages = request_messages_as_dicts(req)
        # 对话续接：显式 resume 优先，否则从消息里解析站点会话 id
        conv_uuid = req.resume_session_id or parse_conv_uuid_from_messages(raw_messages)
        logger.info("[chat] type=%s parsed conv_uuid=%s", type_name, conv_uuid)

        has_tools = bool(req.tools)
        # 工具模式首段协议说明（仅首回合或 full_history 时拼进 prompt）
        tagged_prompt_prefix = (
            format_tagged_prompt(
                req.tools or [],
                allow_parallel_tool_calls=req.parallel_tool_calls is not False,
                tool_choice=req.tool_choice,
            )
            if has_tools
            else ""
        )

        max_retries = 3  # 遇账号冻结（额度/限流）时换资源重试次数
        for attempt in range(max_retries):
            target: RequestTarget | None = None  # 当前轮次选定的 tab/账号/session
            active_session_id: str | None = None  # finally 里释放 busy 与 tab 槽位用
            request_id = uuid.uuid4().hex  # 透传插件，用于日志/关联上游请求
            try:
                # 在锁内完成「占位」：复用已有站点会话或抢新槽位，避免并发撞同一 session
                async with self._state.schedule_lock:
                    if conv_uuid:
                        target = await self._scheduler.reuse_session_target_locked(
                            plugin,
                            type_name,
                            conv_uuid,
                        )
                    if target is None:
                        target = await self._scheduler.allocate_new_target_locked(
                            type_name
                        )
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

                logger.debug(
                    "[chat] prompt debug: type=%s full_history=%s prompt=%s",
                    type_name,
                    target.full_history,
                    content[:500],
                )

                session_id = target.session_id  # None 表示需在插件侧新建站点会话
                if session_id is None:
                    logger.info(
                        "[chat] create_conversation type=%s proxy=%s account=%s",
                        type_name,
                        target.proxy_key.fingerprint_id,
                        self._state.pool.account_id(target.group, target.account),
                    )
                    session_id = await plugin.create_conversation(
                        target.context,
                        target.page,
                        timezone=target.group.timezone
                        or getattr(target.proxy_key, "timezone", None)
                        or TIMEZONE,
                    )
                    if not session_id:
                        raise RuntimeError("插件创建会话失败")
                    async with self._state.schedule_lock:
                        account_id = self._state.pool.account_id(
                            target.group,
                            target.account,
                        )
                        self._state.session_cache.put(
                            session_id,
                            target.proxy_key,
                            type_name,
                            account_id,
                        )
                        self._state.browser_manager.register_session(
                            target.proxy_key,
                            type_name,
                            session_id,
                        )
                        self._state.busy_sessions.add(session_id)
                active_session_id = session_id

                logger.info(
                    "[chat] stream_completion type=%s session_id=%s proxy=%s account=%s full_history=%s",
                    type_name,
                    session_id,
                    target.proxy_key.fingerprint_id,
                    self._state.pool.account_id(target.group, target.account),
                    target.full_history,
                )
                # 新开会话带完整上传历史；复用会话只传本轮 user 附件，避免重复传图
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
                yield session_id_suffix(
                    session_id
                )  # 末尾零宽编码，供客户端解析 session
                return
            except AccountFrozenError as exc:
                logger.warning(
                    "账号限流/额度用尽（插件上报），切换资源重试: type=%s proxy=%s err=%s",
                    type_name,
                    target.proxy_key.fingerprint_id if target else None,
                    exc,
                )
                async with self._state.schedule_lock:
                    if target is not None:
                        self._runtime.report_account_unfreeze(
                            target.group.fingerprint_id,
                            type_name,
                            exc.unfreeze_at,
                        )
                        self._state.browser_manager.mark_tab_draining(
                            target.proxy_key,
                            type_name,
                            frozen_until=exc.unfreeze_at,
                        )
                        self._runtime.invalidate_tab_sessions_locked(
                            target.proxy_key,
                            type_name,
                        )
                if attempt == max_retries - 1:
                    raise RuntimeError(
                        f"已重试 {max_retries} 次仍限流/过载，请稍后再试: {exc}"
                    ) from exc
                continue
            finally:
                if target is not None:
                    async with self._state.schedule_lock:
                        if active_session_id is not None:
                            self._state.busy_sessions.discard(active_session_id)
                        self._state.browser_manager.release_tab(
                            target.proxy_key,
                            type_name,
                        )
                        await self._runtime.reconcile_tabs_locked()

    async def stream_openai_events(
        self,
        type_name: str,
        req: OpenAIChatRequest,
    ) -> AsyncIterator[OpenAIStreamEvent]:
        """
        唯一流式出口：以 OpenAIStreamEvent 为中间态。插件产出字符串流，
        在此包装为 content_delta + finish，供协议适配层编码为各协议 SSE。
        """
        async for chunk in self.stream_completion(type_name, req):
            yield OpenAIStreamEvent(type="content_delta", content=chunk)
        yield OpenAIStreamEvent(type="finish", finish_reason="stop")
