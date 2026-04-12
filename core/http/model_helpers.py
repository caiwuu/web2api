"""Shared helpers for protocol-specific model listing endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException

from core.plugin.base import PluginRegistry

UNKNOWN_MODEL_CREATED_AT = "1970-01-01T00:00:00Z"


def list_provider_model_ids(provider: str) -> list[str]:
    plugin = PluginRegistry.get(provider)
    try:
        mapping = plugin.model_mapping() if plugin is not None else None
    except Exception:
        mapping = None

    if isinstance(mapping, dict) and mapping:
        return list(mapping.keys())

    raise HTTPException(status_code=500, detail="model_mapping is not implemented")


def ensure_provider_model(provider: str, model_id: str) -> str:
    model_ids = list_provider_model_ids(provider)
    if model_id in model_ids:
        return model_id
    raise HTTPException(status_code=404, detail="model not found")


def format_openai_models_response(provider: str, model_ids: list[str]) -> dict[str, Any]:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": now,
                "owned_by": provider,
            }
            for model_id in model_ids
        ],
    }


def format_anthropic_model_response(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "type": "model",
        "display_name": model_id,
        "created_at": UNKNOWN_MODEL_CREATED_AT,
    }


def format_anthropic_models_response(model_ids: list[str]) -> dict[str, Any]:
    return {
        "data": [
            format_anthropic_model_response(model_id)
            for model_id in model_ids
        ],
        "first_id": model_ids[0],
        "last_id": model_ids[-1],
        "has_more": False,
    }
