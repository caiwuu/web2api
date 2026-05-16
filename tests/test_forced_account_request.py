import unittest

from core.protocol.openai import OpenAIProtocolAdapter
from core.protocol.service import CanonicalChatService


class _CapturingHandler:
    def __init__(self) -> None:
        self.openai_req = None

    async def stream_openai_events(self, _provider, openai_req):
        self.openai_req = openai_req
        if False:
            yield None


class TestForcedAccountRequest(unittest.IsolatedAsyncioTestCase):
    async def test_openai_web2api_account_reaches_handler_request(self) -> None:
        canonical_req = OpenAIProtocolAdapter().parse_request(
            "claude",
            {
                "model": "claude-sonnet-4-6",
                "web2api_account": "claude-01",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        handler = _CapturingHandler()
        service = CanonicalChatService(handler)

        await service.collect_raw(canonical_req)

        self.assertIsNotNone(handler.openai_req)
        self.assertEqual(handler.openai_req.web2api_account, "claude-01")


if __name__ == "__main__":
    unittest.main()
