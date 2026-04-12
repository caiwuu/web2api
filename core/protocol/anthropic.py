"""Anthropic 协议适配器。"""

from __future__ import annotations

import json
import uuid as uuid_mod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from core.shared.session_markers import (
    decode_latest_session_id,
    extract_session_id_marker,
    strip_session_id_suffix,
)
from core.shared.models import (
    InputAttachment,
    OpenAIChatRequest,
    OpenAIContentPart,
    OpenAIMessage,
)
from core.shared.tagged_output import parse_tagged_output
from core.shared.tagged_stream_parser import TaggedStreamEvent, TaggedStreamParser
from core.protocol.base import ProtocolAdapter
from core.protocol.images import (
    download_remote_image,
    parse_base64_image,
    parse_data_url,
)
from core.stream.events import OpenAIStreamEvent


@dataclass
class _ContentBlock:
    """Anthropic content block 的内部解析中间类型。"""

    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool | None = None
    mime_type: str | None = None
    data: str | None = None
    url: str | None = None


class AnthropicProtocolAdapter(ProtocolAdapter):
    protocol_name = "anthropic"

    async def parse_request(
        self,
        raw_body: dict[str, Any],
    ) -> OpenAIChatRequest:
        messages_raw = raw_body.get("messages") or []
        if not isinstance(messages_raw, list):
            raise ValueError("messages 必须为数组")

        system_blocks = self._parse_content(raw_body.get("system"))
        resume_session_id: str | None = None

        for block in system_blocks:
            text = block.text or ""
            decoded = decode_latest_session_id(text)
            if decoded:
                resume_session_id = decoded
                block.text = strip_session_id_suffix(text)

        parsed_msgs: list[tuple[str, list[_ContentBlock]]] = []
        for item in messages_raw:
            if not isinstance(item, dict):
                continue
            blocks = self._parse_content(item.get("content"))
            for block in blocks:
                text = block.text or ""
                decoded = decode_latest_session_id(text)
                if decoded:
                    resume_session_id = decoded
                    block.text = strip_session_id_suffix(text)
            role = self._resolve_role(str(item.get("role") or "user"), blocks)
            parsed_msgs.append((role, blocks))

        openai_messages: list[OpenAIMessage] = []

        if system_blocks:
            openai_messages.append(
                OpenAIMessage(
                    role="system",
                    content=self._blocks_to_openai_content(system_blocks),
                )
            )

        for role, blocks in parsed_msgs:
            openai_messages.append(self._blocks_to_openai_message(role, blocks))

        openai_tools: list[dict] | None = None
        raw_tools = raw_body.get("tools")
        if raw_tools:
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": str(tool.get("name") or ""),
                        "description": str(tool.get("description") or ""),
                        "parameters": tool.get("input_schema") or {},
                    },
                }
                for tool in raw_tools
                if isinstance(tool, dict)
            ]

        last_user_attachments, all_attachments = await self._resolve_attachments(
            parsed_msgs
        )

        return OpenAIChatRequest(
            model=str(raw_body.get("model") or ""),
            messages=openai_messages,
            stream=bool(raw_body.get("stream") or False),
            tools=openai_tools,
            tool_choice=raw_body.get("tool_choice"),
            parallel_tool_calls=(
                raw_body.get("parallel_tool_calls")
                if isinstance(raw_body.get("parallel_tool_calls"), bool)
                else None
            ),
            resume_session_id=resume_session_id,
            attachment_files=[],
            attachment_files_last_user=last_user_attachments,
            attachment_files_all_users=all_attachments,
        )

    def render_non_stream(
        self,
        req: OpenAIChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]:
        full = "".join(
            ev.content or ""
            for ev in raw_events
            if ev.type == "content_delta" and ev.content
        )
        session_marker = extract_session_id_marker(full)
        text = strip_session_id_suffix(full)
        message_id = f"msg_{uuid_mod.uuid4().hex}"
        if req.tools:
            parsed = parse_tagged_output(text)
            content: list[dict[str, Any]] = []
            if parsed.thinking:
                content.append({"type": "thinking", "thinking": parsed.thinking})
            if parsed.is_tool_call:
                for tool_call in parsed.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": f"toolu_{uuid_mod.uuid4().hex[:24]}",
                            "name": tool_call.name,
                            "input": tool_call.arguments,
                        }
                    )
                if session_marker:
                    content.append({"type": "text", "text": session_marker})
                return self._message_response(
                    req, message_id, content, stop_reason="tool_use"
                )
            content.append(
                {"type": "text", "text": (parsed.final_answer or "") + session_marker}
            )
            return self._message_response(
                req, message_id, content, stop_reason="end_turn"
            )
        rendered = text
        if session_marker:
            rendered += session_marker
        return self._message_response(
            req,
            message_id,
            [{"type": "text", "text": rendered}],
            stop_reason="end_turn",
        )

    async def render_stream(
        self,
        req: OpenAIChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]:
        message_id = f"msg_{uuid_mod.uuid4().hex}"
        if not req.tools:
            renderer = _AnthropicTaggedRenderer(req, message_id)
            text_block_open = False
            session_marker = ""
            async for event in raw_stream:
                if event.type == "content_delta" and event.content:
                    chunk = event.content
                    if extract_session_id_marker(chunk) and not strip_session_id_suffix(
                        chunk
                    ):
                        session_marker = chunk
                        continue
                    if not text_block_open:
                        for out in renderer.start_block("text"):
                            yield out
                        text_block_open = True
                    for out in renderer.block_delta("text", chunk):
                        yield out
                elif event.type == "finish":
                    break
            if session_marker:
                if not text_block_open:
                    for out in renderer.start_block("text"):
                        yield out
                    text_block_open = True
                for out in renderer.block_delta("text", session_marker):
                    yield out
            if text_block_open:
                for out in renderer.end_block():
                    yield out
            for out in renderer.message_stop("end_turn"):
                yield out
            return

        parser = TaggedStreamParser()
        session_marker = ""
        renderer = _AnthropicTaggedRenderer(req, message_id)
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
                        for out in renderer.marker_text_block(session_marker):
                            yield out
                        session_marker = ""
                    for out in renderer.render(tagged_event):
                        yield out
            elif event.type == "finish":
                break
        for tagged_event in parser.finish():
            if tagged_event.type == "message_stop" and session_marker:
                for out in renderer.marker_text_block(session_marker):
                    yield out
                session_marker = ""
            for out in renderer.render(tagged_event):
                yield out

    def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]:
        status = 400 if isinstance(exc, ValueError) else 500
        err_type = "invalid_request_error" if status == 400 else "api_error"
        return (
            status,
            {
                "type": "error",
                "error": {"type": err_type, "message": str(exc)},
            },
        )

    # ── internal helpers ──

    @staticmethod
    def _resolve_role(
        raw_role: str,
        blocks: list[_ContentBlock],
    ) -> str:
        if raw_role == "user" and any(b.type == "tool_result" for b in blocks):
            return "tool"
        return raw_role

    @staticmethod
    def _parse_content(value: Any) -> list[_ContentBlock]:
        if value is None:
            return []
        if isinstance(value, str):
            return [_ContentBlock(type="text", text=value)]
        if isinstance(value, list):
            blocks: list[_ContentBlock] = []
            for item in value:
                if isinstance(item, str):
                    blocks.append(_ContentBlock(type="text", text=item))
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type == "text":
                    blocks.append(
                        _ContentBlock(type="text", text=str(item.get("text") or ""))
                    )
                elif item_type == "thinking":
                    blocks.append(
                        _ContentBlock(
                            type="thinking", text=str(item.get("thinking") or "")
                        )
                    )
                elif item_type == "image":
                    source = item.get("source") or {}
                    source_type = source.get("type")
                    if source_type == "base64":
                        blocks.append(
                            _ContentBlock(
                                type="image",
                                mime_type=str(source.get("media_type") or ""),
                                data=str(source.get("data") or ""),
                            )
                        )
                elif item_type == "tool_use":
                    blocks.append(
                        _ContentBlock(
                            type="tool_use",
                            id=str(item.get("id") or ""),
                            name=str(item.get("name") or ""),
                            input=(
                                item.get("input")
                                if isinstance(item.get("input"), dict)
                                else {}
                            ),
                        )
                    )
                elif item_type == "tool_result":
                    text_parts = AnthropicProtocolAdapter._parse_content(
                        item.get("content")
                    )
                    blocks.append(
                        _ContentBlock(
                            type="tool_result",
                            tool_use_id=str(item.get("tool_use_id") or ""),
                            text="\n".join(
                                part.text or ""
                                for part in text_parts
                                if part.type == "text"
                            ),
                            is_error=bool(item.get("is_error") or False),
                        )
                    )
            return blocks
        raise ValueError("content 格式不合法")

    @staticmethod
    def _blocks_to_openai_content(
        blocks: list[_ContentBlock],
    ) -> str | list[OpenAIContentPart]:
        if not blocks:
            return ""
        parts: list[OpenAIContentPart] = []
        for block in blocks:
            if block.type in {"text", "thinking", "tool_result"}:
                parts.append(OpenAIContentPart(type="text", text=block.text or ""))
            elif block.type == "image":
                url = block.url or block.data or ""
                parts.append(
                    OpenAIContentPart(type="image_url", image_url={"url": url})
                )
        if not parts:
            return ""
        if len(parts) == 1 and parts[0].type == "text":
            return parts[0].text or ""
        return parts

    @classmethod
    def _blocks_to_openai_message(
        cls, role: str, blocks: list[_ContentBlock]
    ) -> OpenAIMessage:
        if role == "assistant":
            text_blocks = [b for b in blocks if b.type != "tool_use"]
            tool_use_blocks = [b for b in blocks if b.type == "tool_use"]
            tool_calls = [
                {
                    "id": b.id or "",
                    "type": "function",
                    "function": {
                        "name": b.name or "",
                        "arguments": json.dumps(b.input or {}, ensure_ascii=False),
                    },
                }
                for b in tool_use_blocks
            ]
            return OpenAIMessage(
                role=role,
                content=cls._blocks_to_openai_content(text_blocks),
                tool_calls=tool_calls or None,
            )

        if role == "tool":
            tool_call_id = next(
                (
                    b.tool_use_id
                    for b in blocks
                    if b.type == "tool_result" and b.tool_use_id
                ),
                None,
            )
            return OpenAIMessage(
                role=role,
                content=cls._blocks_to_openai_content(blocks),
                tool_call_id=tool_call_id,
            )

        return OpenAIMessage(
            role=role,
            content=cls._blocks_to_openai_content(blocks),
        )

    @staticmethod
    async def _resolve_attachments(
        parsed_msgs: list[tuple[str, list[_ContentBlock]]],
    ) -> tuple[list[InputAttachment], list[InputAttachment]]:
        last_user_blocks: list[_ContentBlock] = []
        all_image_blocks: list[_ContentBlock] = []

        for role, blocks in parsed_msgs:
            if role not in ("user", "system"):
                continue
            images = [b for b in blocks if b.type == "image"]
            all_image_blocks.extend(images)

        for role, blocks in reversed(parsed_msgs):
            if role in ("user", "system"):
                last_user_blocks = [b for b in blocks if b.type == "image"]
                break

        async def _prepare(
            blocks: list[_ContentBlock],
        ) -> list[InputAttachment]:
            attachments: list[InputAttachment] = []
            for idx, block in enumerate(blocks, start=1):
                prefix = f"message_image_{idx}"
                if block.url:
                    prepared = await download_remote_image(block.url, prefix=prefix)
                elif block.data and block.data.startswith("data:"):
                    prepared = parse_data_url(block.data, prefix=prefix)
                elif block.data and block.mime_type:
                    prepared = parse_base64_image(
                        block.data, block.mime_type, prefix=prefix
                    )
                else:
                    continue
                attachments.append(
                    InputAttachment(
                        filename=prepared.filename,
                        mime_type=prepared.mime_type,
                        data=prepared.data,
                    )
                )
            return attachments

        last_attachments = await _prepare(last_user_blocks)
        all_attachments = await _prepare(all_image_blocks)
        return last_attachments, all_attachments

    @staticmethod
    def _message_response(
        req: OpenAIChatRequest,
        message_id: str,
        content: list[dict[str, Any]],
        *,
        stop_reason: str,
    ) -> dict[str, Any]:
        return {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": req.model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }


class _AnthropicTaggedRenderer:
    def __init__(self, req: OpenAIChatRequest, message_id: str) -> None:
        self._req = req
        self._message_id = message_id
        self._started = False
        self._current_index = -1
        self._current_block_type: str | None = None

    def render(self, event: TaggedStreamEvent) -> list[str]:
        if event.type == "message_start":
            return self._message_start()
        if event.type == "block_start":
            if event.block_type == "thinking":
                return self.start_block("thinking")
            if event.block_type == "text":
                return self.start_block("text")
            return []
        if event.type == "block_delta":
            if event.block_type and event.text:
                return self.block_delta(event.block_type, event.text)
            return []
        if event.type == "block_end":
            return self.end_block()
        if event.type == "tool_call":
            return self.tool_call(event.name or "", event.arguments or {})
        if event.type == "message_stop":
            return self.message_stop(event.stop_reason or "end_turn")
        if event.type == "error":
            raise ValueError(event.error or "tagged stream parser error")
        return []

    def start_block(self, block_type: Literal["thinking", "text"]) -> list[str]:
        out = self._message_start()
        out.extend(self.end_block())
        self._current_index += 1
        self._current_block_type = block_type
        if block_type == "thinking":
            payload = {
                "type": "content_block_start",
                "index": self._current_index,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        else:
            payload = {
                "type": "content_block_start",
                "index": self._current_index,
                "content_block": {"type": "text", "text": ""},
            }
        out.append(self._event(payload))
        return out

    def block_delta(
        self,
        block_type: Literal["thinking", "text"],
        text: str,
    ) -> list[str]:
        if self._current_block_type != block_type:
            raise ValueError(f"unexpected delta for block type: {block_type}")
        if block_type == "thinking":
            delta = {"type": "thinking_delta", "thinking": text}
        else:
            delta = {"type": "text_delta", "text": text}
        return [
            self._event(
                {
                    "type": "content_block_delta",
                    "index": self._current_index,
                    "delta": delta,
                }
            )
        ]

    def end_block(self) -> list[str]:
        if self._current_block_type is None:
            return []
        block_index = self._current_index
        self._current_block_type = None
        return [
            self._event(
                {
                    "type": "content_block_stop",
                    "index": block_index,
                }
            )
        ]

    def tool_call(self, name: str, arguments: dict[str, Any]) -> list[str]:
        out = self._message_start()
        out.extend(self.end_block())
        self._current_index += 1
        tool_id = f"toolu_{uuid_mod.uuid4().hex[:24]}"
        out.append(
            self._event(
                {
                    "type": "content_block_start",
                    "index": self._current_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": {},
                    },
                }
            )
        )
        args_json = json.dumps(arguments, ensure_ascii=False)
        if args_json:
            out.append(
                self._event(
                    {
                        "type": "content_block_delta",
                        "index": self._current_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args_json,
                        },
                    }
                )
            )
        out.append(
            self._event(
                {
                    "type": "content_block_stop",
                    "index": self._current_index,
                }
            )
        )
        self._current_block_type = None
        return out

    def marker_text_block(self, marker: str) -> list[str]:
        if not marker:
            return []
        out = self.start_block("text")
        out.extend(self.block_delta("text", marker))
        out.extend(self.end_block())
        return out

    def message_stop(self, stop_reason: str) -> list[str]:
        out = self._message_start()
        out.extend(self.end_block())
        out.append(
            self._event(
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": stop_reason,
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 0},
                }
            )
        )
        out.append(self._event({"type": "message_stop"}))
        return out

    def _message_start(self) -> list[str]:
        if self._started:
            return []
        self._started = True
        return [
            self._event(
                {
                    "type": "message_start",
                    "message": {
                        "id": self._message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self._req.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
            )
        ]

    @staticmethod
    def _event(payload: dict[str, Any]) -> str:
        return f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
