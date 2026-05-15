"""OpenAI 协议适配器。"""

from __future__ import annotations

import json
import time
import uuid as uuid_mod
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from core.api.conv_parser import (
    extract_session_id_marker,
    parse_conv_uuid_from_messages,
    strip_session_id_suffix,
)
from core.api.function_call import build_tool_calls_response
from core.api.schemas import OpenAIChatRequest, OpenAIContentPart, OpenAIMessage
from core.api.tagged_output import format_openai_tagged_answer, parse_tagged_output
from core.api.tagged_stream_parser import TaggedStreamEvent, TaggedStreamParser
from core.hub.schemas import OpenAIStreamEvent
from core.protocol.base import ProtocolAdapter
from core.protocol.schemas import (
    CanonicalChatRequest,
    CanonicalContentBlock,
    CanonicalMessage,
    CanonicalToolSpec,
)


class OpenAIProtocolAdapter(ProtocolAdapter):
    protocol_name = "openai"

    def parse_request(
        self,
        provider: str,
        raw_body: dict[str, Any],
    ) -> CanonicalChatRequest:
        req = OpenAIChatRequest.model_validate(raw_body)
        resume_session_id = parse_conv_uuid_from_messages(
            [self._message_to_raw_dict(m) for m in req.messages]
        )
        system_blocks: list[CanonicalContentBlock] = []
        messages: list[CanonicalMessage] = []
        for msg in req.messages:
            blocks = self._message_to_blocks(msg)
            if msg.role == "system":
                system_blocks.extend(blocks)
            else:
                messages.append(
                    CanonicalMessage(
                        role=cast(
                            Literal["system", "user", "assistant", "tool"], msg.role
                        ),
                        content=blocks,
                    )
                )
        tools = [self._to_tool_spec(tool) for tool in list(req.tools or [])]
        return CanonicalChatRequest(
            protocol="openai",
            provider=provider,
            model=req.model,
            system=system_blocks,
            messages=messages,
            stream=req.stream,
            tools=tools,
            tool_choice=req.tool_choice,
            parallel_tool_calls=req.parallel_tool_calls,
            resume_session_id=resume_session_id,
            metadata={
                "web2api_account": str(raw_body.get("web2api_account") or "").strip()
            }
            if raw_body.get("web2api_account")
            else {},
        )

    def render_non_stream(
        self,
        req: CanonicalChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]:
        reply = "".join(
            ev.content or ""
            for ev in raw_events
            if ev.type == "content_delta" and ev.content
        )
        session_marker = extract_session_id_marker(reply)
        content_for_parse = strip_session_id_suffix(reply)
        chat_id, created = self._response_context(req)
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
        req: CanonicalChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]:
        chat_id, created = self._response_context(req)
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
    def _to_blocks(
        content: str | list[OpenAIContentPart] | None,
    ) -> list[CanonicalContentBlock]:
        if content is None:
            return []
        if isinstance(content, str):
            return [
                CanonicalContentBlock(
                    type="text", text=strip_session_id_suffix(content)
                )
            ]
        blocks: list[CanonicalContentBlock] = []
        for part in content:
            if part.type == "text":
                blocks.append(
                    CanonicalContentBlock(
                        type="text",
                        text=strip_session_id_suffix(part.text or ""),
                    )
                )
            elif part.type == "image_url":
                image_url = part.image_url
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if not url:
                    continue
                if isinstance(url, str) and url.startswith("data:"):
                    blocks.append(CanonicalContentBlock(type="image", data=url))
                else:
                    blocks.append(CanonicalContentBlock(type="image", url=str(url)))
        return blocks

    @classmethod
    def _message_to_blocks(cls, msg: OpenAIMessage) -> list[CanonicalContentBlock]:
        if msg.role == "tool":
            return cls._tool_message_to_blocks(msg)

        blocks = cls._to_blocks(msg.content)
        if msg.role == "assistant" and msg.tool_calls:
            blocks.extend(cls._tool_calls_to_blocks(msg.tool_calls))
        return blocks

    @classmethod
    def _tool_message_to_blocks(cls, msg: OpenAIMessage) -> list[CanonicalContentBlock]:
        text_parts = [
            block.text or ""
            for block in cls._to_blocks(msg.content)
            if block.type == "text"
        ]
        return [
            CanonicalContentBlock(
                type="tool_result",
                tool_use_id=msg.tool_call_id or "",
                text="\n".join(part for part in text_parts if part),
            )
        ]

    @staticmethod
    def _tool_calls_to_blocks(
        tool_calls: list[dict[str, Any]],
    ) -> list[CanonicalContentBlock]:
        blocks: list[CanonicalContentBlock] = []
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            if not isinstance(function, dict):
                continue
            raw_args = function.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    arguments = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    arguments = {}
            elif isinstance(raw_args, dict):
                arguments = raw_args
            else:
                arguments = {}
            blocks.append(
                CanonicalContentBlock(
                    type="tool_use",
                    id=str(tool_call.get("id") or ""),
                    name=str(function.get("name") or ""),
                    input=arguments,
                )
            )
        return blocks

    @staticmethod
    def _to_tool_spec(tool: dict[str, Any]) -> CanonicalToolSpec:
        function_obj = tool.get("function") if tool.get("type") == "function" else tool
        function: dict[str, Any] = (
            function_obj if isinstance(function_obj, dict) else {}
        )
        return CanonicalToolSpec(
            name=str(function.get("name") or ""),
            description=str(function.get("description") or ""),
            input_schema=(
                function.get("parameters") or function.get("input_schema") or {}
            ),
            strict=bool(function.get("strict") or False),
        )

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

    @staticmethod
    def _response_context(req: CanonicalChatRequest) -> tuple[str, int]:
        chat_id = str(
            req.metadata.setdefault(
                "response_id", f"chatcmpl-{uuid_mod.uuid4().hex[:24]}"
            )
        )
        created = int(req.metadata.setdefault("created", int(time.time())))
        return chat_id, created
