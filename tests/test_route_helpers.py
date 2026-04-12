import unittest
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.chat.handler import ChatHandler
from core.http.route_helpers import (
    format_anthropic_stream_error,
    format_openai_stream_error,
    handle_chat_request,
)
from core.protocol.base import ProtocolAdapter
from core.shared.models import OpenAIChatRequest, OpenAIMessage
from core.stream.events import OpenAIStreamEvent


class _FakeRequest:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


class _FakeHandler:
    def __init__(self, events: list[OpenAIStreamEvent]) -> None:
        self._events = events
        self.calls: list[tuple[str, str]] = []

    async def stream_openai_events(
        self,
        provider: str,
        openai_req: Any,
    ) -> AsyncIterator[OpenAIStreamEvent]:
        self.calls.append((provider, openai_req.model))
        for event in self._events:
            yield event


class _FakeAdapter(ProtocolAdapter):
    protocol_name = "fake"

    def __init__(self, *, stream_raises: bool = False) -> None:
        self._stream_raises = stream_raises

    async def parse_request(
        self,
        raw_body: dict[str, Any],
    ) -> OpenAIChatRequest:
        return OpenAIChatRequest(
            model=str(raw_body.get("model") or "fake-model"),
            messages=[OpenAIMessage(role="user", content="test")],
            stream=bool(raw_body.get("stream") or False),
        )

    def render_non_stream(
        self,
        req: OpenAIChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]:
        text = "".join(event.content or "" for event in raw_events)
        return {"protocol": self.protocol_name, "provider": "demo", "text": text}

    async def render_stream(
        self,
        req: OpenAIChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]:
        async for event in raw_stream:
            if self._stream_raises:
                raise RuntimeError("stream failed")
            if event.content:
                yield f"chunk:{event.content}"

    def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
        return 500, {"error": str(exc)}


async def _collect_streaming_response(response: StreamingResponse) -> str:
    parts: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            parts.append(chunk)
        else:
            parts.append(bytes(chunk).decode())
    return "".join(parts)


class TestRouteHelpers(unittest.IsolatedAsyncioTestCase):
    async def test_handle_chat_request_non_stream(self) -> None:
        adapter = _FakeAdapter()
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_chat_request(
            adapter=adapter,
            provider="demo",
            request=cast(Request, _FakeRequest({"model": "m1", "stream": False})),
            handler=cast(ChatHandler, handler),
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertEqual(
            response,
            {"protocol": "fake", "provider": "demo", "text": "hello world"},
        )
        self.assertEqual(handler.calls, [("demo", "m1")])

    async def test_handle_chat_request_openai_stream_error(self) -> None:
        adapter = _FakeAdapter(stream_raises=True)
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_chat_request(
            adapter=adapter,
            provider="demo",
            request=cast(Request, _FakeRequest({"model": "m1", "stream": True})),
            handler=cast(ChatHandler, handler),
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertIsInstance(response, StreamingResponse)
        body = await _collect_streaming_response(response)
        self.assertEqual(body, 'data: {"error": "stream failed"}\n\n')

    async def test_handle_chat_request_anthropic_stream_error(self) -> None:
        adapter = _FakeAdapter(stream_raises=True)
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_chat_request(
            adapter=adapter,
            provider="demo",
            request=cast(Request, _FakeRequest({"model": "m1", "stream": True})),
            handler=cast(ChatHandler, handler),
            stream_error_formatter=format_anthropic_stream_error,
        )

        self.assertIsInstance(response, StreamingResponse)
        body = await _collect_streaming_response(response)
        self.assertEqual(body, 'event: error\ndata: {"error": "stream failed"}\n\n')

    async def test_parse_error_returns_json_response(self) -> None:
        class _ParseErrorAdapter(_FakeAdapter):
            async def parse_request(
                self,
                raw_body: dict[str, Any],
            ) -> OpenAIChatRequest:
                raise ValueError("bad request")

            def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
                return 400, {"error": str(exc)}

        response = await handle_chat_request(
            adapter=_ParseErrorAdapter(),
            provider="demo",
            request=cast(Request, _FakeRequest({"stream": False})),
            handler=cast(ChatHandler, _FakeHandler([])),
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.body.decode(), '{"error":"bad request"}')


if __name__ == "__main__":
    unittest.main()
