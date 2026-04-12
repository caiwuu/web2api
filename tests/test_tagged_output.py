import unittest

from core.shared.tool_calls import format_tools_for_prompt
from core.shared.tagged_output import TaggedOutputError, parse_tagged_output
from core.shared.tagged_output import format_tagged_prompt
from core.shared.tagged_stream_parser import TaggedStreamParser


class TestTaggedOutput(unittest.TestCase):
    def test_parse_single_tool_call_with_thinking(self) -> None:
        parsed = parse_tagged_output(
            "<think>Read file first</think>"
            '<tool_call>{"name":"Read","arguments":{"path":"a.py"}}</tool_call>'
        )

        self.assertEqual(parsed.thinking, "Read file first")
        self.assertIsNotNone(parsed.tool_call)
        self.assertEqual(parsed.tool_call.name, "Read")
        self.assertEqual(parsed.tool_call.arguments, {"path": "a.py"})
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertIsNone(parsed.final_answer)

    def test_parse_tool_calls_with_thinking(self) -> None:
        parsed = parse_tagged_output(
            "<think>Read files first</think>"
            '<tool_calls>[{"name":"Read","arguments":{"path":"a.py"}},'
            '{"name":"Read","arguments":{"path":"b.py"}}]</tool_calls>'
        )

        self.assertEqual(parsed.thinking, "Read files first")
        self.assertEqual(len(parsed.tool_calls), 2)
        self.assertEqual(parsed.tool_calls[0].name, "Read")
        self.assertEqual(parsed.tool_calls[0].arguments, {"path": "a.py"})
        self.assertEqual(parsed.tool_calls[1].arguments, {"path": "b.py"})
        self.assertIsNone(parsed.final_answer)

    def test_parse_final_answer(self) -> None:
        parsed = parse_tagged_output(
            "<think>Done</think><final_answer>Hello world</final_answer>"
        )

        self.assertEqual(parsed.thinking, "Done")
        self.assertEqual(parsed.final_answer, "Hello world")
        self.assertFalse(parsed.is_tool_call)

    def test_rejects_text_outside_tags(self) -> None:
        with self.assertRaises(TaggedOutputError):
            parse_tagged_output("prefix <final_answer>Hello world</final_answer>")

    def test_format_tagged_prompt_supports_tool_choice(self) -> None:
        prompt = format_tagged_prompt(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                }
            ],
            tool_choice="none",
        )

        self.assertIn("Tool choice for this response is fixed to none.", prompt)
        self.assertIn("Your terminal block must be <final_answer>.", prompt)
        self.assertNotIn("## Available tools", prompt)

    def test_format_tools_for_prompt_handles_null_properties(self) -> None:
        rendered = format_tools_for_prompt(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "NoProps",
                        "description": "Tool with null properties",
                        "parameters": {
                            "type": "object",
                            "properties": None,
                            "required": [],
                        },
                    },
                }
            ]
        )
        self.assertIn("NoProps():", rendered)
        self.assertIn("Tool with null properties", rendered)

    def test_parse_multiple_thinks_and_ignore_trailing_content(self) -> None:
        parsed = parse_tagged_output(
            "<think>first</think>\n"
            "<think>second</think>\n"
            "<final_answer>done</final_answer>\n"
            "<think>ignored</think>"
        )

        self.assertEqual(parsed.thinking, "first\n\nsecond")
        self.assertEqual(parsed.final_answer, "done")

    def test_stream_parser_handles_chunked_tool_calls(self) -> None:
        parser = TaggedStreamParser()
        events = []
        for chunk in [
            "<thi",
            "nk>Need file</thi",
            "nk><tool_",
            'calls>[{"name":"Read","arguments":{"path":"a.py"}},',
            '{"name":"Read","arguments":{"path":"b.py"}}]</tool_calls>',
        ]:
            events.extend(parser.feed(chunk))
        events.extend(parser.finish())

        event_types = [event.type for event in events]
        self.assertEqual(
            event_types,
            [
                "message_start",
                "block_start",
                "block_delta",
                "block_end",
                "tool_call",
                "tool_call",
                "message_stop",
            ],
        )
        self.assertEqual(events[2].text, "Need file")
        self.assertEqual(events[4].name, "Read")
        self.assertEqual(events[4].arguments, {"path": "a.py"})
        self.assertEqual(events[4].call_index, 0)
        self.assertEqual(events[5].name, "Read")
        self.assertEqual(events[5].arguments, {"path": "b.py"})
        self.assertEqual(events[5].call_index, 1)
        self.assertEqual(events[6].stop_reason, "tool_use")

    def test_stream_parser_flushes_text_per_chunk(self) -> None:
        parser = TaggedStreamParser()

        first = parser.feed("<final_answer>Hello")
        second = parser.feed(" world")
        third = parser.feed("</final_answer>")
        fourth = parser.finish()

        self.assertEqual(
            [event.type for event in first],
            ["message_start", "block_start", "block_delta"],
        )
        self.assertEqual(first[2].text, "Hello")
        self.assertEqual([event.type for event in second], ["block_delta"])
        self.assertEqual(second[0].text, " world")
        self.assertEqual([event.type for event in third], ["block_end"])
        self.assertEqual([event.type for event in fourth], ["message_stop"])

    def test_stream_parser_keeps_literal_angle_brackets_in_final_answer(self) -> None:
        parser = TaggedStreamParser()
        events = parser.feed("<final_answer>Hello <b>world</b></final_answer>")
        events.extend(parser.finish())

        self.assertEqual(events[0].type, "message_start")
        self.assertEqual(events[1].type, "block_start")
        self.assertEqual(events[-2].type, "block_end")
        self.assertEqual(events[-1].type, "message_stop")
        self.assertEqual(
            "".join(
                event.text or "" for event in events if event.type == "block_delta"
            ),
            "Hello <b>world</b>",
        )
        self.assertEqual(events[-1].stop_reason, "end_turn")

    def test_stream_parser_allows_multiple_thinks_before_terminal(self) -> None:
        parser = TaggedStreamParser()
        events = parser.feed(
            "<think>one</think><think>two</think><final_answer>done</final_answer>"
        )
        events.extend(parser.finish())

        self.assertEqual(
            [event.type for event in events],
            [
                "message_start",
                "block_start",
                "block_delta",
                "block_end",
                "block_start",
                "block_delta",
                "block_end",
                "block_start",
                "block_delta",
                "block_end",
                "message_stop",
            ],
        )
        self.assertEqual(
            [
                (event.block_type, event.text)
                for event in events
                if event.type == "block_delta"
            ],
            [("thinking", "one"), ("thinking", "two"), ("text", "done")],
        )

    def test_stream_parser_ignores_think_after_terminal(self) -> None:
        parser = TaggedStreamParser()
        events = parser.feed("<final_answer>done</final_answer><think>ignored</think>")
        events.extend(parser.finish())

        self.assertEqual(
            [event.type for event in events],
            [
                "message_start",
                "block_start",
                "block_delta",
                "block_end",
                "message_stop",
            ],
        )
        self.assertEqual(events[2].text, "done")


if __name__ == "__main__":
    unittest.main()
