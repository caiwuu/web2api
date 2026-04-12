"""
OpenAI 语义的结构化流事件（唯一流式中间态）。

整条链路：插件产出字符串流 → core 包装为 content_delta + finish →
协议适配层消费事件、编码为各协议 SSE（OpenAI / Anthropic / 未来 Kimi 等）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class OpenAIToolCallDelta(BaseModel):
    """OpenAI stream delta 中的 tool_calls[?] 片段（最小必要字段）。"""

    index: int = 0
    id: str | None = None
    type: Literal["function"] = "function"
    function: dict[str, Any] = Field(default_factory=dict)


class OpenAIStreamEvent(BaseModel):
    """
    OpenAI 语义的“内部流事件”。
    - content_delta：增量文本（delta.content）
    - tool_call_delta：工具调用增量（delta.tool_calls）
    - finish：结束（finish_reason）
    - error：错误
    协议适配层负责将事件序列化为目标协议的 SSE/JSON。
    """

    type: Literal["content_delta", "tool_call_delta", "finish", "error"]

    # content_delta
    content: str | None = None

    # tool_call_delta
    tool_calls: list[OpenAIToolCallDelta] | None = None

    # finish
    finish_reason: str | None = None

    # error
    error: str | None = None
