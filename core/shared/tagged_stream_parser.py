"""Compatibility wrappers around the external toolcall-gateway streaming parser."""

from toolcall_gateway import TaggedOutputError, TaggedStreamEvent, TaggedStreamParser

__all__ = [
    "TaggedOutputError",
    "TaggedStreamEvent",
    "TaggedStreamParser",
]
