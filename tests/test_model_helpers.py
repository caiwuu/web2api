import unittest
from unittest.mock import patch

from fastapi import HTTPException

from core.http.model_helpers import (
    UNKNOWN_MODEL_CREATED_AT,
    ensure_provider_model,
    format_anthropic_model_response,
    format_anthropic_models_response,
    format_openai_models_response,
    list_provider_model_ids,
)


class _FakePlugin:
    def __init__(self, mapping: dict[str, str] | None) -> None:
        self._mapping = mapping

    def model_mapping(self) -> dict[str, str] | None:
        return self._mapping


class TestModelHelpers(unittest.TestCase):
    def test_list_provider_model_ids(self) -> None:
        with patch(
            "core.http.model_helpers.PluginRegistry.get",
            return_value=_FakePlugin({"claude-sonnet": "site-model"}),
        ):
            self.assertEqual(list_provider_model_ids("claude"), ["claude-sonnet"])

    def test_list_provider_model_ids_requires_mapping(self) -> None:
        with patch(
            "core.http.model_helpers.PluginRegistry.get",
            return_value=_FakePlugin(None),
        ):
            with self.assertRaises(HTTPException) as ctx:
                list_provider_model_ids("claude")

        self.assertEqual(ctx.exception.status_code, 500)

    def test_ensure_provider_model_raises_404(self) -> None:
        with patch(
            "core.http.model_helpers.PluginRegistry.get",
            return_value=_FakePlugin({"claude-sonnet": "site-model"}),
        ):
            with self.assertRaises(HTTPException) as ctx:
                ensure_provider_model("claude", "missing-model")

        self.assertEqual(ctx.exception.status_code, 404)

    def test_format_openai_models_response(self) -> None:
        payload = format_openai_models_response("claude", ["claude-sonnet"])
        self.assertEqual(payload["object"], "list")
        self.assertEqual(payload["data"][0]["id"], "claude-sonnet")
        self.assertEqual(payload["data"][0]["owned_by"], "claude")

    def test_format_anthropic_models_response(self) -> None:
        payload = format_anthropic_models_response(
            ["claude-sonnet", "claude-opus"]
        )
        self.assertEqual(payload["first_id"], "claude-sonnet")
        self.assertEqual(payload["last_id"], "claude-opus")
        self.assertFalse(payload["has_more"])
        self.assertEqual(
            payload["data"][0],
            {
                "id": "claude-sonnet",
                "type": "model",
                "display_name": "claude-sonnet",
                "created_at": UNKNOWN_MODEL_CREATED_AT,
            },
        )

    def test_format_anthropic_model_response(self) -> None:
        self.assertEqual(
            format_anthropic_model_response("claude-sonnet"),
            {
                "id": "claude-sonnet",
                "type": "model",
                "display_name": "claude-sonnet",
                "created_at": UNKNOWN_MODEL_CREATED_AT,
            },
        )


if __name__ == "__main__":
    unittest.main()
