"""Tool calls the model wrote as text instead of sending as tool calls.

The whole content of one live answer, verbatim:

    <|start|>assistant<|channel|>commentary to=functions.agentaus_web_search
    {"query":"SC-NFR-11 protocol secure data in transit"}<|call|>

That is Harmony channel format arriving as assistant text rather than as a structured
tool_calls field. Nothing in the bridge knew what it was, so the tool never ran, the
markup was shown to the user as the answer, and the turn was spent.
"""

import logging
import os
import sys
import unittest
from unittest import mock

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import harmony, server, throttle  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge import tool_handling, upstream_http  # noqa: E402

logging.getLogger("agentaus-bridge").setLevel(logging.CRITICAL)

OBSERVED = ('<|start|>assistant<|channel|>commentary to=functions.agentaus_web_search'
            '{"query":"SC-NFR-11 protocol secure data in transit Trellis Data tender"}'
            '<|call|>')


class TheObservedFailure(unittest.TestCase):
    def test_the_call_is_recovered(self):
        text, calls = harmony.extract(OBSERVED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "agentaus_web_search")
        self.assertIn("SC-NFR-11", calls[0]["arguments"])

    def test_nothing_of_the_markup_is_left_to_show_the_user(self):
        text, _ = harmony.extract(OBSERVED)
        self.assertEqual(text, "")


class Shapes(unittest.TestCase):
    """Harmony has several spellings and the model that got here is off the happy path."""

    def test_the_canonical_form_with_constrain_and_message(self):
        _, calls = harmony.extract(
            '<|start|>assistant<|channel|>commentary to=functions.Read '
            '<|constrain|>json<|message|>{"file_path":"/a/b.py"}<|call|>')
        self.assertEqual([c["name"] for c in calls], ["Read"])

    def test_prose_before_a_call_is_kept(self):
        text, calls = harmony.extract(
            'Let me look that up. <|channel|>commentary '
            'to=functions.agentaus_search{"query":"gate","path":"/r"}<|call|>')
        self.assertEqual(text, "Let me look that up.")
        self.assertEqual(len(calls), 1)

    def test_several_calls_in_one_answer(self):
        _, calls = harmony.extract(
            'to=functions.Read{"file_path":"/a"}<|call|> '
            'to=functions.Read{"file_path":"/b"}<|call|>')
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["arguments"], calls[1]["arguments"])

    def test_a_call_cut_off_before_its_terminator(self):
        """Generation stopping early must not lose the call it had already written."""
        _, calls = harmony.extract(
            '<|start|>assistant<|channel|>commentary to=functions.Read{"file_path":"/a"}')
        self.assertEqual(len(calls), 1)

    def test_nested_arguments_are_not_truncated(self):
        _, calls = harmony.extract(
            'to=functions.AskUserQuestion'
            '{"questions":[{"q":"x","options":[{"label":"a"},{"label":"b"}]}]}<|call|>')
        import json
        parsed = json.loads(calls[0]["arguments"])
        self.assertEqual(len(parsed["questions"][0]["options"]), 2)

    def test_unparseable_markup_does_not_loop_or_raise(self):
        text, calls = harmony.extract("to=functions. to=functions.<|call|>")
        self.assertEqual(calls, [])
        self.assertIsInstance(text, str)


class OrdinaryTextIsUntouched(unittest.TestCase):
    def test_a_normal_answer_passes_through_unchanged(self):
        answer = "The bridge caps concurrency at 6 in gate.py:18."
        text, calls = harmony.extract(answer)
        self.assertEqual(text, answer)
        self.assertEqual(calls, [])

    def test_the_words_final_and_commentary_survive_in_prose(self):
        """Channel labels are only labels where the protocol puts them."""
        for answer in ("The final answer is in gate.py.",
                       "See the commentary in the docstring."):
            text, _ = harmony.extract(answer)
            self.assertEqual(text, answer)

    def test_a_final_channel_wrapper_leaves_only_its_content(self):
        text, calls = harmony.extract(
            "<|start|>assistant<|channel|>final<|message|>The answer is 42.<|end|>")
        self.assertEqual(text, "The answer is 42.")
        self.assertEqual(calls, [])


class ServerIntegration(unittest.TestCase):
    def test_a_non_streaming_response_has_the_markup_replaced_by_calls(self):
        data = {"choices": [{"message": {"content": OBSERVED, "tool_calls": None}}]}
        cleaned, calls = tool_handling._recover_harmony_calls(data)
        self.assertEqual(len(calls), 1)
        content = cleaned["choices"][0]["message"]["content"]
        self.assertFalse(content, "markup must not survive into the client's answer")

    def test_the_recovered_calls_are_put_back_into_the_response(self):
        """The response must carry the calls, not only return them to the caller.

        `_with_coerced_calls` writes name/argument corrections back into
        `message.tool_calls`, and the translator builds the client's tool_use blocks
        from it - calls left only in the returned dicts vanish at both, and an answer
        stripped to nothing then reads as empty and is replaced by the fallback.
        """
        data = {"choices": [{"finish_reason": "stop",
                             "message": {"content": OBSERVED, "tool_calls": None}}]}
        cleaned, calls = tool_handling._recover_harmony_calls(data)
        self.assertEqual(len(calls), 1)
        message = cleaned["choices"][0]["message"]
        [call] = message["tool_calls"]
        self.assertEqual(call["id"], calls[0]["id"])
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], calls[0]["name"])
        self.assertEqual(call["function"]["arguments"], calls[0]["arguments"])
        self.assertEqual(cleaned["choices"][0]["finish_reason"], "tool_calls")

    def test_an_ordinary_response_is_returned_unchanged(self):
        data = {"choices": [{"message": {"content": "Plain answer."}}]}
        same, calls = tool_handling._recover_harmony_calls(data)
        self.assertEqual(calls, [])
        self.assertIs(same, data)

    def test_an_empty_response_does_not_raise(self):
        self.assertEqual(tool_handling._recover_harmony_calls({})[1], [])
        self.assertEqual(tool_handling._recover_harmony_calls({"choices": []})[1], [])


class EndToEndNonStreaming(unittest.TestCase):
    """A recovered call must survive the whole buffered turn, not just the helper.

    The helper tests prove the markup is parsed out of the text; this runs the handler
    itself, as the live failure arrived: Agentaus answered 200 with a client tool call
    written as Harmony markup. The client must get a tool_use block for it, corrected
    in name and arguments, with stop_reason tool_use - not an empty turn replaced by
    the fallback answer.
    """

    CALL = ('<|start|>assistant<|channel|>commentary to=functions.read '
            '<|constrain|>json<|message|>{"file_path": "/a/b.py"}<|call|>')

    BODY = {
        "model": "agentaus",
        "max_tokens": 100,
        "tools": [{"name": "Read", "description": "Read a file.",
                   "input_schema": {"type": "object",
                                    "properties": {"file_path": {"type": "string"}},
                                    "required": ["file_path"]}}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Read /a/b.py"}]}],
        "stream": False,
    }

    def setUp(self):
        self.saved = {k: getattr(settings, k) for k in (
            "agentaus_api_key", "retry_backoff_seconds", "max_retries",
            "agentaus_thinking", "agentaus_self_review", "agentaus_grounding_check",
            "agentaus_syntax_check", "agentaus_upstream_concurrency",
            "agentaus_tool_rounds", "agentaus_correction_rounds")}
        settings.agentaus_api_key = "test-key"
        settings.retry_backoff_seconds = 0.01
        settings.max_retries = 0
        settings.agentaus_thinking = False
        settings.agentaus_self_review = False
        settings.agentaus_grounding_check = False
        settings.agentaus_syntax_check = False
        settings.agentaus_upstream_concurrency = 10
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.saved.items():
            setattr(settings, k, v)
        throttle.reset()

    def post(self, responses):
        """Run the buffered handler against a stand-in upstream that replies in turn."""
        seen = []

        async def upstream(client, url, *, json_body=None, **_):
            seen.append(json_body)
            return httpx.Response(200, json=responses[min(len(seen) - 1, len(responses) - 1)])

        with mock.patch.object(upstream_http, "_post_with_retry", upstream), \
                TestClient(server.app) as client:
            r = client.post("/v1/messages", json=self.BODY)
        return r, seen

    def test_a_harmony_text_call_reaches_the_client_as_tool_use(self):
        upstream = {"choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": self.CALL}}]}
        r, seen = self.post([upstream])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(seen), 1,
                         "the recovered call was lost and the empty-answer fallback ran")
        message = r.json()
        tool_uses = [b for b in message["content"] if b["type"] == "tool_use"]
        self.assertEqual(len(tool_uses), 1, message["content"])
        self.assertEqual(tool_uses[0]["name"], "Read",
                         "the mangled name `read` must reach the client corrected")
        self.assertEqual(tool_uses[0]["input"], {"file_path": "/a/b.py"})
        self.assertEqual(message["stop_reason"], "tool_use")
        for block in message["content"]:
            if block["type"] == "text":
                self.assertNotIn("<|", block["text"], "markup must not be shown as prose")

    def test_a_harmony_bridge_call_still_runs_its_rounds(self):
        search = ('<|start|>assistant<|channel|>commentary to=functions.agentaus_search '
                  '<|constrain|>json<|message|>{"query":"gate"}<|call|>')
        r, seen = self.post([
            {"choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": search}}]},
            {"choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "Found it in gate.py."}}]},
        ])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(seen), 2, "the bridge-owned call must run through its round")
        message = r.json()
        self.assertEqual([b["type"] for b in message["content"]], ["text"])
        self.assertEqual(message["content"][0]["text"], "Found it in gate.py.")
        self.assertEqual(message["stop_reason"], "end_turn")


if __name__ == "__main__":
    unittest.main()
