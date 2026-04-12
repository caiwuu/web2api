"""Shared helpers for authenticated HTTP chat routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.admin.auth import require_api_key
from core.chat.handler import ChatHandler
from core.protocol.base import ProtocolAdapter

StreamErrorFormatter = Callable[[dict[str, Any]], str]

STREAMING_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def create_authenticated_router() -> APIRouter:
    """Create an API router with API-key auth applied."""
    return APIRouter(dependencies=[Depends(require_api_key)])


def format_openai_stream_error(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_anthropic_stream_error(payload: dict[str, Any]) -> str:
    return f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def handle_chat_request(
    *,
    adapter: ProtocolAdapter,
    provider: str,
    request: Request,
    handler: ChatHandler,
    stream_error_formatter: StreamErrorFormatter,
) -> Any:
    raw_body = await request.json()
    try:
        openai_req = await adapter.parse_request(raw_body)
    except Exception as exc:
        status, payload = adapter.render_error(exc)
        return JSONResponse(status_code=status, content=payload)

    if openai_req.stream:

        async def sse_stream() -> AsyncIterator[str]:
            try:
                async for event in adapter.render_stream(
                    openai_req,
                    handler.stream_openai_events(provider, openai_req),
                ):
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
        events: list = []
        async for ev in handler.stream_openai_events(provider, openai_req):
            events.append(ev)
        return adapter.render_non_stream(openai_req, events)
    except Exception as exc:
        status, payload = adapter.render_error(exc)
        return JSONResponse(status_code=status, content=payload)
