"""
插件通用能力：页面复用、Cookie 登录、在浏览器内发起 fetch 并流式回传。
接入方只需实现站点特有的 URL/请求体/SSE 解析，其余复用此处逻辑。
"""

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from typing import Any, AsyncIterator

from core.config.settings import get_float

from playwright.async_api import BrowserContext, Page

from core.plugin.errors import AccountFrozenError

ParseSseEvent = Callable[[str], tuple[list[str], str | None, str | None]]

logger = logging.getLogger(__name__)

# 在页面内 POST 请求并流式回传：成功时逐块发送响应体，失败时发送 __error__: 前缀 + 信息，最后发送 __done__
# bindingName 按请求唯一，同一 page 多并发时互不串数据
PAGE_FETCH_STREAM_JS = """
async ({ url, body, bindingName }) => {
  const send = globalThis[bindingName];
  const done = "__done__";
  const errPrefix = "__error__:";
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 90000);
    const resp = await fetch(url, {
      method: "POST",
      body: body,
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
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
    const msg = e.name === "AbortError" ? "请求超时(90s)" : (e.message || String(e));
    await send(errPrefix + msg);
  }
  await send(done);
}
"""


PAGE_FETCH_JSON_JS = """
async ({ url, method, body, headers, timeoutMs }) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs || 15000);
  try {
    const resp = await fetch(url, {
      method: method || "GET",
      body: body ?? undefined,
      headers: headers || {},
      credentials: "include",
      signal: ctrl.signal
    });
    clearTimeout(t);
    const text = await resp.text();
    const headersObj = {};
    resp.headers.forEach((v, k) => { headersObj[k] = v; });
    return {
      ok: resp.ok,
      status: resp.status,
      statusText: resp.statusText,
      url: resp.url,
      redirected: resp.redirected,
      headers: headersObj,
      text,
    };
  } catch (e) {
    clearTimeout(t);
    const msg = e.name === "AbortError" ? `请求超时(${Math.floor((timeoutMs || 15000) / 1000)}s)` : (e.message || String(e));
    return { error: msg };
  }
}
"""


PAGE_FETCH_MULTIPART_JS = """
async ({ url, filename, mimeType, dataBase64, fieldName, extraFields, timeoutMs }) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs || 30000);
  try {
    const binary = atob(dataBase64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    const form = new FormData();
    if (extraFields) {
      Object.entries(extraFields).forEach(([k, v]) => {
        if (v !== undefined && v !== null) form.append(k, String(v));
      });
    }
    const file = new File([bytes], filename, { type: mimeType || "application/octet-stream" });
    form.append(fieldName || "file", file);
    const resp = await fetch(url, {
      method: "POST",
      body: form,
      credentials: "include",
      signal: ctrl.signal
    });
    clearTimeout(t);
    const text = await resp.text();
    const headersObj = {};
    resp.headers.forEach((v, k) => { headersObj[k] = v; });
    return {
      ok: resp.ok,
      status: resp.status,
      statusText: resp.statusText,
      url: resp.url,
      redirected: resp.redirected,
      headers: headersObj,
      text,
    };
  } catch (e) {
    clearTimeout(t);
    const msg = e.name === "AbortError" ? `请求超时(${Math.floor((timeoutMs || 30000) / 1000)}s)` : (e.message || String(e));
    return { error: msg };
  }
}
"""


async def ensure_page_for_site(
    context: BrowserContext,
    url_contains: str,
    start_url: str,
    *,
    timeout: int = 20000,
) -> Page:
    """
    若已有页面 URL 包含 url_contains 则复用，否则 new_page 并 goto start_url。
    接入方只需提供「站点特征」和「入口 URL」。
    """
    if context.pages:
        for p in context.pages:
            if url_contains in (p.url or ""):
                return p
    page = await context.new_page()
    await page.goto(start_url, wait_until="domcontentloaded", timeout=timeout)
    return page


async def create_page_for_site(
    context: BrowserContext,
    start_url: str,
    *,
    reuse_page: Page | None = None,
    timeout: int = 20000,
) -> Page:
    """
    若传入 reuse_page 则在其上 goto start_url，否则 new_page 再 goto。
    用于复用浏览器默认空白页或 page 池的初始化与补回。
    """
    if reuse_page is not None:
        await reuse_page.goto(start_url, wait_until="domcontentloaded", timeout=timeout)
        return reuse_page
    page = await context.new_page()
    await page.goto(start_url, wait_until="domcontentloaded", timeout=timeout)
    return page


def _cookie_domain_matches(cookie_domain: str, site_domain: str) -> bool:
    """判断 cookie 的 domain 是否属于站点 domain（如 .claude.ai 与 claude.ai 视为同一域）。"""
    a = cookie_domain if cookie_domain.startswith(".") else f".{cookie_domain}"
    b = site_domain if site_domain.startswith(".") else f".{site_domain}"
    return a == b


def _cookie_to_set_param(c: Any) -> dict[str, str]:
    """将 context.cookies() 返回的项转为 add_cookies 接受的 SetCookieParam 格式。"""
    return {
        "name": c["name"],
        "value": c["value"],
        "domain": c.get("domain") or "",
        "path": c.get("path") or "/",
    }


async def clear_cookies_for_domain(
    context: BrowserContext,
    site_domain: str,
) -> None:
    """清除 context 内属于指定站点域的所有 cookie，保留其他域。"""
    cookies = await context.cookies()
    keep = [
        c
        for c in cookies
        if not _cookie_domain_matches(c.get("domain", ""), site_domain)
    ]
    await context.clear_cookies()
    if keep:
        await context.add_cookies([_cookie_to_set_param(c) for c in keep])  # type: ignore[arg-type]
    logger.info(
        "[auth] cleared cookies for domain=%s (kept %s cookies)", site_domain, len(keep)
    )


async def clear_page_storage_for_switch(page: Page) -> None:
    """切号前清空当前页面的 localStorage（当前 origin）。"""
    try:
        await page.evaluate("() => { window.localStorage.clear(); }")
        logger.info("[auth] cleared localStorage for switch")
    except Exception as e:
        logger.warning("[auth] clear localStorage failed (page may be detached): %s", e)


async def safe_page_reload(page: Page, url: str | None = None) -> None:
    """安全地 reload 或 goto(url)，忽略因 ERR_ABORTED / frame detached 导致的异常。"""
    try:
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        else:
            await page.reload(wait_until="domcontentloaded")
    except Exception as e:
        err_msg = str(e)
        if "ERR_ABORTED" in err_msg or "detached" in err_msg.lower():
            logger.warning(
                "[auth] page.reload/goto 被中止或 frame 已分离: %s", err_msg[:200]
            )
        else:
            raise


async def apply_cookie_auth(
    context: BrowserContext,
    page: Page,
    auth: dict[str, Any],
    cookie_name: str,
    auth_keys: list[str],
    domain: str,
    *,
    path: str = "/",
    reload: bool = True,
) -> None:
    """
    从 auth 中按 auth_keys 顺序取第一个非空值作为 cookie 值，写入 context 并可选 reload。
    接入方只需提供 cookie 名、auth 里的 key 列表、域名。
    仅写 cookie 不 reload 时，同 context 内的 fetch() 仍会带上 cookie；reload 仅在需要页面文档同步登录态时用。
    """
    value = None
    for k in auth_keys:
        v = auth.get(k)
        if v is not None and v != "":
            value = str(v).strip()
            if value:
                break
    if not value:
        raise ValueError(f"auth 需包含以下其一且非空: {auth_keys}")

    logger.info(
        "[auth] context.add_cookies domain=%s name=%s reload=%s page.url=%s",
        domain,
        cookie_name,
        reload,
        page.url,
    )
    await context.add_cookies(
        [
            {
                "name": cookie_name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": True,
                "httpOnly": True,
            }
        ]
    )
    if reload:
        await safe_page_reload(page)


async def request_json_via_page_fetch(
    page: Page,
    url: str,
    *,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_ms: int = 15000,
) -> dict[str, Any]:
    """
    在页面内发起非流式 fetch，请求结果按 JSON 优先解析返回。
    这样能复用浏览器真实网络栈、cookie 与代理扩展能力。
    """
    logger.info(
        "[fetch] page request method=%s url=%s page.url=%s",
        method,
        url[:120] + "..." if len(url) > 120 else url,
        page.url or "",
    )
    result = await page.evaluate(
        PAGE_FETCH_JSON_JS,
        {
            "url": url,
            "method": method,
            "body": body,
            "headers": headers or {},
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


async def upload_file_via_page_fetch(
    page: Page,
    url: str,
    *,
    filename: str,
    mime_type: str,
    data: bytes,
    field_name: str = "file",
    extra_fields: dict[str, str] | None = None,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    logger.info(
        "[fetch] page upload filename=%s mime=%s url=%s page.url=%s",
        filename,
        mime_type,
        url[:120] + "..." if len(url) > 120 else url,
        page.url or "",
    )
    result = await page.evaluate(
        PAGE_FETCH_MULTIPART_JS,
        {
            "url": url,
            "filename": filename,
            "mimeType": mime_type,
            "dataBase64": base64.b64encode(data).decode("ascii"),
            "fieldName": field_name,
            "extraFields": extra_fields or {},
            "timeoutMs": timeout_ms,
        },
    )
    if not isinstance(result, dict):
        raise RuntimeError("页面上传返回结果异常")
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


async def stream_raw_via_page_fetch(
    context: BrowserContext,
    page: Page,
    url: str,
    body: str,
    request_id: str,
    *,
    on_http_error: Callable[[str, dict[str, str] | None], int | None] | None = None,
    on_headers: Callable[[dict[str, str]], None] | None = None,
    error_state: dict[str, bool] | None = None,
    fetch_timeout: int = 90,
    read_timeout: float = 15.0,
    first_chunk_timeout: float | None = None,
) -> AsyncIterator[str]:
    """
    在浏览器内对 url 发起 POST body，流式回传原始字符串块（含 SSE 等）。
    同一 page 多请求用 request_id 区分 binding，互不串数据。
    通过 CDP Runtime.addBinding 注入 sendChunk_<request_id>，用 Runtime.bindingCalled 接收。
    收到 __headers__: 时解析 JSON 并调用 on_headers(headers)；收到 __error__: 时调用 on_http_error(msg)；收到 __done__ 结束。
    """
    chunk_queue: asyncio.Queue[str] = asyncio.Queue()
    BINDING_NAME = "sendChunk_" + request_id
    started_at = asyncio.get_running_loop().time()
    if first_chunk_timeout is None:
        first_chunk_timeout = max(
            5.0,
            get_float("scheduler", "stream_first_chunk_timeout_seconds", 30.0),
        )

    def on_binding_called(event: dict[str, Any]) -> None:
        name = event.get("name")
        payload = event.get("payload", "")
        if name == BINDING_NAME:
            chunk_queue.put_nowait(
                payload if isinstance(payload, str) else str(payload)
            )

    cdp = None
    try:
        cdp = await context.new_cdp_session(page)
        cdp.on("Runtime.bindingCalled", on_binding_called)
        await cdp.send("Runtime.addBinding", {"name": BINDING_NAME})

        logger.info(
            "[fetch] evaluate fetch on pool page (no goto) url=%s page.url=%s",
            url[:80] + "..." if len(url) > 80 else url,
            page.url or "",
        )

        async def run_fetch() -> None:
            await page.evaluate(
                PAGE_FETCH_STREAM_JS,
                {"url": url, "body": body, "bindingName": BINDING_NAME},
            )

        fetch_task = asyncio.create_task(run_fetch())
        try:
            headers = None
            seen_first_chunk = False
            while True:
                try:
                    timeout = (max(1.0, get_float("scheduler", "stream_no_data_timeout_seconds", read_timeout)) if seen_first_chunk else first_chunk_timeout)
                    chunk = await asyncio.wait_for(chunk_queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if seen_first_chunk:
                        logger.warning("流式读取超时")
                        raise RuntimeError("流式读取超时")
                    logger.warning(
                        "流式首包超时 request_id=%s elapsed=%.2fs url=%s",
                        request_id,
                        asyncio.get_running_loop().time() - started_at,
                        url[:80] + "..." if len(url) > 80 else url,
                    )
                    raise RuntimeError("流式首包超时")
                if not seen_first_chunk:
                    logger.info(
                        "[fetch] first chunk received request_id=%s elapsed=%.2fs url=%s",
                        request_id,
                        asyncio.get_running_loop().time() - started_at,
                        url[:80] + "..." if len(url) > 80 else url,
                    )
                seen_first_chunk = True
                if chunk == "__done__":
                    logger.info(
                        "[fetch] stream done request_id=%s elapsed=%.2fs first_chunk=%s",
                        request_id,
                        asyncio.get_running_loop().time() - started_at,
                        seen_first_chunk,
                    )
                    break
                if chunk.startswith("__headers__:"):
                    try:
                        headers = json.loads(chunk[12:])
                        if on_headers and isinstance(headers, dict):
                            on_headers({k: str(v) for k, v in headers.items()})
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug("[fetch] 解析 __headers__ 失败: %s", e)
                    continue
                if chunk.startswith("__error__:"):
                    msg = chunk[10:].strip()
                    saw_terminal = bool(error_state and error_state.get("terminal"))
                    if on_http_error:
                        unfreeze_at = on_http_error(msg, headers)
                        if isinstance(unfreeze_at, int):
                            logger.warning("[fetch] __error__ from page: %s", msg)
                            raise AccountFrozenError(msg, unfreeze_at)
                    if saw_terminal:
                        logger.info(
                            "[fetch] page fetch disconnected after terminal event: %s",
                            msg,
                        )
                        continue
                    logger.warning(
                        "[fetch] __error__ from page before terminal event: %s", msg
                    )
                    raise RuntimeError(msg)
                    continue
                yield chunk
        finally:
            if fetch_task.done():
                try:
                    await fetch_task
                except asyncio.CancelledError:
                    pass
            else:
                fetch_task.cancel()

                def consume_fetch_task_result(task: asyncio.Task[None]) -> None:
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.debug("[fetch] background fetch task ended: %s", exc)

                fetch_task.add_done_callback(consume_fetch_task_result)
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception as e:
                logger.debug("detach CDP session 时异常: %s", e)


def parse_sse_to_events(buffer: str, chunk: str) -> tuple[str, list[str]]:
    """
    把 chunk 追加到 buffer，按行拆出 data: 后的 payload 列表，返回 (剩余 buffer, payload 列表)。
    接入方对每个 payload 自行 JSON 解析并抽取 text / message_id / error。
    """
    buffer += chunk
    lines = buffer.split("\n")
    buffer = lines[-1]
    payloads: list[str] = []
    for line in lines[:-1]:
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]" or not payload:
            continue
        payloads.append(payload)
    return (buffer, payloads)


async def stream_completion_via_sse(
    context: BrowserContext,
    page: Page,
    url: str,
    body: str,
    parse_event: ParseSseEvent,
    request_id: str,
    *,
    on_http_error: Callable,
    is_terminal_event: Callable[[str], bool] | None = None,
    collect_message_id: list[str] | None = None,
) -> AsyncIterator[str]:
    """
    在浏览器内 POST 拿到流，按 SSE 行拆成 data 事件，用 parse_event(payload) 解析每条；
    逐块 yield 文本，可选把 message_id 收集到 collect_message_id。
    parse_event(payload) 返回 (texts, message_id, error)，error 非空时仅打 debug 日志不抛错。
    """
    buffer = ""
    stream_state: dict[str, bool] = {"terminal": False}
    async for chunk in stream_raw_via_page_fetch(
        context,
        page,
        url,
        body,
        request_id,
        on_http_error=on_http_error,
        error_state=stream_state,
    ):
        buffer, payloads = parse_sse_to_events(buffer, chunk)
        for payload in payloads:
            if is_terminal_event and is_terminal_event(payload):
                stream_state["terminal"] = True
            try:
                texts, message_id, error = parse_event(payload)
            except Exception as e:
                logger.debug("parse_stream_event 单条解析异常: %s", e)
                continue
            if error:
                logger.debug("SSE error: %s", error)
                continue
            if message_id and collect_message_id is not None:
                collect_message_id.append(message_id)
            for t in texts:
                yield t
