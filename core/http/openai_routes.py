"""
OpenAI 协议路由。

支持：
- /openai/{provider}/v1/chat/completions
- /openai/{provider}/v1/models
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from core.chat.handler import ChatHandler
from core.http.dependencies import get_chat_handler
from core.http.model_helpers import (
    format_openai_models_response,
    list_provider_model_ids,
)
from core.http.route_helpers import (
    create_authenticated_router,
    format_openai_stream_error,
    handle_chat_request,
)
from core.protocol.openai import OpenAIProtocolAdapter


def create_openai_router() -> APIRouter:
    """创建 OpenAI 协议路由。"""
    router = create_authenticated_router()
    adapter = OpenAIProtocolAdapter()

    @router.get("/openai/{provider}/v1/models")
    def list_models(provider: str) -> dict[str, Any]:
        return format_openai_models_response(provider, list_provider_model_ids(provider))

    @router.post("/openai/{provider}/v1/chat/completions")
    async def chat_completions(
        provider: str,
        request: Request,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        return await handle_chat_request(
            adapter=adapter,
            provider=provider,
            request=request,
            handler=handler,
            stream_error_formatter=format_openai_stream_error,
        )

    return router
