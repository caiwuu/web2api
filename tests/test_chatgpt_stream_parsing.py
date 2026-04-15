import time
import unittest

from core.plugin.chatgpt import (
    _extract_legacy_text,
    _freeze_until_from_error,
    _parse_sse_chunks,
    _parse_stream_payload,
)


class TestChatGPTStreamParsing(unittest.TestCase):
    def test_parse_sse_chunks_keeps_event_types(self) -> None:
        buffer, events = _parse_sse_chunks(
            "",
            'event: delta_encoding\ndata: "v1"\n\n'
            'event: delta\ndata: {"v":[{"p":"/message/content/parts/0","o":"append","v":"你好"}]}\n\n',
        )
        self.assertEqual(buffer, "")
        self.assertEqual(
            events,
            [
                ("delta_encoding", '"v1"'),
                (
                    "delta",
                    '{"v":[{"p":"/message/content/parts/0","o":"append","v":"你好"}]}',
                ),
            ],
        )

    def test_parse_stream_payload_handles_v1_delta_flow(self) -> None:
        seen: dict[str, str] = {}

        encoding = _parse_stream_payload(
            "delta_encoding",
            '"v1"',
            use_v1=False,
            seen_text_by_message_id=seen,
        )
        self.assertTrue(encoding.use_v1)

        added = _parse_stream_payload(
            "delta",
            '{"o":"add","v":{"message":{"id":"msg-1","content":{"content_type":"text","parts":[""]}}}}',
            use_v1=True,
            seen_text_by_message_id=seen,
        )
        self.assertEqual(added.message_id, "msg-1")
        self.assertEqual(added.texts, [])

        appended = _parse_stream_payload(
            "delta",
            '{"v":[{"p":"/message/content/parts/0","o":"append","v":"今天是星期二"}]}',
            use_v1=True,
            seen_text_by_message_id=seen,
        )
        self.assertEqual(appended.texts, ["今天是星期二"])

    def test_extract_legacy_text_returns_increment_only(self) -> None:
        seen: dict[str, str] = {}
        first = {
            "message": {
                "id": "msg-2",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["Hello"]},
            }
        }
        second = {
            "message": {
                "id": "msg-2",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["Hello world"]},
            }
        }

        texts_1, message_id_1 = _extract_legacy_text(first, seen)
        texts_2, message_id_2 = _extract_legacy_text(second, seen)

        self.assertEqual(message_id_1, "msg-2")
        self.assertEqual(texts_1, ["Hello"])
        self.assertEqual(message_id_2, "msg-2")
        self.assertEqual(texts_2, [" world"])

    def test_v1_delta_ignores_user_message_text(self) -> None:
        """user/system 消息的 content 不应被当作 assistant 输出提取。"""
        seen: dict[str, str] = {}
        user_msg = _parse_stream_payload(
            "delta",
            '{"o":"add","v":{"message":{"id":"user-1","author":{"role":"user"},'
            '"content":{"content_type":"text","parts":["<tool_calls>[{}]</tool_calls>"]},'
            '"status":"finished_successfully"}}}',
            use_v1=True,
            seen_text_by_message_id=seen,
        )
        self.assertEqual(user_msg.message_id, "user-1")
        self.assertEqual(user_msg.message_role, "user")
        self.assertEqual(user_msg.texts, [], "user 消息文本不应作为输出")

        system_msg = _parse_stream_payload(
            "delta",
            '{"v":{"message":{"id":"sys-1","author":{"role":"system"},'
            '"content":{"content_type":"text","parts":["system prompt"]},'
            '"status":"finished_successfully"}}}',
            use_v1=True,
            seen_text_by_message_id=seen,
        )
        self.assertEqual(system_msg.message_role, "system")
        self.assertEqual(system_msg.texts, [], "system 消息文本不应作为输出")

    def test_freeze_until_from_error_handles_risk_control(self) -> None:
        now = int(time.time())
        freeze_until = _freeze_until_from_error(
            "HTTP 403 {\"detail\":\"Unusual activity has been detected from your device. Try again later.\"}"
        )
        self.assertIsNotNone(freeze_until)
        assert freeze_until is not None
        self.assertGreaterEqual(freeze_until, now + 1700)


if __name__ == "__main__":
    unittest.main()
