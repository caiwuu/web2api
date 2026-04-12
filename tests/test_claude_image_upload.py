import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import BrowserContext, Page

from core.plugin.claude import ClaudePlugin
from core.shared.models import InputAttachment


class TestClaudeImageUpload(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_attachments_uses_wiggle_upload_endpoint(self) -> None:
        plugin = ClaudePlugin()
        state = {"site_context": {"org_uuid": "org-123"}}
        attachment = InputAttachment(
            filename="message_1_image_1.png",
            mime_type="image/png",
            data=b"fake-image-bytes",
        )

        with patch(
            "core.plugin.claude.upload_file_via_page_fetch",
            new=AsyncMock(
                return_value={
                    "status": 200,
                    "json": {"success": True, "file_uuid": "file-123"},
                }
            ),
        ) as mock_upload:
            context = MagicMock(spec=BrowserContext)
            page = MagicMock(spec=Page)
            prepared = await plugin.prepare_attachments(
                context,
                page,
                "conv-456",
                state,
                [attachment],
            )

        args = mock_upload.await_args
        assert args is not None
        self.assertEqual(
            args.args[1],
            "https://claude.ai/api/organizations/org-123/conversations/conv-456/wiggle/upload-file",
        )
        self.assertEqual(prepared, {"attachments": [], "files": ["file-123"]})

        body = plugin.build_completion_body(
            "Please analyze the attached image.",
            "conv-456",
            state,
            prepared,
        )
        self.assertEqual(body["attachments"], [])
        self.assertEqual(body["files"], ["file-123"])


if __name__ == "__main__":
    unittest.main()
