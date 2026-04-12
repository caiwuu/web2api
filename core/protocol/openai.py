"""OpenAI 协议适配器。"""

from __future__ import annotations

import json
import time
import uuid as uuid_mod
from collections.abc import AsyncIterator
from typing import Any

from core.shared.session_markers import (
    extract_session_id_marker,
    parse_conv_uuid_from_messages,
    strip_session_id_suffix,
)
from core.shared.tool_calls import build_tool_calls_response
from core.shared.models import OpenAIChatRequest, OpenAIMessage
from core.shared.tagged_output import format_openai_tagged_answer, parse_tagged_output
from core.shared.tagged_stream_parser import TaggedStreamEvent, TaggedStreamParser
from core.protocol.base import ProtocolAdapter
from core.stream.events import OpenAIStreamEvent


class OpenAIProtocolAdapter(ProtocolAdapter):
    protocol_name = "openai"

    async def parse_request(
        self,
        raw_body: dict[str, Any],
    ) -> OpenAIChatRequest:
        req = OpenAIChatRequest.model_validate(raw_body)
        resume_session_id = parse_conv_uuid_from_messages(
            [self._message_to_raw_dict(m) for m in req.messages]
        )
        req.resume_session_id = resume_session_id
        return req

    def render_non_stream(
        self,
        req: OpenAIChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]:
        reply = "".join(
            ev.content or ""
            for ev in raw_events
            if ev.type == "content_delta" and ev.content
        )
        session_marker = extract_session_id_marker(reply)
        content_for_parse = strip_session_id_suffix(reply)
        chat_id = f"chatcmpl-{uuid_mod.uuid4().hex[:24]}"
        created = int(time.time())
        if req.tools:
            parsed = parse_tagged_output(content_for_parse)
            if parsed.is_tool_call:
                tool_calls_list = [
                    {
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    }
                    for tool_call in parsed.tool_calls
                ]
                text_content = self._thinking_text_for_openai(
                    parsed.thinking, session_marker
                )
                return build_tool_calls_response(
                    tool_calls_list,
                    chat_id,
                    req.model,
                    created,
                    text_content=text_content,
                )
            content_reply = format_openai_tagged_answer(parsed)
            if session_marker:
                content_reply += session_marker
        else:
            content_reply = reply
        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": created,
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content_reply},
                    "finish_reason": "stop",
                }
            ],
        }

    async def render_stream(
        self,
        req: OpenAIChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]:
        chat_id = f"chatcmpl-{uuid_mod.uuid4().hex[:24]}"
        created = int(time.time())
        if not req.tools:
            session_marker = ""
            async for event in raw_stream:
                if event.type == "content_delta" and event.content:
                    chunk = event.content
                    if extract_session_id_marker(chunk) and not strip_session_id_suffix(
                        chunk
                    ):
                        session_marker = chunk
                        continue
                    yield self._content_delta(chat_id, req.model, created, chunk)
                elif event.type == "finish":
                    break
            if session_marker:
                yield self._content_delta(chat_id, req.model, created, session_marker)
            yield self._finish_delta(chat_id, req.model, created, "stop")
            yield "data: [DONE]\n\n"
            return

        parser = TaggedStreamParser()
        session_marker = ""
        async for event in raw_stream:
            if event.type == "content_delta" and event.content:
                chunk = event.content
                if extract_session_id_marker(chunk) and not strip_session_id_suffix(
                    chunk
                ):
                    session_marker = chunk
                    continue
                for tagged_event in parser.feed(chunk):
                    if tagged_event.type == "message_stop" and session_marker:
                        yield self._content_delta(
                            chat_id, req.model, created, session_marker
                        )
                        session_marker = ""
                    for sse in self._render_tagged_stream_event(
                        chat_id, req.model, created, tagged_event
                    ):
                        yield sse
            elif event.type == "finish":
                break
        for tagged_event in parser.finish():
            if tagged_event.type == "message_stop" and session_marker:
                yield self._content_delta(chat_id, req.model, created, session_marker)
                session_marker = ""
            for sse in self._render_tagged_stream_event(
                chat_id, req.model, created, tagged_event
            ):
                yield sse

    def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
        status = 400 if isinstance(exc, ValueError) else 500
        err_type = "invalid_request_error" if status == 400 else "server_error"
        return (
            status,
            {"error": {"message": str(exc), "type": err_type}},
        )

    @staticmethod
    def _message_to_raw_dict(msg: OpenAIMessage) -> dict[str, Any]:
        if isinstance(msg.content, list):
            content: str | list[dict[str, Any]] = [p.model_dump() for p in msg.content]
        elif isinstance(msg.content, str):
            content = msg.content
        else:
            content = ""
        out: dict[str, Any] = {"role": msg.role, "content": content}
        if msg.tool_calls is not None:
            out["tool_calls"] = msg.tool_calls
        if msg.tool_call_id is not None:
            out["tool_call_id"] = msg.tool_call_id
        return out

    @staticmethod
    def _content_delta(chat_id: str, model: str, created: int, text: str) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text},
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    @staticmethod
    def _assistant_start(chat_id: str, model: str, created: int) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    @staticmethod
    def _tool_calls_delta(
        chat_id: str,
        model: str,
        created: int,
        tool_calls: list[dict[str, Any]],
    ) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": tool_calls},
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    @staticmethod
    def _finish_delta(chat_id: str, model: str, created: int, reason: str) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "logprobs": None,
                            "finish_reason": reason,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    @staticmethod
    def _thinking_text_for_openai(
        thinking: str | None,
        session_marker: str = "",
    ) -> str:
        parts: list[str] = []
        if thinking:
            parts.append(f"<think>{thinking}</think>")
        if session_marker:
            parts.append(session_marker)
        return "\n".join(part for part in parts if part)

    def _render_tagged_stream_event(
        self,
        chat_id: str,
        model: str,
        created: int,
        event: TaggedStreamEvent,
    ) -> list[str]:
        if event.type == "message_start":
            return [self._assistant_start(chat_id, model, created)]
        if event.type == "block_start":
            if event.block_type == "thinking":
                return [self._content_delta(chat_id, model, created, "<think>")]
            return []
        if event.type == "block_delta":
            if event.text:
                return [self._content_delta(chat_id, model, created, event.text)]
            return []
        if event.type == "block_end":
            if event.block_type == "thinking":
                return [self._content_delta(chat_id, model, created, "</think>")]
            return []
        if event.type == "tool_call":
            call_index = event.call_index or 0
            tool_call_id = f"call_{uuid_mod.uuid4().hex[:24]}"
            out = [
                self._tool_calls_delta(
                    chat_id,
                    model,
                    created,
                    [
                        {
                            "index": call_index,
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": event.name or "",
                                "arguments": "",
                            },
                        }
                    ],
                )
            ]
            args = json.dumps(event.arguments or {}, ensure_ascii=False)
            if args:
                out.append(
                    self._tool_calls_delta(
                        chat_id,
                        model,
                        created,
                        [
                            {
                                "index": call_index,
                                "function": {"arguments": args},
                            }
                        ],
                    )
                )
            return out
        if event.type == "message_stop":
            reason = "tool_calls" if event.stop_reason == "tool_use" else "stop"
            return [
                self._finish_delta(chat_id, model, created, reason),
                "data: [DONE]\n\n",
            ]
        if event.type == "error":
            raise ValueError(event.error or "tagged stream parser error")
        return []
