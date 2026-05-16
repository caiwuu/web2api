"""Shared helpers for protocol-specific chat routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.api.auth import require_api_key
from core.api.chat_handler import ChatHandler
from core.protocol.base import ProtocolAdapter
from core.protocol.service import CanonicalChatService

StreamErrorFormatter = Callable[[dict[str, Any]], str]

STREAMING_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def create_protocol_router() -> APIRouter:
    """Create a protocol router with API-key auth applied."""
    return APIRouter(dependencies=[Depends(require_api_key)])


def format_openai_stream_error(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_anthropic_stream_error(payload: dict[str, Any]) -> str:
    return "event: error\n" f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def handle_protocol_chat_request(
    *,
    adapter: ProtocolAdapter,
    provider: str,
    request: Request,
    handler: ChatHandler,
    stream_error_formatter: StreamErrorFormatter,
) -> Any:
    raw_body = await request.json()
    try:
        canonical_req = adapter.parse_request(provider, raw_body)
    except Exception as exc:
        status, payload = adapter.render_error(exc)
        return JSONResponse(status_code=status, content=payload)

    service = CanonicalChatService(handler)
    if canonical_req.stream:
        stream_iter = adapter.render_stream(
            canonical_req,
            service.stream_raw(canonical_req),
        )
        try:
            first_event = await stream_iter.__anext__()
        except StopAsyncIteration:
            first_event = None
        except Exception as exc:
            status, payload = adapter.render_error(exc)
            return JSONResponse(status_code=status, content=payload)

        async def sse_stream() -> AsyncIterator[str]:
            if first_event is not None:
                yield first_event
            try:
                async for event in stream_iter:
                    yield event
            except Exception as exc:
                status, payload = adapter.render_error(exc)
                del status
                yield stream_error_formatter(payload)

        return StreamingResponse(
            sse_stream(),
            media_type="text/event-stream",
            headers=STREAMING_HEADERS,
        )

    try:
        raw_events = await service.collect_raw(canonical_req)
        return adapter.render_non_stream(canonical_req, raw_events)
    except Exception as exc:
        status, payload = adapter.render_error(exc)
        return JSONResponse(status_code=status, content=payload)
