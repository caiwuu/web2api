"""Chat 编排层：对外 `ChatHandler`，内部由 runtime / scheduler / executor 协作。"""

from core.chat.handler import ChatHandler

__all__ = ["ChatHandler"]
