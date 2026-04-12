"""Helpers for rendering OpenAI-compatible tool calls from tagged output."""

from __future__ import annotations

import json
import uuid
from typing import Any

from toolcall_gateway import format_tools_for_prompt as _format_tools_for_prompt


def format_tools_for_prompt(tools: list[dict[str, Any]]) -> str:
    """
    将 OpenAI / Cursor 风格的 tools 转为可读文本，用于 tagged prompt。
    """
    return _format_tools_for_prompt(tools)


def _normalize_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False)
    try:
        parsed = json.loads(str(arguments)) if arguments else {}
    except json.JSONDecodeError:
        parsed = {}
    return json.dumps(parsed, ensure_ascii=False)


def build_tool_calls_response(
    tool_calls_list: list[dict[str, Any]],
    chat_id: str,
    model: str,
    created: int,
    *,
    text_content: str = "",
) -> dict[str, Any]:
    """返回 OpenAI Chat Completions 兼容的 tool_calls 响应。"""
    tool_calls: list[dict[str, Any]] = []
    for tool_call in tool_calls_list:
        tool_calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tool_call.get("name", ""),
                    "arguments": _normalize_tool_arguments(
                        tool_call.get("arguments", {})
                    ),
                },
            }
        )

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def build_tool_calls_with_ids(
    tool_calls_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """构建带稳定 id 的 OpenAI tool_calls delta 片段。"""
    tool_calls: list[dict[str, Any]] = []
    for index, tool_call in enumerate(tool_calls_list):
        tool_calls.append(
            {
                "index": index,
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tool_call.get("name", ""),
                    "arguments": _normalize_tool_arguments(
                        tool_call.get("arguments", {})
                    ),
                },
            }
        )
    return tool_calls
