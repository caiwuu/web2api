import json
import unittest

from core.chat.prompt_builder import extract_user_content
from core.protocol.anthropic import AnthropicProtocolAdapter
from core.protocol.openai import OpenAIProtocolAdapter


class TestProtocolToolHistory(unittest.IsolatedAsyncioTestCase):
    async def test_openai_round_trips_assistant_tool_calls_and_tool_call_id(
        self,
    ) -> None:
        adapter = OpenAIProtocolAdapter()
        openai_req = await adapter.parse_request(
            {
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "Find the answer"},
                    {
                        "role": "assistant",
                        "content": "<think>Need to inspect the file</think>",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"path":"a.py"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_123",
                        "content": "file contents",
                    },
                    {"role": "user", "content": "continue"},
                ],
                "tools": [],
            },
        )

        assistant_msg = openai_req.messages[1]
        tool_msg = openai_req.messages[2]
        assert assistant_msg.tool_calls is not None
        tc0 = assistant_msg.tool_calls[0]
        self.assertEqual(tc0["id"], "call_123")
        self.assertEqual(
            json.loads(tc0["function"]["arguments"]),
            {"path": "a.py"},
        )
        self.assertEqual(tool_msg.tool_call_id, "call_123")

        prompt = extract_user_content(
            openai_req.messages,
            has_tools=True,
            tagged_prompt_prefix="PROMPT",
            full_history=True,
        )
        self.assertIn(
            '<tool_calls>[{"name": "Read", "arguments": {"path": "a.py"}}]</tool_calls>',
            prompt,
        )
        self.assertIn("Tool result for call_id=call_123:", prompt)
        self.assertIn("<tool_result>\nfile contents\n</tool_result>", prompt)
        self.assertIn("Do not output Observation.", prompt)
        self.assertIn("<tool_calls>[...]</tool_calls>", prompt)

    async def test_anthropic_round_trips_tool_use_and_tool_result(self) -> None:
        adapter = AnthropicProtocolAdapter()
        openai_req = await adapter.parse_request(
            {
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "Find the answer"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Need to inspect"},
                            {
                                "type": "tool_use",
                                "id": "toolu_123",
                                "name": "Read",
                                "input": {"path": "a.py"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_123",
                                "content": "file contents",
                            }
                        ],
                    },
                    {"role": "user", "content": "continue"},
                ],
                "tools": [],
            },
        )

        assistant_msg = openai_req.messages[1]
        tool_msg = openai_req.messages[2]
        assert assistant_msg.tool_calls is not None
        tc0 = assistant_msg.tool_calls[0]
        self.assertEqual(tc0["id"], "toolu_123")
        self.assertEqual(tc0["function"]["name"], "Read")
        self.assertEqual(
            json.loads(tc0["function"]["arguments"]),
            {"path": "a.py"},
        )
        self.assertEqual(tool_msg.role, "tool")
        self.assertEqual(tool_msg.tool_call_id, "toolu_123")
        self.assertEqual(tool_msg.content, "file contents")


if __name__ == "__main__":
    unittest.main()
