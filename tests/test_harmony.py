"""Tool calls the model wrote as text instead of sending as tool calls.

The whole content of one live answer, verbatim:

    <|start|>assistant<|channel|>commentary to=functions.agentaus_web_search
    {"query":"SC-NFR-11 protocol secure data in transit"}<|call|>

That is Harmony channel format arriving as assistant text rather than as a structured
tool_calls field. Nothing in the bridge knew what it was, so the tool never ran, the
markup was shown to the user as the answer, and the turn was spent.
"""

import logging
import unittest

from agentaus_bridge import harmony, server

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
        cleaned, calls = server._recover_harmony_calls(data)
        self.assertEqual(len(calls), 1)
        content = cleaned["choices"][0]["message"]["content"]
        self.assertFalse(content, "markup must not survive into the client's answer")

    def test_an_ordinary_response_is_returned_unchanged(self):
        data = {"choices": [{"message": {"content": "Plain answer."}}]}
        same, calls = server._recover_harmony_calls(data)
        self.assertEqual(calls, [])
        self.assertIs(same, data)

    def test_an_empty_response_does_not_raise(self):
        self.assertEqual(server._recover_harmony_calls({})[1], [])
        self.assertEqual(server._recover_harmony_calls({"choices": []})[1], [])


if __name__ == "__main__":
    unittest.main()
