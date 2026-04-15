"""
ChatGPT 插件：基于 chatgpt.com Web 端的逆向 API。

站点特有逻辑：
  - 认证方式：Cookie（__Secure-next-auth.session-token 或 accessToken）
  - 会话创建：不需要显式创建——第一次发消息时自动创建
  - Sentinel 挑战：自主生成 proof token (FNV-1a PoW) + Turnstile (dx mode)
  - 流式补全：POST /backend-api/f/conversation  带 sentinel headers
  - SSE 格式：v1 delta 编码 —— event: delta_encoding / event: delta / data: {...}
  - 无需 UI 交互：直接在浏览器内生成 sentinel tokens 并调用 API
"""

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import BrowserContext, Page

from core.plugin.base import BaseSitePlugin, PluginRegistry, SiteConfig
from core.plugin.errors import AccountFrozenError
from core.plugin.helpers import request_json_via_page_fetch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ChatGPT SSE 解析（支持 event: 行 + data: 行的双行格式）
# ---------------------------------------------------------------------------

_UNUSUAL_ACTIVITY_RE = re.compile(
    r"Unusual activity has been detected", re.IGNORECASE
)


def _parse_sse_chunks(
    buffer: str, chunk: str
) -> tuple[str, list[tuple[str, str]]]:
    """
    将 chunk 追加到 buffer，按空行分割出 SSE 事件。
    ChatGPT 使用 event: + data: 双行格式（与 Claude 的纯 data: 不同）。
    返回 (剩余 buffer, [(event_type, data_payload), ...])。
    event_type 为空字符串时表示无 event: 行的普通 data 事件。
    """
    buffer += chunk
    events: list[tuple[str, str]] = []

    blocks = buffer.split("\n\n")
    buffer = blocks[-1]  # 最后一块可能不完整

    for block in blocks[:-1]:
        event_type = ""
        data_parts: list[str] = []
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                payload = line[6:].strip()
                if payload and payload != "[DONE]":
                    data_parts.append(payload)

        for dp in data_parts:
            events.append((event_type, dp))

    return (buffer, events)


# ---------------------------------------------------------------------------
# v1 delta 编码解析
# ---------------------------------------------------------------------------


@dataclass
class StreamPayloadResult:
    """_parse_stream_payload 的返回值。"""
    texts: list[str] = field(default_factory=list)
    message_id: str | None = None
    message_role: str | None = None
    error: str | None = None
    use_v1: bool = False


def _parse_stream_payload(
    event_type: str,
    data: str,
    *,
    use_v1: bool,
    seen_text_by_message_id: dict[str, str],
) -> StreamPayloadResult:
    """
    解析单条 ChatGPT SSE 事件。

    v1 delta 编码流程：
      1. event=delta_encoding, data="v1" → 切换到 v1 模式
      2. event=delta, data={o:"add", v:{message:{...}}} → 初始消息（提取 message_id）
      3. event=delta, data={v:[{p:"/message/content/parts/0", o:"append", v:"text"}]} → 追加文本
      4. 非 delta 事件（type=message_stream_complete 等）→ 元数据，忽略

    legacy 模式（use_v1=False）使用 _extract_legacy_text。
    """
    result = StreamPayloadResult(use_v1=use_v1)

    if event_type == "delta_encoding":
        stripped = data.strip().strip('"')
        if stripped == "v1":
            result.use_v1 = True
        return result

    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return result

    if not isinstance(obj, dict):
        return result

    # 非 delta 事件（type 字段为 message_stream_complete 等元信息）
    obj_type = obj.get("type")
    if obj_type == "error":
        result.error = obj.get("detail") or obj.get("message") or "Unknown error"
        return result

    if event_type == "delta" and use_v1:
        return _parse_v1_delta(obj, result, seen_text_by_message_id)

    # legacy: 非 v1 模式下的传统 data 事件
    if not use_v1 and "message" in obj:
        texts, mid = _extract_legacy_text(obj, seen_text_by_message_id)
        result.texts = texts
        result.message_id = mid
        return result

    return result


def _parse_v1_delta(
    obj: dict[str, Any],
    result: StreamPayloadResult,
    seen_text_by_message_id: dict[str, str],
) -> StreamPayloadResult:
    """解析 v1 delta 编码事件。"""
    result.use_v1 = True
    op = obj.get("o", "")
    v = obj.get("v")

    # v 为 dict 且包含 message：提取 message_id / role / 初始文本
    # 注意：不限制 op=="add"，因为 assistant 消息的事件可能省略 o 字段
    if isinstance(v, dict) and "message" in v:
        msg = v["message"]
        if isinstance(msg, dict):
            mid = msg.get("id")
            if mid:
                result.message_id = str(mid)
                author = msg.get("author")
                role = author.get("role") if isinstance(author, dict) else None
                result.message_role = role
                # 只从 assistant 消息提取文本；user/system/developer 消息是回显，不是输出
                if role in ("assistant", None):
                    content = msg.get("content")
                    if isinstance(content, dict):
                        parts = content.get("parts", [])
                        if parts and isinstance(parts[0], str) and parts[0]:
                            result.texts.append(parts[0])
                            seen_text_by_message_id[result.message_id] = parts[0]
        return result

    # patch / 无 op 的 v 数组：增量补丁
    patches = obj.get("v") if "v" in obj and isinstance(obj.get("v"), list) else None
    if patches is None and op == "patch" and isinstance(v, list):
        patches = v
    if isinstance(patches, list):
        for patch in patches:
            if not isinstance(patch, dict):
                continue
            p = patch.get("p", "")
            po = patch.get("o", "")
            pv = patch.get("v")
            if (
                "/message/content/parts/" in p
                and po == "append"
                and isinstance(pv, str)
            ):
                result.texts.append(pv)

    return result


# ---------------------------------------------------------------------------
# Legacy（非 v1）文本提取
# ---------------------------------------------------------------------------


def _extract_legacy_text(
    obj: dict[str, Any],
    seen_text_by_message_id: dict[str, str],
) -> tuple[list[str], str | None]:
    """
    从传统（非 v1）ChatGPT SSE data 中提取增量文本。
    ChatGPT legacy 模式每条事件包含完整累积文本，需计算增量。
    返回 (texts, message_id)。
    """
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return ([], None)

    author = msg.get("author")
    if isinstance(author, dict) and author.get("role") != "assistant":
        return ([], None)

    mid = msg.get("id")
    if not mid:
        return ([], None)
    mid = str(mid)

    content = msg.get("content")
    if not isinstance(content, dict) or content.get("content_type") != "text":
        return ([], mid)

    parts = content.get("parts", [])
    if not parts or not isinstance(parts[0], str):
        return ([], mid)

    full_text = parts[0]
    prev_text = seen_text_by_message_id.get(mid, "")
    if full_text == prev_text:
        return ([], mid)

    increment = full_text[len(prev_text):]
    seen_text_by_message_id[mid] = full_text
    return ([increment] if increment else [], mid)


# ---------------------------------------------------------------------------
# 错误解析
# ---------------------------------------------------------------------------


def _detect_stream_terminal(data_str: str, event_type: str, use_v1: bool) -> bool:
    """检测 SSE 流是否到达终止事件。"""
    try:
        obj = json.loads(data_str)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(obj, dict):
        return False
    if obj.get("type") == "message_stream_complete":
        return True
    if event_type == "delta" and use_v1:
        patches = obj.get("v")
        if isinstance(patches, list):
            for patch in patches:
                if (
                    isinstance(patch, dict)
                    and patch.get("p") == "/message/status"
                    and patch.get("v") == "finished_successfully"
                ):
                    return True
    return False


def _extract_conversation_id(data_str: str) -> str | None:
    """从 SSE data 中提取 conversation_id。"""
    try:
        obj = json.loads(data_str)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict):
        cid = obj.get("conversation_id")
        if cid:
            return str(cid)
    return None


def _freeze_until_from_error(message: str) -> int | None:
    """
    从错误信息解析冻结时间。
    ChatGPT 遇到风控时返回 HTTP 403 + "Unusual activity" 消息，冻结 ~30 分钟。
    """
    if _UNUSUAL_ACTIVITY_RE.search(message):
        return int(time.time()) + 1800

    if "429" in message:
        return int(time.time()) + 3600

    return None


# ---------------------------------------------------------------------------
# 浏览器端 JS —— 自主 Sentinel Token 生成 + 直接 API 调用
# ---------------------------------------------------------------------------

# 完整的 sentinel 生成 + 流式请求逻辑，在浏览器内执行。
# 流程：
#   1. 获取 Bearer token（/api/auth/session）
#   2. 生成 requirements token（浏览器指纹 → FNV-1a PoW）
#   3. 调用 /sentinel/chat-requirements/prepare
#   4. 解算 PoW 挑战
#   5. 调用 /sentinel/chat-requirements/finalize
#   6. 带 sentinel headers 直接 POST /f/conversation
#   7. SSE 流通过 CDP binding 回传
_SENTINEL_STREAM_JS = """
async ({ bindingName, bodyStr }) => {
  const send = globalThis[bindingName];
  try {
    // ===== 辅助函数（从 ChatGPT 前端逆向） =====
    function kG(e) { return e[Math.floor(Math.random()*e.length)] }
    function _qt() {
      let e = kG(Object.keys(Object.getPrototypeOf(navigator)));
      try { return e + "\u2212" + navigator[e].toString() } catch { return e }
    }
    function AG(e) {
      e = JSON.stringify(e);
      return window.TextEncoder
        ? btoa(String.fromCharCode(...new TextEncoder().encode(e)))
        : btoa(unescape(encodeURIComponent(e)));
    }
    function mqt(e) {
      let t = 2166136261;
      for (let n = 0; n < e.length; n++)
        t ^= e.charCodeAt(n), t = Math.imul(t, 16777619) >>> 0;
      t ^= t >>> 16; t = Math.imul(t, 2246822507) >>> 0;
      t ^= t >>> 13; t = Math.imul(t, 3266489909) >>> 0;
      t ^= t >>> 16;
      return (t >>> 0).toString(16).padStart(8, "0");
    }
    const sid = crypto.randomUUID();
    function getConfig() {
      return [
        screen?.width + screen?.height, "" + new Date,
        performance?.memory?.jsHeapSizeLimit, Math.random(),
        navigator.userAgent,
        kG(Array.from(document.scripts).map(e=>e?.src).filter(e=>e)),
        (Array.from(document.scripts||[]).map(e=>e?.src?.match("c/[^/]*/_")).filter(e=>e?.length)[0]??[])[0]
          ?? document.documentElement.getAttribute("data-build"),
        navigator.language, navigator.languages?.join(","), Math.random(),
        _qt(), kG(Object.keys(document)), kG(Object.keys(window)),
        performance.now(), sid,
        [...new URLSearchParams(window.location.search).keys()].join(","),
        navigator?.hardwareConcurrency, performance.timeOrigin,
        Number("ai" in window), Number("createPRNG" in window),
        Number("cache" in window), Number("data" in window),
        Number("solana" in window), Number("dump" in window),
        Number("InstallTrigger" in window),
      ];
    }

    // ===== 1. Bearer token =====
    const sess = await (await fetch("/api/auth/session", {credentials:"include"})).json();
    const accessToken = sess.accessToken;
    if (!accessToken) { await send("__error__:No access token"); await send("__done__"); return; }
    const did = JSON.parse(localStorage.getItem("oai-did") || '""');

    // ===== 2. Requirements token (p 参数) =====
    const c1 = getConfig(); c1[3] = 1; c1[9] = 0;
    const reqToken = "gAAAAAC" + AG(c1);

    // ===== 3. /sentinel/chat-requirements/prepare =====
    const prepResp = await fetch("/backend-api/sentinel/chat-requirements/prepare", {
      method: "POST", credentials: "include",
      headers: {"Content-Type":"application/json", "Authorization":"Bearer "+accessToken},
      body: JSON.stringify({p: reqToken}),
    });
    if (!prepResp.ok) {
      await send("__error__:prepare failed " + prepResp.status);
      await send("__done__"); return;
    }
    const prep = await prepResp.json();

    // ===== 4. PoW =====
    let proofAnswer = null;
    if (prep.proofofwork?.required) {
      const {seed, difficulty} = prep.proofofwork;
      const t0 = performance.now();
      for (let i = 0; i < 500000; i++) {
        const c = getConfig(); c[3] = i; c[9] = Math.round(performance.now() - t0);
        const a = AG(c);
        if (mqt(seed + a).substring(0, difficulty.length) <= difficulty) {
          proofAnswer = "gAAAAAB" + a + "~S"; break;
        }
      }
    }

    // ===== 5. Turnstile (dx mode) =====
    const turnstileToken = prep.turnstile?.dx || "";

    // ===== 6. /sentinel/chat-requirements/finalize =====
    const finBody = {prepare_token: prep.prepare_token || ""};
    if (proofAnswer) finBody.proofofwork = proofAnswer;
    if (turnstileToken) finBody.turnstile = turnstileToken;
    const finResp = await fetch("/backend-api/sentinel/chat-requirements/finalize", {
      method: "POST", credentials: "include",
      headers: {"Content-Type":"application/json", "Authorization":"Bearer "+accessToken},
      body: JSON.stringify(finBody),
    });
    if (!finResp.ok) {
      await send("__error__:finalize failed " + finResp.status);
      await send("__done__"); return;
    }
    await finResp.json();

    // ===== 7. POST /f/conversation =====
    const resp = await fetch("/backend-api/f/conversation", {
      method: "POST", credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": "Bearer " + accessToken,
        "oai-device-id": did,
        "oai-language": navigator.language,
        "openai-sentinel-proof-token": proofAnswer || "",
        "openai-sentinel-turnstile-token": turnstileToken,
        "openai-sentinel-chat-requirements-prepare-token": prep.prepare_token || "",
      },
      body: bodyStr,
    });

    await send("__status__:" + resp.status);
    if (!resp.ok) {
      const t = await resp.text();
      await send("__error__:HTTP " + resp.status + " " + (t.length > 800 ? t.slice(0,800)+"..." : t));
      await send("__done__"); return;
    }
    if (!resp.body) {
      await send("__error__:No response body");
      await send("__done__"); return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      await send(dec.decode(value));
    }
    await send("__done__");
  } catch(e) {
    const msg = (e.message || String(e)) + (e.stack ? " | stack:" + e.stack.split("\\n").slice(0,3).join(" ") : "");
    try { await send("__error__:" + msg); } catch {}
    try { await send("__done__"); } catch {}
  }
}
"""


# ---------------------------------------------------------------------------
# ChatGPTPlugin
# ---------------------------------------------------------------------------


class ChatGPTPlugin(BaseSitePlugin):
    """ChatGPT Web2API 插件。"""

    type_name = "chatgpt"

    site = SiteConfig(
        start_url="https://chatgpt.com",
        api_base="https://chatgpt.com/backend-api",
        cookie_name="__Secure-next-auth.session-token",
        cookie_domain=".chatgpt.com",
        auth_keys=["__Secure-next-auth.session-token", "session_token"],
        config_section="chatgpt",
    )

    # ---- 5 个必须实现的 hook（其中 stream_completion 被完整覆盖） ----

    async def fetch_site_context(
        self, context: BrowserContext, page: Page
    ) -> dict[str, Any] | None:
        """获取当前用户信息。ChatGPT 不需要复杂的上下文，只需确认已登录。"""
        del context
        try:
            resp = await request_json_via_page_fetch(
                page,
                f"{self.api_base}/me",
                timeout_ms=15000,
            )
        except Exception as e:
            logger.warning("[%s] fetch /me 失败: %s", self.type_name, e)
            return None

        status = int(resp.get("status") or 0)
        if status != 200:
            logger.warning(
                "[%s] fetch_site_context /me status=%s",
                self.type_name,
                status,
            )
            return None

        data = resp.get("json")
        if not isinstance(data, dict):
            logger.warning("[%s] /me 返回非 JSON", self.type_name)
            return None

        user_id = data.get("id") or data.get("user_id")
        email = data.get("email") or ""
        if not user_id:
            # 某些情况下 /me 返回不同结构
            user = data.get("user")
            if isinstance(user, dict):
                user_id = user.get("id") or user.get("user_id")

        logger.info(
            "[%s] fetch_site_context 成功 user_id=%s email=%s",
            self.type_name,
            user_id,
            email,
        )
        return {"user_id": str(user_id or ""), "email": email}

    async def create_session(
        self,
        context: BrowserContext,
        page: Page,
        site_context: dict[str, Any],
    ) -> str | None:
        """
        ChatGPT 不需要显式创建会话——第一次发消息时自动创建。
        返回一个本地生成的 UUID 作为会话 ID 占位符，
        实际的 conversation_id 在流式响应中获取。
        """
        del context, page, site_context
        return str(uuid.uuid4())

    def build_completion_url(self, session_id: str, state: dict[str, Any]) -> str:
        return f"{self.api_base}/f/conversation"

    def build_completion_body(
        self,
        message: str,
        session_id: str,
        state: dict[str, Any],
        prepared_attachments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tz = state.get("timezone") or "UTC"
        parent_message_id = state.get("parent_message_id") or "client-created-root"
        conversation_id = state.get("conversation_id")

        msg_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "action": "next",
            "messages": [
                {
                    "id": msg_id,
                    "author": {"role": "user"},
                    "create_time": time.time(),
                    "content": {"content_type": "text", "parts": [message]},
                    "metadata": {
                        "serialization_metadata": {"custom_symbol_offsets": []},
                    },
                }
            ],
            "parent_message_id": parent_message_id,
            "model": "auto",
            "timezone_offset_min": 0,
            "timezone": tz,
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "system_hints": [],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {
                "is_dark_mode": True,
                "time_since_loaded": 1000,
                "page_height": 210,
                "page_width": 1200,
                "pixel_ratio": 2,
                "screen_height": 982,
                "screen_width": 1512,
                "app_name": "chatgpt.com",
            },
            "paragen_cot_summary_display_override": "allow",
        }

        if conversation_id:
            body["conversation_id"] = conversation_id

        return body

    def parse_stream_event(
        self, payload: str
    ) -> tuple[list[str], str | None, str | None]:
        # 不使用基类的 SSE 解析路径——ChatGPT 在 stream_completion 中完整覆盖
        return ([], None, None)

    def is_stream_end_event(self, payload: str) -> bool:
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(obj, dict):
            return False
        return obj.get("type") == "message_stream_complete"

    # ---- 完整覆盖 stream_completion：自主 Sentinel 方案 ----
    #
    # 自主生成 sentinel tokens 并直接调用 API，无需 UI 交互。
    # 流程（全在浏览器内执行）：
    #   1. /api/auth/session → Bearer token
    #   2. 浏览器指纹 → requirements token (p 参数)
    #   3. /sentinel/chat-requirements/prepare → prepare_token + PoW challenge
    #   4. FNV-1a PoW 解算
    #   5. /sentinel/chat-requirements/finalize → 最终 token
    #   6. 带 sentinel headers 直接 POST /f/conversation
    #   7. SSE 流通过 CDP binding 回传
    #
    # 优势：
    #   - 不触碰 UI（无输入框/发送按钮交互）
    #   - 不需要每次导航/重置页面
    #   - 多轮对话靠 body 中 conversation_id + parent_message_id 维持

    async def stream_completion(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        message: str,
        **kwargs: Any,
    ) -> Any:
        state = self._session_state.get(session_id)
        if not state:
            raise RuntimeError(f"未知会话 ID: {session_id}")

        body = self.build_completion_body(message, session_id, state)
        request_id: str = kwargs.get("request_id", "")

        logger.info(
            "[%s] stream_completion session_id=%s conv_id=%s",
            self.type_name,
            session_id,
            state.get("conversation_id"),
        )

        chunk_queue: asyncio.Queue[str] = asyncio.Queue()
        binding_name = f"sendChunk_{request_id}"

        def on_binding_called(event: dict[str, Any]) -> None:
            if event.get("name") == binding_name:
                p = event.get("payload", "")
                chunk_queue.put_nowait(p if isinstance(p, str) else str(p))

        cdp = None
        eval_task: asyncio.Task[Any] | None = None
        try:
            cdp = await context.new_cdp_session(page)
            cdp.on("Runtime.bindingCalled", on_binding_called)
            await cdp.send("Runtime.addBinding", {"name": binding_name})

            body_str = json.dumps(body)
            eval_task = asyncio.create_task(
                page.evaluate(
                    _SENTINEL_STREAM_JS,
                    {"bindingName": binding_name, "bodyStr": body_str},
                )
            )

            buffer = ""
            assistant_message_id: str | None = None
            stream_terminal = False
            use_v1 = False
            seen_text: dict[str, str] = {}

            while True:
                try:
                    chunk = await asyncio.wait_for(chunk_queue.get(), timeout=130.0)
                except asyncio.TimeoutError:
                    logger.warning("[%s] 流式读取超时", self.type_name)
                    break

                if chunk == "__done__":
                    break
                if chunk.startswith("__status__:"):
                    status_code = chunk[11:].strip()
                    logger.debug("[%s] HTTP status: %s", self.type_name, status_code)
                    continue
                if chunk.startswith("__error__:"):
                    msg = chunk[10:].strip()
                    if stream_terminal:
                        continue
                    freeze = _freeze_until_from_error(msg)
                    if freeze is not None:
                        raise AccountFrozenError(msg, freeze)
                    logger.warning("[%s] __error__: %s", self.type_name, msg)
                    raise RuntimeError(msg)

                buffer, events = _parse_sse_chunks(buffer, chunk)
                for event_type, data_str in events:
                    pr = _parse_stream_payload(
                        event_type,
                        data_str,
                        use_v1=use_v1,
                        seen_text_by_message_id=seen_text,
                    )
                    use_v1 = pr.use_v1

                    if pr.error:
                        logger.debug("[%s] SSE error: %s", self.type_name, pr.error)
                        continue

                    if pr.message_id and pr.message_role == "assistant":
                        assistant_message_id = pr.message_id

                    if _detect_stream_terminal(data_str, event_type, use_v1):
                        stream_terminal = True

                    for t in pr.texts:
                        yield t

                    if not state.get("conversation_id"):
                        cid = _extract_conversation_id(data_str)
                        if cid and session_id in self._session_state:
                            self._session_state[session_id]["conversation_id"] = cid

            if assistant_message_id and session_id in self._session_state:
                self._session_state[session_id]["parent_message_id"] = (
                    assistant_message_id
                )
                logger.info(
                    "[%s] updated parent_message_id=%s",
                    self.type_name,
                    assistant_message_id,
                )

        finally:
            if eval_task is not None:
                if not eval_task.done():
                    eval_task.cancel()
                    try:
                        await eval_task
                    except (asyncio.CancelledError, Exception):
                        pass
                elif not eval_task.cancelled() and eval_task.exception():
                    logger.debug("evaluate 异常: %s", eval_task.exception())
            if cdp is not None:
                try:
                    await cdp.detach()
                except Exception as e:
                    logger.debug("detach CDP session 异常: %s", e)

    def on_http_error(
        self, message: str, headers: dict[str, str] | None
    ) -> int | None:
        return _freeze_until_from_error(message)


def register_chatgpt_plugin() -> None:
    """注册 ChatGPT 插件到全局 Registry。"""
    PluginRegistry.register(ChatGPTPlugin())
