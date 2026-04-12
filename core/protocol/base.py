"""协议适配器抽象。内部统一以 OpenAI 语义事件流为中间态。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from core.shared.models import OpenAIChatRequest
from core.stream.events import OpenAIStreamEvent


class ProtocolAdapter(ABC):
    protocol_name: str

    @abstractmethod
    async def parse_request(
        self,
        raw_body: dict[str, Any],
    ) -> OpenAIChatRequest: ...

    @abstractmethod
    def render_non_stream(
        self,
        req: OpenAIChatRequest,
        raw_events: list[OpenAIStreamEvent],
    ) -> dict[str, Any]: ...

    @abstractmethod
    def render_stream(
        self,
        req: OpenAIChatRequest,
        raw_stream: AsyncIterator[OpenAIStreamEvent],
    ) -> AsyncIterator[str]: ...

    @abstractmethod
    def render_error(self, exc: Exception) -> tuple[int, dict[str, Any]]: ...
