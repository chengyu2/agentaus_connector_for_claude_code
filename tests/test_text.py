"""Repairing typography a model applied to something that is not prose.

Observed in a live answer: a table of real documents where every filename carried
U+2011, the non-breaking hyphen. It renders identically to a hyphen and is not one, so
those paths could not be copied, clicked or searched for - and a citation check
correctly reported that none of the files existed.
"""

import unittest

from agentaus_bridge.text import normalise_for_display, normalise_identifiers


class IdentifiersAreRepaired(unittest.TestCase):
    def test_the_non_breaking_hyphen_that_broke_a_real_filename(self):
        self.assertEqual(
            normalise_for_display("See FIN‑2025‑26‑00616.pdf"),
            "See FIN-2025-26-00616.pdf")

    def test_the_plain_unicode_hyphen_too(self):
        self.assertEqual(normalise_for_display("a‐b"), "a-b")

    def test_non_breaking_spaces_become_spaces(self):
        self.assertEqual(normalise_for_display("Non breaking here"),
                         "Non breaking here")

    def test_a_code_span_gets_the_full_treatment(self):
        """Inside backticks everything is an identifier, so em dashes go too."""
        self.assertEqual(normalise_for_display("Call `plan—turn` now."),
                         "Call `plan-turn` now.")


class ProseIsLeftAlone(unittest.TestCase):
    """The narrower rule exists because the wider one damages readable text.

    `normalise_identifiers` flattens em dashes, which is right for a summary the model
    will re-read and wrong for a sentence a person is about to read.
    """

    def test_em_dashes_survive_in_a_sentence(self):
        sentence = "The bridge — which caps at 6 — is fine."
        self.assertEqual(normalise_for_display(sentence), sentence)

    def test_en_dashes_survive_in_a_range(self):
        self.assertEqual(normalise_for_display("A range of 5–10 items."),
                         "A range of 5–10 items.")

    def test_smart_quotes_survive(self):
        quoted = "He said “no” and meant it."
        self.assertEqual(normalise_for_display(quoted), quoted)

    def test_the_wider_rule_still_flattens_everything(self):
        """Unchanged behaviour where it is still the right rule."""
        self.assertEqual(normalise_identifiers("a — b"), "a - b")

    def test_empty_and_none_are_safe(self):
        self.assertEqual(normalise_for_display(""), "")
        self.assertIsNone(normalise_for_display(None))


class ItReachesTheClient(unittest.TestCase):
    def test_a_buffered_answer_is_repaired_on_the_way_out(self):
        from agentaus_bridge.translate import agentaus_response_to_anthropic
        message = agentaus_response_to_anthropic(
            {"choices": [{"message": {"content": "Open FIN‑2025.pdf"}}], "usage": {}},
            model="agentaus")
        text = "".join(b.get("text", "") for b in message.get("content", [])
                       if b.get("type") == "text")
        self.assertIn("FIN-2025.pdf", text)
        self.assertNotIn("‑", text)


if __name__ == "__main__":
    unittest.main()
