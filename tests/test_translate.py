"""Unit tests for the Anthropic <-> Agentaus translation layer.

Run with:  ./.venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from xml_style import setUpModule, tearDownModule  # noqa: E402,F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge.translate import (  # noqa: E402
    NO_VISION_SYSTEM,
    AnthropicStreamBuilder,
    ToolCallAccumulator,
    agentaus_response_to_anthropic,
    anthropic_request_to_agentaus as _translate,
    estimate_request_tokens,
)
from agentaus_bridge.identity import IDENTITY  # noqa: E402


def anthropic_request_to_agentaus(body: dict, **kwargs) -> dict:
    """The translation with the identity line taken off the front of the system prompt.
    These tests are about how a conversation is translated; tests/test_identity.py guards
    the identity line itself."""
    out = _translate(body, **kwargs)
    messages = out["messages"]
    if messages and messages[0]["role"] == "system" and messages[0]["content"].startswith(IDENTITY):
        rest = messages[0]["content"][len(IDENTITY):].lstrip("\n")
        out["messages"] = ([{"role": "system", "content": rest}] if rest else []) + messages[1:]
    return out


def events(blob: bytes) -> list[dict]:
    """Parse an SSE byte blob into its JSON payloads."""
    out = []
    for chunk in blob.decode().split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


class TestRequestTranslation(unittest.TestCase):
    def test_system_blocks_become_a_system_message(self):
        result = anthropic_request_to_agentaus(
            {
                "system": [{"type": "text", "text": "You are a coding agent."}],
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        self.assertEqual(result["messages"][0],
                         {"role": "system", "content": "You are a coding agent.\n\n" + NO_VISION_SYSTEM})
        # Without this flag Agentaus prepends its own persona.
        self.assertTrue(result["system_prompt_overwrite"])

    def test_string_content_passes_through(self):
        result = anthropic_request_to_agentaus({"messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(result["messages"], [{"role": "user", "content": "hello"}])

    def test_tool_use_and_result_round_trip_ordering(self):
        """Anthropic puts tool_result in a user turn; OpenAI needs assistant-then-tool."""
        result = anthropic_request_to_agentaus(
            {
                "messages": [
                    {"role": "user", "content": "read the file"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Reading."},
                            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "a.py"}},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "print(1)"}
                        ],
                    },
                ]
            }
        )
        roles = [m["role"] for m in result["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        assistant = result["messages"][1]
        self.assertEqual(assistant["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "Read")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"path": "a.py"})
        self.assertEqual(result["messages"][2]["tool_call_id"], "toolu_1")
        body = result["messages"][2]["content"]
        self.assertIn('<tool_result tool="Read">', body)
        self.assertIn("print(1)", body)

    def test_tool_result_error_is_marked(self):
        result = anthropic_request_to_agentaus(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_9",
                                "content": "no such file",
                                "is_error": True,
                            }
                        ],
                    }
                ]
            }
        )
        body = result["messages"][0]["content"]
        self.assertIn("<tool_error", body)
        self.assertIn("no such file", body)
        self.assertIn("Do not treat its output as an answer", body)

    def test_tools_are_mapped_and_server_side_tools_dropped(self):
        result = anthropic_request_to_agentaus(
            {
                "messages": [{"role": "user", "content": "x"}],
                "tools": [
                    {
                        "name": "Bash",
                        "description": "run a command",
                        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                    },
                    {"type": "web_search_20250305", "name": "web_search"},  # no input_schema
                ],
                "tool_choice": {"type": "auto"},
            }
        )
        self.assertEqual(len(result["tools"]), 1)
        self.assertEqual(result["tools"][0]["function"]["name"], "Bash")
        self.assertEqual(result["tool_choice"], "auto")

    def test_tool_choice_variants(self):
        def choice(value):
            return anthropic_request_to_agentaus(
                {
                    "messages": [{"role": "user", "content": "x"}],
                    "tools": [{"name": "T", "input_schema": {"type": "object"}}],
                    "tool_choice": value,
                }
            )["tool_choice"]

        self.assertEqual(choice({"type": "any"}), "required")
        self.assertEqual(choice({"type": "none"}), "none")
        self.assertEqual(choice({"type": "tool", "name": "T"}),
                         {"type": "function", "function": {"name": "T"}})

    def test_images_degrade_to_a_note_rather_than_vanishing(self):
        result = anthropic_request_to_agentaus(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
                            {"type": "text", "text": "what is this?"},
                        ],
                    }
                ]
            }
        )
        body = result["messages"][-1]["content"]
        self.assertIn("Image omitted: you cannot see images", body)
        self.assertIn("what is this?", body)
        # Even with no system prompt of its own, the request says the model has no vision.
        # That creates a system message, so Agentaus's own persona is replaced too.
        self.assertEqual(result["messages"][0], {"role": "system", "content": NO_VISION_SYSTEM})
        self.assertTrue(result["system_prompt_overwrite"])

    def test_thinking_blocks_are_dropped(self):
        result = anthropic_request_to_agentaus(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "secret reasoning"},
                            {"type": "text", "text": "answer"},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(result["messages"][0]["content"], "answer")

    def test_empty_assistant_turn_is_skipped(self):
        result = anthropic_request_to_agentaus(
            {"messages": [{"role": "assistant", "content": []}, {"role": "user", "content": "hi"}]}
        )
        self.assertEqual([m["role"] for m in result["messages"]], ["user"])


IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "iVBORw0KGgo" + "A" * 50000}}


def _read_result(content):
    """A Read call whose tool_result carries `content`, translated."""
    return anthropic_request_to_agentaus({
        "system": "You are a coding agent.",
        "messages": [
            {"role": "user", "content": "What does /tmp/shot.png say?"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Read",
                 "input": {"file_path": "/tmp/shot.png"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": content}]},
        ],
    })


class TestNoVision(unittest.TestCase):
    """Agentaus cannot see images, and every path an image takes must say so."""

    def test_an_image_only_tool_result_is_a_note_not_base64(self):
        tool = _read_result([IMAGE])["messages"][-1]
        self.assertEqual(tool["role"], "tool")
        self.assertIn("Image omitted: you cannot see images", tool["content"])
        self.assertNotIn("iVBORw0KGgo", tool["content"])
        self.assertIn("calling the tool again will not change that", tool["content"])
        self.assertNotIn("never ask for it", tool["content"])

    def test_a_caption_with_an_image_keeps_the_caption_and_notes_the_image(self):
        tool = _read_result([{"type": "text", "text": "Screenshot captured."}, IMAGE])["messages"][-1]
        self.assertIn("Screenshot captured.", tool["content"])
        self.assertIn("Image omitted", tool["content"])
        self.assertNotIn("never ask for it", tool["content"])

    def test_an_image_nested_in_another_container_gets_its_note(self):
        """has_unseen finds an image at any depth, so the note must appear at any depth
        too - otherwise the empty text falls back to sending the base64."""
        nested = [{"type": "search_result", "source": "https://x", "title": "x",
                   "content": [{"type": "text", "text": "a caption"}, IMAGE]}]
        tool = _read_result(nested)["messages"][-1]
        self.assertIn("a caption", tool["content"])
        self.assertIn("Image omitted", tool["content"])
        self.assertNotIn("iVBORw0KGgo", tool["content"])

    def test_a_plain_text_document_is_passed_through(self):
        """A text-source document is readable, so it is neither omitted nor 'unseen'."""
        doc = {"type": "document", "title": "notes.txt",
               "source": {"type": "text", "media_type": "text/plain", "data": "the launch is on 3 May"}}
        result = anthropic_request_to_agentaus({"messages": [{"role": "user", "content": [
            doc, {"type": "text", "text": "when is the launch?"}]}]})
        body = result["messages"][-1]["content"]
        self.assertIn("the launch is on 3 May", body)
        self.assertNotIn("Document omitted", body)
        self.assertFalse(any(m["role"] == "system" for m in result["messages"]))

    def test_a_text_tool_result_keeps_its_usual_framing(self):
        tool = _read_result([{"type": "text", "text": "def main(): pass"}])["messages"][-1]
        self.assertIn("never ask for it", tool["content"])
        self.assertNotIn("Image omitted", tool["content"])

    def test_every_request_says_the_model_cannot_see_images(self):
        system = _read_result([{"type": "text", "text": "x"}])["messages"][0]["content"]
        self.assertIn(NO_VISION_SYSTEM, system)

    def test_a_document_attachment_is_noted_too(self):
        result = anthropic_request_to_agentaus({"messages": [{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "data": "JVBERi0"}},
            {"type": "text", "text": "summarise this"}]}]})
        self.assertIn("Document omitted", result["messages"][-1]["content"])

    def test_an_image_is_estimated_as_its_note_not_its_base64(self):
        with_image = {"messages": [{"role": "user", "content": [
            IMAGE, {"type": "text", "text": "what is this?"}]}]}
        self.assertLess(estimate_request_tokens(with_image), 500)


class TestResponseTranslation(unittest.TestCase):
    def test_text_response(self):
        message = agentaus_response_to_anthropic(
            {
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}],
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
            model="agentaus",
        )
        self.assertEqual(message["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(message["stop_reason"], "end_turn")
        self.assertEqual(message["usage"], {"input_tokens": 10, "output_tokens": 3})

    def test_tool_call_response(self):
        message = agentaus_response_to_anthropic(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "Read", "arguments": '{"path":"a.py"}'},
                                }
                            ],
                        },
                    }
                ]
            },
            model="agentaus",
        )
        self.assertEqual(message["stop_reason"], "tool_use")
        block = message["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["name"], "Read")
        self.assertEqual(block["input"], {"path": "a.py"})

    def test_malformed_arguments_do_not_crash(self):
        message = agentaus_response_to_anthropic(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {"id": "c", "function": {"name": "T", "arguments": "not json"}}
                            ]
                        },
                    }
                ]
            },
            model="agentaus",
        )
        self.assertEqual(message["content"][0]["input"], {"__raw_arguments__": "not json"})


class TestStreamBuilder(unittest.TestCase):
    def test_text_stream_is_well_formed(self):
        builder = AnthropicStreamBuilder("agentaus", input_tokens=5, chunk_chars=4)
        blob = builder.start() + builder.text("abcdefgh") + builder.finish("stop", {"output_tokens": 2})
        types = [e["type"] for e in events(blob)]
        self.assertEqual(types[0], "message_start")
        self.assertEqual(types[1], "content_block_start")
        self.assertEqual(types.count("content_block_delta"), 2)  # 8 chars / 4
        self.assertEqual(types[-3], "content_block_stop")
        self.assertEqual(types[-2], "message_delta")
        self.assertEqual(types[-1], "message_stop")

    def test_tool_use_stream_closes_the_text_block_first(self):
        builder = AnthropicStreamBuilder("agentaus", chunk_chars=0)
        blob = (
            builder.start()
            + builder.text("thinking out loud")
            + builder.tool_use("call_1", "Read", '{"path":"a.py"}')
            + builder.finish("tool_calls", None)
        )
        parsed = events(blob)
        types = [e["type"] for e in parsed]
        # text block (index 0) must be closed before the tool block (index 1) opens
        first_stop = types.index("content_block_stop")
        tool_start = next(
            i for i, e in enumerate(parsed)
            if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"
        )
        self.assertLess(first_stop, tool_start)
        self.assertEqual(parsed[tool_start]["index"], 1)
        self.assertEqual(parsed[tool_start]["content_block"]["input"], {})
        deltas = [e for e in parsed if e.get("delta", {}).get("type") == "input_json_delta"]
        self.assertEqual("".join(d["delta"]["partial_json"] for d in deltas), '{"path":"a.py"}')
        self.assertEqual(parsed[-2]["delta"]["stop_reason"], "tool_use")

    def test_tool_block_beats_generic_stop_finish(self):
        # A recovered tool call can arrive inside a stream Agentaus still finishes
        # with the generic "stop"; the emitted tool_use blocks must not end up
        # paired with end_turn.
        builder = AnthropicStreamBuilder("agentaus", chunk_chars=0)
        blob = (
            builder.start()
            + builder.tool_use("call_1", "Read", '{"path":"a.py"}')
            + builder.finish("stop", None)
        )
        parsed = events(blob)
        self.assertTrue(any(
            e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"
            for e in parsed
        ))
        self.assertEqual(parsed[-2]["delta"]["stop_reason"], "tool_use")

    def test_tool_block_beats_stop_finish_even_after_text(self):
        builder = AnthropicStreamBuilder("agentaus", chunk_chars=0)
        blob = (
            builder.start()
            + builder.text("let me look")
            + builder.tool_use("call_1", "Bash", '{"cmd":"ls"}')
            + builder.finish("stop", None)
        )
        parsed = events(blob)
        self.assertEqual(parsed[-2]["delta"]["stop_reason"], "tool_use")

    def test_text_stream_still_ends_end_turn(self):
        builder = AnthropicStreamBuilder("agentaus", chunk_chars=0)
        blob = builder.start() + builder.text("hello") + builder.finish("stop", None)
        parsed = events(blob)
        self.assertEqual(parsed[-2]["delta"]["stop_reason"], "end_turn")

    def test_max_tokens_finish_still_wins_for_text(self):
        builder = AnthropicStreamBuilder("agentaus", chunk_chars=0)
        blob = builder.start() + builder.text("ab") + builder.finish("length", None)
        parsed = events(blob)
        self.assertEqual(parsed[-2]["delta"]["stop_reason"], "max_tokens")

    def test_tool_block_keeps_tool_use_for_length_finish(self):
        builder = AnthropicStreamBuilder("agentaus", chunk_chars=0)
        blob = (
            builder.start()
            + builder.tool_use("call_1", "Read", '{"path":"a.py"}')
            + builder.finish("length", None)
        )
        parsed = events(blob)
        self.assertEqual(parsed[-2]["delta"]["stop_reason"], "tool_use")

    def test_ping_event_shape(self):
        self.assertEqual(events(AnthropicStreamBuilder.ping()), [{"type": "ping"}])


class TestToolCallAccumulator(unittest.TestCase):
    def test_fragmented_openai_tool_call_deltas_reassemble(self):
        acc = ToolCallAccumulator()
        acc.add([{"index": 0, "id": "call_1", "function": {"name": "Read", "arguments": '{"pa'}}])
        acc.add([{"index": 0, "function": {"arguments": 'th":"a.py"}'}}])
        calls = acc.drain()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"], '{"path":"a.py"}')

    def test_multiple_parallel_calls_keep_order(self):
        acc = ToolCallAccumulator()
        acc.add([
            {"index": 1, "id": "b", "function": {"name": "Two", "arguments": "{}"}},
            {"index": 0, "id": "a", "function": {"name": "One", "arguments": "{}"}},
        ])
        self.assertEqual([c["name"] for c in acc.drain()], ["One", "Two"])


if __name__ == "__main__":
    unittest.main()
