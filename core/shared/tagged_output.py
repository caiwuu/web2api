"""Compatibility wrappers around the external toolcall-gateway library."""

from __future__ import annotations

from typing import Any

from toolcall_gateway import TaggedOutput, TaggedOutputError, TaggedToolCall
from toolcall_gateway import build_tagged_prompt as _build_tagged_prompt
from toolcall_gateway import parse_tagged_output as _parse_tagged_output

__all__ = [
    "TaggedOutput",
    "TaggedOutputError",
    "TaggedToolCall",
    "format_tagged_prompt",
    "parse_tagged_output",
    "format_openai_tagged_answer",
]


def format_tagged_prompt(
    tools: list[dict[str, Any]],
    tools_text: str | None = None,
    *,
    allow_parallel_tool_calls: bool = True,
    tool_choice: str | dict[str, Any] | None = None,
) -> str:
    """Build the tagged-protocol system prompt via toolcall-gateway."""
    return _build_tagged_prompt(
        tools,
        tools_text=tools_text,
        allow_parallel_tool_calls=allow_parallel_tool_calls,
        tool_choice=tool_choice,
    )


def parse_tagged_output(text: str) -> TaggedOutput:
    """Parse strict tagged output via toolcall-gateway."""
    return _parse_tagged_output(text)


def format_openai_tagged_answer(parsed: TaggedOutput) -> str:
    """Render a tagged final answer for OpenAI-compatible text content."""
    if not parsed.is_final_answer:
        raise TaggedOutputError("tagged output is not a final answer")
    parts: list[str] = []
    if parsed.thinking:
        parts.append(f"<think>{parsed.thinking}</think>")
    parts.append(parsed.final_answer or "")
    return "\n\n".join(part for part in parts if part)
