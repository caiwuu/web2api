"""
插件抽象与注册表：type_name -> 插件实现。

三层设计：
  AbstractPlugin   — 最底层接口，理论上支持任意协议（非 Cookie、非 SSE 的站点也能接）。
  BaseSitePlugin   — Cookie 认证 + SSE 流式站点的通用编排，插件开发者继承它只需实现 5 个 hook。
  PluginRegistry   — 全局注册表。
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator

from playwright.async_api import BrowserContext, Page

from core.shared.models import InputAttachment
from core.config.settings import get
from core.plugin.errors import AccountFrozenError  # noqa: F401  — re-export for backward compat
from core.plugin.helpers import (
    create_page_for_site,
    stream_completion_via_sse,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SiteConfig：纯声明式站点配置
# ---------------------------------------------------------------------------


@dataclass
class SiteConfig:
    """Cookie 认证站点的声明式配置，插件开发者只需填字段，无需写任何方法。"""

    start_url: str
    api_base: str
    cookie_name: str
    cookie_domain: str
    auth_keys: list[str]
    config_section: str = (
        ""  # config.yaml 中的 section，如 "claude"，用于覆盖 start_url/api_base
    )


# ---------------------------------------------------------------------------
# AbstractPlugin — 最底层抽象接口
# ---------------------------------------------------------------------------


class AbstractPlugin(ABC):
    """
    各 type（如 claude、kimi）需实现此接口并注册。
    若站点基于 Cookie + SSE，推荐直接继承 BaseSitePlugin 而非此类。
    """

    def __init__(self) -> None:
        self._session_state: dict[str, dict[str, Any]] = {}

    type_name: str

    async def create_page(
        self, context: BrowserContext, reuse_page: Page | None = None
    ) -> Page:
        raise NotImplementedError

    async def create_conversation(
        self,
        context: BrowserContext,
        page: Page,
        **kwargs: Any,
    ) -> str | None:
        raise NotImplementedError

    async def stream_completion(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        message: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        if False:
            yield  # 使抽象方法为 async generator，与子类一致，便于 async for 迭代
        raise NotImplementedError

    def parse_session_id(self, messages: list[dict[str, Any]]) -> str | None:
        return None

    def is_stream_end_event(self, payload: str) -> bool:
        """判断某条流式 payload 是否表示本轮响应已正常结束。默认不识别。"""
        return False

    def has_session(self, session_id: str) -> bool:
        return session_id in self._session_state

    def drop_session(self, session_id: str) -> None:
        self._session_state.pop(session_id, None)

    def drop_sessions(self, session_ids: list[str] | set[str]) -> None:
        for session_id in session_ids:
            self._session_state.pop(session_id, None)

    def model_mapping(self) -> dict[str, str] | None:
        """子类可覆盖；BaseSitePlugin 会从 config_section 的 model_mapping 读取。"""
        return None

    def on_http_error(self, message: str, headers: dict[str, str] | None) -> int | None:
        return None


# ---------------------------------------------------------------------------
# BaseSitePlugin — Cookie + SSE 站点的通用编排
# ---------------------------------------------------------------------------


class BaseSitePlugin(AbstractPlugin):
    """
    Cookie 认证 + SSE 流式站点的公共基类。

    插件开发者继承此类后，只需：
      1. 声明 site = SiteConfig(...)        — 站点配置
      2. 实现 fetch_site_context()           — 获取站点上下文（如 org/user 信息）
      3. 实现 create_session()              — 调用站点 API 创建会话
      4. 实现 build_completion_url/body()    — 拼补全请求的 URL 与 body
      5. 实现 parse_stream_event()          — 解析单条流式事件（如 SSE data）

    create_page / create_conversation / stream_completion
    均由基类自动编排，无需重写。
    """

    site: SiteConfig  # 子类必须赋值

    # ---- 从 config.yaml 读取的 URL 属性（config_section 有值时覆盖默认） ----

    @property
    def start_url(self) -> str:
        if self.site.config_section:
            url = get(self.site.config_section, "start_url")
            if url:
                return str(url).strip()
        return self.site.start_url

    @property
    def api_base(self) -> str:
        if self.site.config_section:
            base = get(self.site.config_section, "api_base")
            if base:
                return str(base).strip()
        return self.site.api_base

    def model_mapping(self) -> dict[str, str] | None:
        """从 config 的 config_section.model_mapping 读取；未配置时返回 None。"""
        if self.site.config_section:
            m = get(self.site.config_section, "model_mapping")
            if isinstance(m, dict) and m:
                return {str(k): str(v) for k, v in m.items()}
        return None

    # ---- 基类全自动实现，子类无需碰 ----

    async def create_page(
        self,
        context: BrowserContext,
        reuse_page: Page | None = None,
    ) -> Page:
        return await create_page_for_site(
            context, self.start_url, reuse_page=reuse_page
        )

    async def create_conversation(
        self,
        context: BrowserContext,
        page: Page,
        **kwargs: Any,
    ) -> str | None:
        # 调用子类获取站点上下文
        site_context = await self.fetch_site_context(context, page)
        if site_context is None:
            logger.warning(
                "[%s] fetch_site_context 返回 None，请确认已登录", self.type_name
            )
            return None
        # 通过站点上下文创建会话
        conv_id = await self.create_session(context, page, site_context)
        if conv_id is None:
            return None
        state: dict[str, Any] = {"site_context": site_context}
        if kwargs.get("timezone") is not None:
            state["timezone"] = kwargs["timezone"]
        self._session_state[conv_id] = state
        logger.info(
            "[%s] create_conversation done conv_id=%s sessions=%s",
            self.type_name,
            conv_id,
            list(self._session_state.keys()),
        )
        return conv_id

    async def stream_completion(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        message: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        state = self._session_state.get(session_id)
        if not state:
            raise RuntimeError(f"未知会话 ID: {session_id}")

        url = self.build_completion_url(session_id, state)
        attachments = list(kwargs.get("attachments") or [])
        prepared_attachments = await self.prepare_attachments(
            context,
            page,
            session_id,
            state,
            attachments,
        )
        body = self.build_completion_body(
            message,
            session_id,
            state,
            prepared_attachments,
        )
        body_json = json.dumps(body)
        request_id: str = kwargs.get("request_id", "")

        logger.info(
            "[%s] stream_completion session_id=%s url=%s",
            self.type_name,
            session_id,
            url,
        )

        out_message_ids: list[str] = []
        async for text in stream_completion_via_sse(
            context,
            page,
            url,
            body_json,
            self.parse_stream_event,
            request_id,
            on_http_error=self.on_http_error,
            is_terminal_event=self.is_stream_end_event,
            collect_message_id=out_message_ids,
        ):
            yield text

        if out_message_ids and session_id in self._session_state:
            self.on_stream_completion_finished(session_id, out_message_ids)

    # ---- 子类必须实现的 hook ----

    @abstractmethod
    async def fetch_site_context(
        self, context: BrowserContext, page: Page
    ) -> dict[str, Any] | None:
        """获取站点上下文信息（如 org_uuid、user_id 等），失败返回 None。"""
        ...

    @abstractmethod
    async def create_session(
        self,
        context: BrowserContext,
        page: Page,
        site_context: dict[str, Any],
    ) -> str | None:
        """调用站点 API 创建会话，返回会话 ID，失败返回 None。"""
        ...

    @abstractmethod
    def build_completion_url(self, session_id: str, state: dict[str, Any]) -> str:
        """根据会话状态拼出补全请求的完整 URL。"""
        ...

    @abstractmethod
    def build_completion_body(
        self,
        message: str,
        session_id: str,
        state: dict[str, Any],
        prepared_attachments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构建补全请求体，返回 dict（基类负责 json.dumps）。"""
        ...

    @abstractmethod
    def parse_stream_event(
        self,
        payload: str,
    ) -> tuple[list[str], str | None, str | None]:
        """
        解析单条流式事件 payload（如 SSE data 行）。
        返回 (texts, message_id, error_message)。
        """
        ...

    # ---- 子类可选覆盖的 hook（有合理默认值） ----

    def on_stream_completion_finished(
        self,
        session_id: str,
        message_ids: list[str],
    ) -> None:
        """Hook：流式补全结束后调用，子类可按需用 message_ids 更新会话 state（如记续写用的父消息 id）。"""

    async def prepare_attachments(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        state: dict[str, Any],
        attachments: list[InputAttachment],
    ) -> dict[str, Any]:
        del context, page, session_id, state, attachments
        return {}


# ---------------------------------------------------------------------------
# PluginRegistry — 全局注册表
# ---------------------------------------------------------------------------


class PluginRegistry:
    """全局插件注册表：type_name -> AbstractPlugin。"""

    _plugins: dict[str, AbstractPlugin] = {}

    @classmethod
    def register(cls, plugin: AbstractPlugin) -> None:
        cls._plugins[plugin.type_name] = plugin

    @classmethod
    def get(cls, type_name: str) -> AbstractPlugin | None:
        return cls._plugins.get(type_name)

    @classmethod
    def all_types(cls) -> list[str]:
        return list(cls._plugins.keys())
