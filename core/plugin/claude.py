"""
Claude 插件：仅实现站点特有的上下文获取、会话创建、请求体构建、SSE 解析和限流处理。
其余编排逻辑（create_page / apply_auth / stream_completion 流程）全部由 BaseSitePlugin 完成。
调试时可在 config.yaml 的 claude.start_url、claude.api_base 指向 mock。
"""

import datetime
import json
import logging
import re
import time
from typing import Any

from playwright.async_api import BrowserContext, Page

from core.shared.models import InputAttachment
from core.constants import TIMEZONE
from core.plugin.base import BaseSitePlugin, PluginRegistry, SiteConfig
from core.plugin.helpers import (
    clear_cookies_for_domain,
    clear_page_storage_for_switch,
    request_json_via_page_fetch,
    safe_page_reload,
    upload_file_via_page_fetch,
)

logger = logging.getLogger(__name__)

_HTTP_ERR_BODY_RE = re.compile(r"^HTTP\s+429\s+(.*)$", re.DOTALL)
# 内嵌 JSON 字符串里常见 \"resetsAt\":epoch
_RESETS_AT_EMBEDDED_RE = re.compile(r'\\"resetsAt\\"\s*:\s*(\d+)')
_RESETS_AT_PLAIN_RE = re.compile(r'"resetsAt"\s*:\s*(\d+)')


def _claude_rate_limit_reset_unix_from_error_payload(message: str) -> int | None:
    """
    从页面 fetch 失败信息中解析限流解冻时间（Unix 秒）。
    形如：HTTP 429 { "type":"error", "error":{ "type":"rate_limit_error",
    "message": "{\\\"resetsAt\\\": ...}" } } —— 以响应体为准，而非通用 Anthropic 头。
    """
    m = _HTTP_ERR_BODY_RE.match(message.strip())
    if not m:
        return None
    body = m.group(1).strip()
    if body.endswith("...") and len(body) > 3:
        body_try = body[:-3].rstrip()
    else:
        body_try = body

    inner: dict[str, Any] | None = None
    try:
        data = json.loads(body_try)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            raw = err.get("message")
            if isinstance(raw, str):
                try:
                    inner = json.loads(raw)
                except json.JSONDecodeError:
                    inner = None
            elif isinstance(raw, dict):
                inner = raw

    if isinstance(inner, dict):
        ra = inner.get("resetsAt")
        if isinstance(ra, int) and ra > 0:
            return ra
        windows = inner.get("windows")
        if isinstance(windows, dict):
            exceeded: list[int] = []
            for w in windows.values():
                if not isinstance(w, dict):
                    continue
                if w.get("status") != "exceeded_limit":
                    continue
                rw = w.get("resets_at")
                if isinstance(rw, int) and rw > 0:
                    exceeded.append(rw)
            if exceeded:
                return min(exceeded)

    for rx in (_RESETS_AT_EMBEDDED_RE, _RESETS_AT_PLAIN_RE):
        mm = rx.search(message)
        if mm:
            try:
                v = int(mm.group(1))
                if v > 0:
                    return v
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------------------
# 站点特有：请求体 & SSE 解析
# ---------------------------------------------------------------------------


def _default_completion_body(
    message: str, *, is_follow_up: bool = False, timezone: str = TIMEZONE
) -> dict[str, Any]:
    """构建 Claude completion 请求体。续写时不带 create_conversation_params，否则 API 返回 400。"""
    body: dict[str, Any] = {
        "prompt": message,
        "timezone": timezone,
        "personalized_styles": [
            {
                "type": "default",
                "key": "Default",
                "name": "Normal",
                "nameKey": "normal_style_name",
                "prompt": "Normal\n",
                "summary": "Default responses from Claude",
                "summaryKey": "normal_style_summary",
                "isDefault": True,
            }
        ],
        "locale": "en-US",
        "tools": [
            {"type": "web_search_v0", "name": "web_search"},
            {"type": "artifacts_v0", "name": "artifacts"},
            {"type": "repl_v0", "name": "repl"},
            {"type": "widget", "name": "weather_fetch"},
            {"type": "widget", "name": "recipe_display_v0"},
            {"type": "widget", "name": "places_map_display_v0"},
            {"type": "widget", "name": "message_compose_v1"},
            {"type": "widget", "name": "ask_user_input_v0"},
            {"type": "widget", "name": "places_search"},
            {"type": "widget", "name": "fetch_sports_data"},
        ],
        "attachments": [],
        "files": [],
        "sync_sources": [],
        "rendering_mode": "messages",
    }
    if not is_follow_up:
        body["create_conversation_params"] = {
            "name": "",
            "include_conversation_preferences": True,
            "is_temporary": False,
        }
    return body


def _parse_one_sse_event(payload: str) -> tuple[list[str], str | None, str | None]:
    """解析单条 Claude SSE data 行，返回 (texts, message_id, error)。"""
    result: list[str] = []
    message_id: str | None = None
    error_message: str | None = None
    try:
        obj = json.loads(payload)
        if not isinstance(obj, dict):
            return (result, message_id, error_message)
        kind = obj.get("type")
        if kind == "error":
            err = obj.get("error") or {}
            error_message = err.get("message") or err.get("type") or "Unknown error"
            return (result, message_id, error_message)
        if "text" in obj and obj.get("text"):
            result.append(str(obj["text"]))
        elif kind == "content_block_delta":
            delta = obj.get("delta")
            if isinstance(delta, dict) and "text" in delta:
                result.append(str(delta["text"]))
            elif isinstance(delta, str) and delta:
                result.append(delta)
        elif kind == "message_start":
            msg = obj.get("message")
            if isinstance(msg, dict):
                for key in ("uuid", "id"):
                    if msg.get(key):
                        message_id = str(msg[key])
                        break
            if not message_id:
                mid = (
                    obj.get("message_uuid") or obj.get("uuid") or obj.get("message_id")
                )
                if mid:
                    message_id = str(mid)
        elif (
            kind
            and kind
            not in (
                "ping",
                "content_block_start",
                "content_block_stop",
                "message_stop",
                "message_delta",
                "message_limit",
            )
            and not result
        ):
            logger.debug(
                "SSE 未解析出正文 type=%s payload=%s",
                kind,
                payload[:200] if len(payload) > 200 else payload,
            )
    except json.JSONDecodeError:
        pass
    return (result, message_id, error_message)


def _is_terminal_sse_event(payload: str) -> bool:
    """Claude 正常流结束时会发送 message_stop。"""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("type") == "message_stop"


# ---------------------------------------------------------------------------
# ClaudePlugin — 只需声明配置 + 实现 5 个 hook
# ---------------------------------------------------------------------------


class ClaudePlugin(BaseSitePlugin):
    """Claude Web2API 插件。auth 需含 sessionKey。"""

    type_name = "claude"

    site = SiteConfig(
        start_url="https://claude.ai",
        api_base="https://claude.ai/api",
        cookie_name="sessionKey",
        cookie_domain=".claude.ai",
        auth_keys=["sessionKey", "session_key"],
        config_section="claude",
    )

    async def apply_auth(
        self,
        context: BrowserContext,
        page: Page,
        auth: dict[str, Any],
        *,
        reload: bool = True,
    ) -> None:
        await clear_cookies_for_domain(context, self.site.cookie_domain)
        await clear_page_storage_for_switch(page)
        await safe_page_reload(page, url=self.start_url)

        await super().apply_auth(context, page, auth, reload=reload)

    # ---- 5 个必须实现的 hook ----

    async def fetch_site_context(
        self, context: BrowserContext, page: Page
    ) -> dict[str, Any] | None:
        del context
        resp = await request_json_via_page_fetch(
            page,
            f"{self.api_base}/account",
            timeout_ms=15000,
        )
        if int(resp.get("status") or 0) != 200:
            text = str(resp.get("text") or "")[:500]
            logger.warning(
                "[%s] fetch_site_context 失败 status=%s url=%s body=%s",
                self.type_name,
                resp.get("status"),
                resp.get("url"),
                text,
            )
            return None
        data = resp.get("json")
        if not isinstance(data, dict):
            logger.warning("[%s] fetch_site_context 返回非 JSON", self.type_name)
            return None
        memberships = data.get("memberships") or []
        if not memberships:
            return None
        org = memberships[0].get("organization") or {}
        org_uuid = org.get("uuid")
        return {"org_uuid": org_uuid} if org_uuid else None

    async def create_session(
        self,
        context: BrowserContext,
        page: Page,
        site_context: dict[str, Any],
    ) -> str | None:
        del context
        org_uuid = site_context["org_uuid"]
        url = f"{self.api_base}/organizations/{org_uuid}/chat_conversations"
        resp = await request_json_via_page_fetch(
            page,
            url,
            method="POST",
            body=json.dumps({"name": "", "model": "claude-sonnet-4-5-20250929"}),
            headers={"Content-Type": "application/json"},
            timeout_ms=15000,
        )
        status = int(resp.get("status") or 0)
        if status not in (200, 201):
            text = str(resp.get("text") or "")[:500]
            logger.warning("创建会话失败 %s: %s", status, text)
            return None
        data = resp.get("json")
        if not isinstance(data, dict):
            logger.warning("创建会话返回非 JSON")
            return None
        return data.get("uuid")

    def build_completion_url(self, session_id: str, state: dict[str, Any]) -> str:
        org_uuid = state["site_context"]["org_uuid"]
        return f"{self.api_base}/organizations/{org_uuid}/chat_conversations/{session_id}/completion"

    # 构建请求体
    def build_completion_body(
        self,
        message: str,
        session_id: str,
        state: dict[str, Any],
        prepared_attachments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parent = state.get("parent_message_uuid")
        tz = state.get("timezone") or TIMEZONE
        body = _default_completion_body(
            message, is_follow_up=parent is not None, timezone=tz
        )
        if parent:
            body["parent_message_uuid"] = parent
        if prepared_attachments:
            body.update(prepared_attachments)
        return body

    def parse_stream_event(
        self,
        payload: str,
    ) -> tuple[list[str], str | None, str | None]:
        return _parse_one_sse_event(payload)

    def is_stream_end_event(self, payload: str) -> bool:
        return _is_terminal_sse_event(payload)

    # 处理错误
    def on_http_error(
        self,
        message: str,
        headers: dict[str, str] | None,
    ) -> int | None:
        if "429" not in message:
            return None
        from_body = _claude_rate_limit_reset_unix_from_error_payload(message)
        if from_body is not None:
            return from_body
        if headers:
            reset = headers.get("anthropic-ratelimit-requests-reset") or headers.get(
                "Anthropic-Ratelimit-Requests-Reset"
            )
            if reset:
                try:
                    s = str(reset).strip()
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    dt = datetime.datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    return int(dt.timestamp())
                except Exception:
                    pass
        return int(time.time()) + 5 * 3600

    _UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    def on_stream_completion_finished(
        self,
        session_id: str,
        message_ids: list[str],
    ) -> None:
        """Claude 多轮续写需要 parent_message_uuid，取本轮最后一条消息 UUID 写入 state。"""
        last_uuid = next(
            (m for m in reversed(message_ids) if self._UUID_RE.match(m)), None
        )
        if last_uuid and session_id in self._session_state:
            self._session_state[session_id]["parent_message_uuid"] = last_uuid
            logger.info(
                "[%s] updated parent_message_uuid=%s", self.type_name, last_uuid
            )

    async def prepare_attachments(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        state: dict[str, Any],
        attachments: list[InputAttachment],
    ) -> dict[str, Any]:
        del context
        if not attachments:
            return {}
        if len(attachments) > 5:
            raise RuntimeError("Claude 单次最多上传 5 张图片")

        org_uuid = state["site_context"]["org_uuid"]
        url = (
            f"{self.api_base}/organizations/{org_uuid}/conversations/"
            f"{session_id}/wiggle/upload-file"
        )
        file_ids: list[str] = []
        for attachment in attachments:
            resp = await upload_file_via_page_fetch(
                page,
                url,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                data=attachment.data,
                field_name="file",
                timeout_ms=30000,
            )
            status = int(resp.get("status") or 0)
            if status not in (200, 201):
                text = str(resp.get("text") or "")[:500]
                raise RuntimeError(f"图片上传失败 {status}: {text}")
            data = resp.get("json")
            if not isinstance(data, dict):
                raise RuntimeError("图片上传返回非 JSON")
            file_uuid = data.get("file_uuid") or data.get("uuid")
            if not file_uuid:
                raise RuntimeError("图片上传未返回 file_uuid")
            file_ids.append(str(file_uuid))
        return {"attachments": [], "files": file_ids}


def register_claude_plugin() -> None:
    """注册 Claude 插件到全局 Registry。"""
    PluginRegistry.register(ClaudePlugin())
