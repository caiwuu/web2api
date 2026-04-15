"""
DeepSeek 插件：基于 chat.deepseek.com Web 端的逆向 API。

站点特有逻辑：
  - 认证方式：localStorage 中的 userToken（JWT），请求时需带 Authorization: Bearer <token>
  - 所有 API 请求需携带客户端标识 header（x-app-version, x-client-platform 等）
  - 会话创建：POST /api/v0/chat_session/create  body: {"agent":"chat"}
  - PoW 挑战：POST /api/v0/chat/create_pow_challenge  => Base64 编码后放入 x-ds-pow-response
  - 流式补全：POST /api/v0/chat/completion  带 Authorization + x-ds-pow-response（Base64）
  - SSE 格式：data: {"v":"text","p":"response/content"} / {"v":"FINISHED","p":"response/status"}
"""

import asyncio
import base64
import json
import logging
import time
from typing import Any

from playwright.async_api import BrowserContext, Page

from core.plugin.base import BaseSitePlugin, PluginRegistry, SiteConfig
from core.plugin.helpers import parse_sse_to_events
from core.plugin.errors import AccountFrozenError

logger = logging.getLogger(__name__)

# DeepSeek API 要求的客户端标识 header
_DS_CLIENT_HEADERS: dict[str, str] = {
    "x-app-version": "20241129.1",
    "x-client-locale": "zh_CN",
    "x-client-platform": "web",
    "x-client-version": "1.8.0",
}

# 从 localStorage 提取 userToken 的 JS
_GET_USER_TOKEN_JS = """
() => {
  try {
    const raw = localStorage.getItem("userToken");
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return (parsed && parsed.value) ? parsed.value : raw;
  } catch(e) {
    return localStorage.getItem("userToken") || null;
  }
}
"""

# 带自定义 header 的非流式 fetch JS
_AUTHED_FETCH_JSON_JS = """
async ({ url, method, body, extraHeaders, timeoutMs }) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs || 15000);
  try {
    const headers = { "Content-Type": "application/json", ...extraHeaders };
    const resp = await fetch(url, {
      method: method || "GET",
      body: body ?? undefined,
      headers,
      credentials: "include",
      signal: ctrl.signal
    });
    clearTimeout(t);
    const text = await resp.text();
    const headersObj = {};
    resp.headers.forEach((v, k) => { headersObj[k] = v; });
    return { ok: resp.ok, status: resp.status, text, headers: headersObj };
  } catch (e) {
    clearTimeout(t);
    const msg = e.name === "AbortError"
      ? `请求超时(${Math.floor((timeoutMs || 15000) / 1000)}s)`
      : (e.message || String(e));
    return { error: msg };
  }
}
"""

# 带自定义 header 的流式 fetch JS
_AUTHED_FETCH_STREAM_JS = """
async ({ url, body, bindingName, extraHeaders }) => {
  const send = globalThis[bindingName];
  const done = "__done__";
  const errPrefix = "__error__:";
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 120000);
    const headers = {
      "Content-Type": "application/json",
      "Accept": "*/*",
      ...extraHeaders
    };
    const resp = await fetch(url, {
      method: "POST",
      body: body,
      headers: headers,
      credentials: "include",
      signal: ctrl.signal
    });
    clearTimeout(t);
    if (!resp.ok) {
      const errText = await resp.text();
      const errSnippet = (errText && errText.length > 800) ? errText.slice(0, 800) + "..." : (errText || "");
      await send(errPrefix + "HTTP " + resp.status + " " + errSnippet);
      await send(done);
      return;
    }
    if (!resp.body) {
      await send(errPrefix + "No response body");
      await send(done);
      return;
    }
    const headersObj = {};
    resp.headers.forEach((v, k) => { headersObj[k] = v; });
    await send("__headers__:" + JSON.stringify(headersObj));
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    while (true) {
      const { done: streamDone, value } = await reader.read();
      if (streamDone) break;
      await send(dec.decode(value));
    }
  } catch (e) {
    const msg = e.name === "AbortError" ? "请求超时(120s)" : (e.message || String(e));
    await send(errPrefix + msg);
  }
  await send(done);
}
"""

# 加载 DeepSeek sha3 WASM 并调用 wasm_solve
_SOLVE_POW_JS = """
async ({ challenge, salt, difficulty, expireAt }) => {
  const wasmUrl = 'https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm';
  try {
    const wasmResp = await fetch(wasmUrl);
    if (!wasmResp.ok) {
      return { error: 'WASM_FETCH_FAILED: ' + wasmResp.status };
    }
    const wasmBytes = await wasmResp.arrayBuffer();
    const { instance } = await WebAssembly.instantiate(wasmBytes, { wbg: {} });
    const ex = instance.exports;

    if (!ex.wasm_solve || !ex.__wbindgen_add_to_stack_pointer || !ex.__wbindgen_export_0 || !ex.memory) {
      return { error: 'WASM_EXPORTS_MISSING: ' + Object.keys(ex).join(',') };
    }

    const encoder = new TextEncoder();
    function encodeString(text) {
      const encoded = encoder.encode(text);
      const ptr = ex.__wbindgen_export_0(encoded.length, 1) >>> 0;
      new Uint8Array(ex.memory.buffer).set(encoded, ptr);
      return [ptr, encoded.length];
    }

    const prefix = salt + '_' + expireAt + '_';
    const retptr = ex.__wbindgen_add_to_stack_pointer(-16);
    const [ptrC, lenC] = encodeString(challenge);
    const [ptrP, lenP] = encodeString(prefix);

    ex.wasm_solve(retptr, ptrC, lenC, ptrP, lenP, difficulty);

    const dv = new DataView(ex.memory.buffer);
    const status = dv.getInt32(retptr, true);
    const answer = dv.getFloat64(retptr + 8, true);
    ex.__wbindgen_add_to_stack_pointer(16);

    if (status !== 0) {
      return { answer: Math.floor(answer) };
    }
    return { error: 'WASM_SOLVE_FAILED' };
  } catch (e) {
    return { error: 'WASM_ERROR: ' + (e.message || String(e)) };
  }
}
"""


def _build_auth_headers(token: str) -> dict[str, str]:
    """构建带 Authorization + 客户端标识的 header 集合。"""
    headers = dict(_DS_CLIENT_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get_user_token(page: Page) -> str | None:
    try:
        token = await page.evaluate(_GET_USER_TOKEN_JS)
        if token and isinstance(token, str) and len(token) > 10:
            return token
    except Exception as e:
        logger.warning("[deepseek] 提取 userToken 失败: %s", e)
    return None


async def _authed_json_fetch(
    page: Page,
    url: str,
    token: str,
    *,
    method: str = "GET",
    body: str | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout_ms: int = 15000,
) -> dict[str, Any]:
    headers = _build_auth_headers(token)
    if extra_headers:
        headers.update(extra_headers)
    result = await page.evaluate(
        _AUTHED_FETCH_JSON_JS,
        {
            "url": url,
            "method": method,
            "body": body,
            "extraHeaders": headers,
            "timeoutMs": timeout_ms,
        },
    )
    if not isinstance(result, dict):
        raise RuntimeError("页面 fetch 返回结果异常")
    error = result.get("error")
    if error:
        raise RuntimeError(str(error))
    text = result.get("text")
    if isinstance(text, str) and text:
        try:
            result["json"] = json.loads(text)
        except json.JSONDecodeError:
            result["json"] = None
    else:
        result["json"] = None
    return result


def _parse_deepseek_sse_event(
    payload: str,
) -> tuple[list[str], str | None, str | None]:
    """
    解析 DeepSeek SSE data 行。返回 (texts, message_id, error_message)。

    新版格式:
      {"v": "text"}                                    → 内容块
      {"v": {"response": {"message_id": 2, ...}}}      → 初始响应（提取 message_id）
      {"p": "response/fragments/-1/content", "o": "APPEND", "v": "..."} → 片段操作（跳过）
      {"p": "response", "o": "BATCH", "v": [...]}      → 批量状态更新（跳过）
      {"p": "response/status", "o": "SET", "v": "FINISHED"} → 流结束
    """
    result: list[str] = []
    message_id: str | None = None
    error_message: str | None = None

    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return (result, message_id, error_message)

    if not isinstance(obj, dict):
        return (result, message_id, error_message)

    if "v" in obj:
        p = obj.get("p", "")
        o = obj.get("o", "")
        v = obj["v"]

        # 提取 message_id（顶层或嵌套在 v.response 中）
        if obj.get("response_message_id"):
            message_id = str(obj["response_message_id"])
        elif isinstance(v, dict):
            resp = v.get("response")
            if isinstance(resp, dict) and resp.get("message_id"):
                message_id = str(resp["message_id"])

        # 状态事件
        if p in ("response/status", "response/search_status"):
            return (result, message_id, error_message)

        # 有操作符的事件（APPEND/BATCH/SET）→ 内部操作，跳过内容提取
        if o:
            return (result, message_id, error_message)

        # v 是字符串且无 p → 内容块
        if isinstance(v, str) and v and not p:
            result.append(v)
        # v 是字符串且 p 是 response/content → 旧格式内容
        elif p == "response/content" and isinstance(v, str) and v:
            result.append(v)

        return (result, message_id, error_message)

    # 旧版 OpenAI 兼容格式
    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content", "")
                ctype = delta.get("type", "text")
                if ctype == "text" and content:
                    result.append(str(content))

    if obj.get("code") and obj.get("code") != 0:
        error_message = obj.get("msg") or obj.get("message") or "Unknown error"

    return (result, message_id, error_message)


def _is_deepseek_terminal_event(payload: str) -> bool:
    """检测流结束：{"p":"response/status","v":"FINISHED"} 或带 "o":"SET" 的变体。"""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    return obj.get("p") == "response/status" and obj.get("v") == "FINISHED"


# ---------------------------------------------------------------------------
# DeepSeekPlugin
# ---------------------------------------------------------------------------


class DeepSeekPlugin(BaseSitePlugin):
    """DeepSeek Web2API 插件。"""

    type_name = "deepseek"

    site = SiteConfig(
        start_url="https://chat.deepseek.com",
        api_base="https://chat.deepseek.com/api/v0",
        cookie_name="ds_session",
        cookie_domain=".deepseek.com",
        auth_keys=["ds_session", "ds_session_id", "userToken"],
        config_section="deepseek",
    )

    async def fetch_site_context(
        self, context: BrowserContext, page: Page
    ) -> dict[str, Any] | None:
        del context
        token = await _get_user_token(page)
        if not token:
            logger.warning(
                "[%s] localStorage 中无 userToken，请先登录 chat.deepseek.com",
                self.type_name,
            )
            return None

        resp = await _authed_json_fetch(
            page, f"{self.api_base}/users/current", token, timeout_ms=15000
        )
        status = int(resp.get("status") or 0)
        if status != 200:
            logger.warning(
                "[%s] fetch_site_context status=%s body=%s",
                self.type_name,
                status,
                str(resp.get("text") or "")[:300],
            )
            return None

        data = resp.get("json")
        if not isinstance(data, dict):
            logger.warning("[%s] fetch_site_context 返回非 JSON", self.type_name)
            return None

        code = data.get("code")
        if code and code != 0:
            logger.warning(
                "[%s] fetch_site_context code=%s msg=%s",
                self.type_name,
                code,
                data.get("msg"),
            )
            return None

        inner = data.get("data")
        if not isinstance(inner, dict):
            logger.warning(
                "[%s] fetch_site_context data 无效 (code=%s msg=%s)",
                self.type_name,
                code,
                data.get("msg"),
            )
            return None
        biz = inner.get("biz_data")
        if not isinstance(biz, dict):
            biz = inner
        # user_id 可能在 biz_data.user.id 或 biz_data.id
        user_obj = biz.get("user")
        if isinstance(user_obj, dict):
            user_id = user_obj.get("id") or user_obj.get("user_id")
        else:
            user_id = biz.get("id") or biz.get("user_id")
        if not user_id:
            logger.warning(
                "[%s] fetch_site_context 无法获取 user_id, keys=%s",
                self.type_name,
                list(biz.keys()),
            )
            return None

        logger.info("[%s] fetch_site_context 成功 user_id=%s", self.type_name, user_id)
        return {"user_id": str(user_id), "token": token}

    async def create_session(
        self,
        context: BrowserContext,
        page: Page,
        site_context: dict[str, Any],
    ) -> str | None:
        del context
        token = site_context.get("token", "")
        resp = await _authed_json_fetch(
            page,
            f"{self.api_base}/chat_session/create",
            token,
            method="POST",
            body=json.dumps({"agent": "chat"}),
            timeout_ms=15000,
        )
        status = int(resp.get("status") or 0)
        if status != 200:
            logger.warning(
                "[%s] create_session status=%s body=%s",
                self.type_name,
                status,
                str(resp.get("text") or "")[:300],
            )
            return None

        data = resp.get("json")
        if not isinstance(data, dict) or data.get("code") != 0:
            logger.warning(
                "[%s] create_session 失败: %s",
                self.type_name,
                str(resp.get("text") or "")[:300],
            )
            return None

        inner = data.get("data")
        if not isinstance(inner, dict):
            return None
        biz = inner.get("biz_data")
        if not isinstance(biz, dict):
            biz = inner
        # session ID 在 biz_data.chat_session.id
        chat_session = biz.get("chat_session")
        if isinstance(chat_session, dict):
            session_id = chat_session.get("id")
        else:
            session_id = biz.get("id") or inner.get("id")
        if not session_id:
            logger.warning(
                "[%s] create_session 未返回 session_id, keys=%s",
                self.type_name,
                list(biz.keys()),
            )
            return None

        return str(session_id)

    def build_completion_url(self, session_id: str, state: dict[str, Any]) -> str:
        return f"{self.api_base}/chat/completion"

    def build_completion_body(
        self,
        message: str,
        session_id: str,
        state: dict[str, Any],
        prepared_attachments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "chat_session_id": session_id,
            "parent_message_id": state.get("parent_message_id"),
            "model_type": "expert",
            "prompt": message,
            "ref_file_ids": [],
            "thinking_enabled": True,
            "search_enabled": True,
            "preempt": False,
        }

    def parse_stream_event(
        self, payload: str
    ) -> tuple[list[str], str | None, str | None]:
        return _parse_deepseek_sse_event(payload)

    def is_stream_end_event(self, payload: str) -> bool:
        return _is_deepseek_terminal_event(payload)

    # ---- stream_completion：带 Auth + PoW + 客户端 header ----

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

        token = state.get("site_context", {}).get("token", "")
        if not token:
            token = await _get_user_token(page) or ""

        pow_header_value = await self._get_pow_response(page, token)

        url = self.build_completion_url(session_id, state)
        body = self.build_completion_body(message, session_id, state)
        body_json = json.dumps(body)
        request_id: str = kwargs.get("request_id", "")

        extra_headers = _build_auth_headers(token)
        if pow_header_value:
            extra_headers["x-ds-pow-response"] = pow_header_value

        logger.info(
            "[%s] stream_completion session_id=%s pow=%s auth=%s",
            self.type_name,
            session_id,
            "yes" if pow_header_value else "no",
            "yes" if token else "no",
        )

        chunk_queue: asyncio.Queue[str] = asyncio.Queue()
        binding_name = f"sendChunk_{request_id}"

        def on_binding_called(event: dict[str, Any]) -> None:
            if event.get("name") == binding_name:
                p = event.get("payload", "")
                chunk_queue.put_nowait(p if isinstance(p, str) else str(p))

        cdp = None
        try:
            cdp = await context.new_cdp_session(page)
            cdp.on("Runtime.bindingCalled", on_binding_called)
            await cdp.send("Runtime.addBinding", {"name": binding_name})

            async def run_fetch() -> None:
                await page.evaluate(
                    _AUTHED_FETCH_STREAM_JS,
                    {
                        "url": url,
                        "body": body_json,
                        "bindingName": binding_name,
                        "extraHeaders": extra_headers,
                    },
                )

            fetch_task = asyncio.create_task(run_fetch())
            buffer = ""
            out_message_ids: list[str] = []
            stream_terminal = False
            in_think = False
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(chunk_queue.get(), timeout=130.0)
                    except asyncio.TimeoutError:
                        logger.warning("[%s] 流式读取超时", self.type_name)
                        break

                    if chunk == "__done__":
                        break
                    if chunk.startswith("__headers__:"):
                        continue
                    if chunk.startswith("__error__:"):
                        msg = chunk[10:].strip()
                        if stream_terminal:
                            continue
                        if "429" in msg:
                            raise AccountFrozenError(msg, int(time.time()) + 3600)
                        logger.warning("[%s] __error__: %s", self.type_name, msg)
                        raise RuntimeError(msg)

                    buffer, payloads = parse_sse_to_events(buffer, chunk)
                    for payload in payloads:
                        if self.is_stream_end_event(payload):
                            stream_terminal = True

                        try:
                            evt = json.loads(payload)
                        except (json.JSONDecodeError, ValueError):
                            evt = None

                        if not isinstance(evt, dict) or "v" not in evt:
                            continue

                        ep = evt.get("p", "")
                        eo = evt.get("o", "")
                        ev = evt["v"]

                        # --- 提取 message_id ---
                        if evt.get("response_message_id"):
                            mid = str(evt["response_message_id"])
                            if mid not in out_message_ids:
                                out_message_ids.append(mid)
                        elif isinstance(ev, dict):
                            resp = ev.get("response")
                            if isinstance(resp, dict) and resp.get("message_id"):
                                mid = str(resp["message_id"])
                                if mid not in out_message_ids:
                                    out_message_ids.append(mid)

                        # --- 1. 初始响应：检测首个 fragment 类型 ---
                        if isinstance(ev, dict) and "response" in ev:
                            resp = ev["response"]
                            if isinstance(resp, dict):
                                frags = resp.get("fragments", [])
                                if frags and isinstance(frags[0], dict):
                                    ftype = frags[0].get("type", "")
                                    initial = frags[0].get("content", "")
                                    if ftype == "THINK":
                                        in_think = True
                                        yield "<think>"
                                        if initial:
                                            yield initial
                                    elif ftype == "RESPONSE" and initial:
                                        yield initial
                            continue

                        # --- 2. 新 fragment 追加（THINK→RESPONSE 切换） ---
                        if (
                            ep == "response/fragments"
                            and eo == "APPEND"
                            and isinstance(ev, list)
                        ):
                            for frag in ev:
                                if not isinstance(frag, dict):
                                    continue
                                ftype = frag.get("type", "")
                                initial = frag.get("content", "")
                                if ftype == "RESPONSE":
                                    if in_think:
                                        in_think = False
                                        yield "</think>"
                                    if initial:
                                        yield initial
                                elif ftype == "THINK":
                                    if not in_think:
                                        in_think = True
                                        yield "<think>"
                                    if initial:
                                        yield initial
                            continue

                        # --- 3. fragment content APPEND ---
                        if (
                            "fragments" in ep
                            and "content" in ep
                            and eo == "APPEND"
                            and isinstance(ev, str)
                        ):
                            yield ev
                            continue

                        # --- 4. elapsed_secs SET → 思考结束 ---
                        if "elapsed_secs" in ep and eo == "SET":
                            if in_think:
                                in_think = False
                                yield "</think>"
                            continue

                        # --- 5. fragment content 赋值（有 p 无 o） ---
                        if (
                            "fragments" in ep
                            and "content" in ep
                            and not eo
                            and isinstance(ev, str)
                        ):
                            yield ev
                            continue

                        # --- 6. 状态/批量事件 → 跳过 ---
                        if eo:
                            continue

                        # --- 7. 裸 {"v":"text"} → 正文内容 ---
                        if isinstance(ev, str) and ev and not ep:
                            yield ev
            finally:
                try:
                    await asyncio.wait_for(fetch_task, timeout=5.0)
                except asyncio.TimeoutError:
                    fetch_task.cancel()
                    try:
                        await fetch_task
                    except asyncio.CancelledError:
                        pass

            if out_message_ids and session_id in self._session_state:
                self.on_stream_completion_finished(session_id, out_message_ids)

        finally:
            if cdp is not None:
                try:
                    await cdp.detach()
                except Exception as e:
                    logger.debug("detach CDP session 异常: %s", e)

    async def _get_pow_response(self, page: Page, token: str) -> str | None:
        """获取 PoW 挑战、求解、Base64 编码后返回。"""
        try:
            resp = await _authed_json_fetch(
                page,
                f"{self.api_base}/chat/create_pow_challenge",
                token,
                method="POST",
                body=json.dumps({"target_path": "/api/v0/chat/completion"}),
                timeout_ms=10000,
            )
            data = resp.get("json")
            if not isinstance(data, dict) or data.get("code") != 0:
                logger.warning(
                    "[%s] PoW challenge 失败: %s",
                    self.type_name,
                    str(resp.get("text") or "")[:200],
                )
                return None

            inner = data.get("data")
            if not isinstance(inner, dict):
                logger.warning("[%s] PoW data 为空", self.type_name)
                return None

            # 逐层解嵌套找到 PoW 参数
            # 可能的结构：
            #   data.data.biz_data.challenge = {algorithm, challenge, salt, ...}
            #   data.data.biz_data = {algorithm, challenge(str), salt, ...}
            #   data.data = {algorithm, challenge(str), salt, ...}
            biz = inner.get("biz_data") if isinstance(inner, dict) else None
            pow_data = None

            for candidate in [biz, inner]:
                if not isinstance(candidate, dict):
                    continue
                ch = candidate.get("challenge")
                if isinstance(ch, dict) and "algorithm" in ch:
                    pow_data = ch
                    break
                if isinstance(ch, str) and "algorithm" in candidate:
                    pow_data = candidate
                    break

            if pow_data is None:
                pow_data = biz if isinstance(biz, dict) else inner

            if not isinstance(pow_data, dict):
                logger.warning("[%s] PoW data 结构无法解析", self.type_name)
                return None

            algorithm = str(pow_data.get("algorithm", ""))
            challenge = pow_data.get("challenge")
            if not isinstance(challenge, str):
                logger.warning(
                    "[%s] challenge 类型异常: %s, pow_data keys=%s",
                    self.type_name,
                    type(challenge).__name__,
                    list(pow_data.keys()),
                )
                return None
            salt = str(pow_data.get("salt", ""))
            difficulty = int(pow_data.get("difficulty", 0))
            expire_at = pow_data.get("expire_at", 0)
            signature = str(pow_data.get("signature", ""))
            target_path = str(
                pow_data.get("target_path", "") or "/api/v0/chat/completion"
            )

            if not challenge:
                logger.warning("[%s] PoW challenge 为空", self.type_name)
                return None

            logger.info(
                "[%s] PoW: algorithm=%s difficulty=%s salt=%s challenge=%s...",
                self.type_name,
                algorithm,
                difficulty,
                salt[:16],
                challenge[:16],
            )

            result = await self._solve_pow_in_browser(
                page, challenge, salt, difficulty, expire_at
            )
            if isinstance(result, dict) and "error" in result:
                logger.warning("[%s] PoW 求解失败: %s", self.type_name, result["error"])
                return None
            answer = result.get("answer", 0) if isinstance(result, dict) else 0

            pow_obj = {
                "algorithm": algorithm,
                "challenge": challenge,
                "salt": salt,
                "answer": answer,
                "signature": signature,
                "target_path": target_path,
            }
            pow_json = json.dumps(pow_obj, separators=(",", ":"))
            pow_b64 = base64.b64encode(pow_json.encode()).decode()

            logger.info(
                "[%s] PoW solved: answer=%s b64_len=%d",
                self.type_name,
                answer,
                len(pow_b64),
            )
            return pow_b64

        except Exception as e:
            logger.warning("[%s] _get_pow_response 异常: %s", self.type_name, e)
            return None

    async def _solve_pow_in_browser(
        self,
        page: Page,
        challenge: str,
        salt: str,
        difficulty: int,
        expire_at: Any,
    ) -> dict[str, Any]:
        try:
            result = await page.evaluate(
                _SOLVE_POW_JS,
                {
                    "challenge": challenge,
                    "salt": salt,
                    "difficulty": difficulty,
                    "expireAt": expire_at,
                },
            )
            if isinstance(result, dict):
                return result
            return {"error": f"unexpected result: {result}"}
        except Exception as e:
            logger.warning("[%s] PoW 浏览器求解失败: %s", self.type_name, e)
            return {"error": str(e)}

    def on_stream_completion_finished(
        self, session_id: str, message_ids: list[str]
    ) -> None:
        if message_ids and session_id in self._session_state:
            try:
                parent_mid = int(message_ids[-1])
            except (ValueError, TypeError):
                parent_mid = message_ids[-1]
            self._session_state[session_id]["parent_message_id"] = parent_mid
            logger.info("[%s] updated parent_message_id=%s", self.type_name, parent_mid)

    def on_http_error(self, message: str, headers: dict[str, str] | None) -> int | None:
        if "429" in message:
            return int(time.time()) + 3600
        return None


def register_deepseek_plugin() -> None:
    PluginRegistry.register(DeepSeekPlugin())
