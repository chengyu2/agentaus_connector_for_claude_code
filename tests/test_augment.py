"""The compensation applied to Agentaus turns.

Two properties matter beyond the wording itself: it must reach Agentaus turns only,
and the review pass must never discard a good answer because the reviewer replied in
an unexpected shape.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge.augment import (  # noqa: E402
    CORE_GUIDANCE,
    TOOL_GUIDANCE,
    declared_verdict,
    guidance_for,
    review_says_ok,
    with_guidance,
    worth_reviewing,
)


class TestGuidanceSelection(unittest.TestCase):
    def test_tool_discipline_is_omitted_when_no_tools_are_offered(self):
        """Advice about not re-calling tools is unusable on a plain generation turn."""
        notes = guidance_for({"messages": []})

        self.assertIn("Before writing any code", notes)
        self.assertNotIn("Do not re-run a tool", notes)

    def test_tool_discipline_is_included_when_tools_are_offered(self):
        notes = guidance_for({"tools": [{"name": "Read"}]})

        self.assertIn("Do not re-run a tool", notes)

    def test_core_guidance_is_always_present(self):
        for body in ({}, {"tools": [{"name": "Read"}]}):
            self.assertIn(CORE_GUIDANCE.strip()[:40], guidance_for(body))

    def test_guidance_names_the_observed_failure_modes(self):
        """Each instruction exists because of a behaviour actually seen from Agentaus."""
        combined = CORE_GUIDANCE + TOOL_GUIDANCE

        self.assertIn("empty", combined)
        self.assertIn("negatives", combined)
        self.assertIn("re-run a tool", combined)
        self.assertIn("guess", combined.lower())


class TestSystemPromptComposition(unittest.TestCase):
    def test_existing_system_prompt_is_preserved(self):
        """Claude Code's own prompt is the agent; the notes only supplement it."""
        out = with_guidance("ORIGINAL PROMPT", {})

        self.assertTrue(out.startswith("ORIGINAL PROMPT"))
        self.assertIn("Before writing any code", out)

    def test_block_form_system_prompts_are_appended_to(self):
        blocks = [{"type": "text", "text": "ORIGINAL"}]
        out = with_guidance(blocks, {})

        self.assertEqual(out[0], blocks[0])
        self.assertEqual(len(out), 2)
        self.assertIn("Before writing any code", out[1]["text"])

    def test_absent_system_prompt_yields_just_the_notes(self):
        self.assertIn("Before writing any code", with_guidance(None, {}))

    def test_original_is_not_mutated(self):
        blocks = [{"type": "text", "text": "ORIGINAL"}]
        with_guidance(blocks, {})

        self.assertEqual(len(blocks), 1, "the caller's list was modified in place")


class TestReviewVerdict(unittest.TestCase):
    def test_plain_approval_is_recognised(self):
        for verdict in ("OK", "ok", " OK ", "OK.", "**OK**", "`OK`"):
            self.assertTrue(review_says_ok(verdict), f"{verdict!r} not read as approval")

    def test_real_defects_are_not_read_as_approval(self):
        verdict = "The function fails on an empty list: median([]) raises IndexError."

        self.assertFalse(review_says_ok(verdict))

    def test_empty_review_keeps_the_original_answer(self):
        """A reviewer that returns nothing must not trigger a rewrite."""
        self.assertTrue(review_says_ok(""))

    def test_a_long_reply_starting_with_ok_is_still_treated_as_defects(self):
        verdict = "OK, but there is a real problem: the empty case is unhandled " * 3

        self.assertFalse(review_says_ok(verdict))


class TestReviewThreshold(unittest.TestCase):
    def test_short_answers_are_not_worth_a_round_trip(self):
        self.assertFalse(worth_reviewing("Done."))
        self.assertFalse(worth_reviewing(""))

    def test_substantial_answers_are_reviewed(self):
        self.assertTrue(worth_reviewing("x" * 250))

    def test_threshold_is_configurable(self):
        self.assertTrue(worth_reviewing("x" * 50, min_chars=10))
        self.assertFalse(worth_reviewing("x" * 50, min_chars=100))


class TestVerifyDontAssume(unittest.TestCase):
    """The guidance must tell the model to find out rather than guess.

    Assuming is the failure that produces confidently wrong answers, which are worse
    than an admitted gap - the user acts on them.
    """

    def test_core_guidance_says_to_verify(self):
        notes = guidance_for({})

        self.assertIn("Find out rather than assume", notes)
        self.assertIn("say so", notes, "must tell the model to admit what it cannot check")

    def test_tool_guidance_warns_against_pattern_matching_documents(self):
        notes = guidance_for({"tools": [{"name": "Read"}]})

        self.assertIn("read it and interpret it properly", notes)


class TestDeclaredVerdict(unittest.TestCase):
    """Reading a stated verdict beats sniffing prose.

    "OK, but the empty case is broken" and a bare "**OK**" both defeat a substring
    check, and they fail in opposite directions - one discards a good answer, the other
    ships a broken one.
    """

    def test_stated_verdicts_are_read(self):
        self.assertIs(declared_verdict("VERDICT: OK"), True)
        self.assertIs(declared_verdict("VERDICT: DEFECTS\n- empty case unhandled"), False)

    def test_markdown_around_the_verdict_is_tolerated(self):
        self.assertIs(declared_verdict("**VERDICT: OK**"), True)
        self.assertIs(declared_verdict("`VERDICT: DEFECTS`\n- x"), False)

    def test_a_verdict_later_in_the_reply_is_found(self):
        self.assertIs(declared_verdict("Here is my review.\nVERDICT: OK"), True)

    def test_missing_verdict_returns_none_so_the_caller_can_ask(self):
        """None is the signal to adjudicate with a model call, not to guess."""
        self.assertIsNone(declared_verdict("The code looks broadly fine to me."))

    def test_the_ambiguous_case_that_motivated_this(self):
        """A substring check reads this as approval; it is the opposite."""
        review = "OK, but there is a real problem: median([]) raises IndexError."

        self.assertIsNone(declared_verdict(review),
                          "must defer rather than guess at an unformatted review")

    def test_empty_review_is_treated_as_sound(self):
        self.assertIs(declared_verdict(""), True)

    def test_fallback_still_works_without_a_model(self):
        self.assertTrue(review_says_ok("VERDICT: OK"))
        self.assertFalse(review_says_ok("VERDICT: DEFECTS\n- broken"))


if __name__ == "__main__":
    unittest.main()
