"""
把 OpenAIStreamEvent 编码为 OpenAI ChatCompletions SSE chunk。

这是 Stream 层的“协议输出工具”，用于把插件输出的结构化事件流转换为
OpenAI 兼容的 `data: {...}\\n\\n` 片段。

当前不替换既有渲染链路，仅提供给后续协议/插件扩展使用。
"""

from __future__ import annotations

import json
import time
import uuid as uuid_mod
from collections.abc import AsyncIterator, Iterator

from core.stream.events import OpenAIStreamEvent


def make_openai_stream_context(*, model: str) -> tuple[str, int]:
    """生成 OpenAI SSE 上下文：chat_id + created。"""
    chat_id = f"chatcmpl-{uuid_mod.uuid4().hex[:24]}"
    created = int(time.time())
    # model 由上层写入 payload
    del model
    return chat_id, created


def _chunk(
    *,
    chat_id: str,
    model: str,
    created: int,
    delta: dict,
    finish_reason: str | None = None,
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
                        "delta": delta,
                        "logprobs": None,
                        "finish_reason": finish_reason,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )


def encode_openai_sse_events(
    events: Iterator[OpenAIStreamEvent],
    *,
    chat_id: str,
    model: str,
    created: int,
) -> Iterator[str]:
    """同步编码器：OpenAIStreamEvent -> OpenAI SSE strings。"""
    # 兼容主流 OpenAI SSE 客户端：先发一帧 role:assistant + content:""
    yield _chunk(
        chat_id=chat_id,
        model=model,
        created=created,
        delta={"role": "assistant", "content": ""},
        finish_reason=None,
    )
    for ev in events:
        if ev.type == "content_delta":
            if ev.content:
                yield _chunk(
                    chat_id=chat_id,
                    model=model,
                    created=created,
                    delta={"content": ev.content},
                    finish_reason=None,
                )
        elif ev.type == "tool_call_delta":
            if ev.tool_calls:
                yield _chunk(
                    chat_id=chat_id,
                    model=model,
                    created=created,
                    delta={"tool_calls": [tc.model_dump() for tc in ev.tool_calls]},
                    finish_reason=None,
                )
        elif ev.type == "finish":
            # OpenAI 的结束 chunk 允许 delta 为空对象
            yield _chunk(
                chat_id=chat_id,
                model=model,
                created=created,
                delta={},
                finish_reason=ev.finish_reason or "stop",
            )
            yield "data: [DONE]\n\n"
            return
        elif ev.type == "error":
            # OpenAI SSE 没有标准 error 事件，这里用 data 包一层 error 对象（与现有实现一致风格）
            msg = ev.error or "unknown error"
            yield (
                "data: "
                + json.dumps(
                    {"error": {"message": msg, "type": "server_error"}},
                    ensure_ascii=False,
                )
                + "\n\n"
            )


async def encode_openai_sse_events_async(
    events: AsyncIterator[OpenAIStreamEvent],
    *,
    chat_id: str,
    model: str,
    created: int,
) -> AsyncIterator[str]:
    """异步编码器：OpenAIStreamEvent -> OpenAI SSE strings。"""
    async for ev in events:
        for out in encode_openai_sse_events(
            iter([ev]),
            chat_id=chat_id,
            model=model,
            created=created,
        ):
            yield out
