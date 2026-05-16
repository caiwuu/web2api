import unittest
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from core.api.protocol_routes import (
    format_anthropic_stream_error,
    format_openai_stream_error,
    handle_protocol_chat_request,
)
from core.hub.schemas import OpenAIStreamEvent
from core.protocol.base import ProtocolAdapter
from core.protocol.schemas import CanonicalChatRequest


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

    def __init__(
        self,
        *,
        stream_raises: bool = False,
        stream_raises_after_first_chunk: bool = False,
    ) -> None:
        self._stream_raises = stream_raises
        self._stream_raises_after_first_chunk = stream_raises_after_first_chunk

    def parse_request(
        self,
        provider: str,
        raw_body: dict[str, Any],
    ) -> CanonicalChatRequest:
        return CanonicalChatRequest(
            protocol="openai",
            provider=provider,
            model=str(raw_body.get("model") or "fake-model"),
            stream=bool(raw_body.get("stream") or False),
        )

    def render_non_stream(
        self,
        req: CanonicalChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]:
        text = "".join(event.content or "" for event in raw_events)
        return {"protocol": self.protocol_name, "provider": req.provider, "text": text}

    async def render_stream(
        self,
        req: CanonicalChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]:
        yielded = False
        async for event in raw_stream:
            if self._stream_raises:
                raise RuntimeError("stream failed")
            if event.content:
                yield f"chunk:{event.content}"
                yielded = True
                if self._stream_raises_after_first_chunk and yielded:
                    raise RuntimeError("stream failed")

    def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
        return 500, {"error": str(exc)}


async def _collect_streaming_response(response: StreamingResponse) -> str:
    parts: list[str] = []
    async for chunk in response.body_iterator:
        parts.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(parts)


class TestProtocolRouteHelper(unittest.IsolatedAsyncioTestCase):
    async def test_handle_protocol_chat_request_non_stream(self) -> None:
        adapter = _FakeAdapter()
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_protocol_chat_request(
            adapter=adapter,
            provider="demo",
            request=_FakeRequest({"model": "m1", "stream": False}),
            handler=handler,
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertEqual(
            response,
            {"protocol": "fake", "provider": "demo", "text": "hello world"},
        )
        self.assertEqual(handler.calls, [("demo", "m1")])

    async def test_handle_protocol_chat_request_openai_stream_error(self) -> None:
        adapter = _FakeAdapter(stream_raises_after_first_chunk=True)
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_protocol_chat_request(
            adapter=adapter,
            provider="demo",
            request=_FakeRequest({"model": "m1", "stream": True}),
            handler=handler,
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertIsInstance(response, StreamingResponse)
        body = await _collect_streaming_response(response)
        self.assertEqual(body, 'chunk:hello worlddata: {"error": "stream failed"}\n\n')

    async def test_handle_protocol_chat_request_anthropic_stream_error(self) -> None:
        adapter = _FakeAdapter(stream_raises_after_first_chunk=True)
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_protocol_chat_request(
            adapter=adapter,
            provider="demo",
            request=_FakeRequest({"model": "m1", "stream": True}),
            handler=handler,
            stream_error_formatter=format_anthropic_stream_error,
        )

        self.assertIsInstance(response, StreamingResponse)
        body = await _collect_streaming_response(response)
        self.assertEqual(
            body,
            'chunk:hello worldevent: error\ndata: {"error": "stream failed"}\n\n',
        )

    async def test_handle_protocol_chat_request_stream_error_before_first_chunk_returns_json_response(
        self,
    ) -> None:
        adapter = _FakeAdapter(stream_raises=True)
        handler = _FakeHandler(
            [OpenAIStreamEvent(type="content_delta", content="hello world")]
        )

        response = await handle_protocol_chat_request(
            adapter=adapter,
            provider="demo",
            request=_FakeRequest({"model": "m1", "stream": True}),
            handler=handler,
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 500)

    async def test_parse_error_returns_json_response(self) -> None:
        class _ParseErrorAdapter(_FakeAdapter):
            def parse_request(
                self,
                provider: str,
                raw_body: dict[str, Any],
            ) -> CanonicalChatRequest:
                raise ValueError("bad request")

            def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
                return 400, {"error": str(exc)}

        response = await handle_protocol_chat_request(
            adapter=_ParseErrorAdapter(),
            provider="demo",
            request=_FakeRequest({"stream": False}),
            handler=_FakeHandler([]),
            stream_error_formatter=format_openai_stream_error,
        )

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.body.decode(), '{"error":"bad request"}')


if __name__ == "__main__":
    unittest.main()
