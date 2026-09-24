"""The compensation applied to Agentaus turns.

Two properties matter beyond the wording itself: it must reach Agentaus turns only,
and the review pass must never discard a good answer because the reviewer replied in
an unexpected shape.
"""

from __future__ import annotations

import os
import sys
import unittest
from xml_style import setUpModule, tearDownModule  # noqa: E402,F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import augment  # noqa: E402
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


class TestTheAnalysisReportingGate(unittest.TestCase):
    """Rule 6 asks for no summary, and on a turn that computed something the summary is
    the deliverable. Measured on four data tasks with identical correct results: the
    useful reply ran 1,800-2,300 characters and the thin one ran 119.

    The gate is structural - what the turn *ran*, not what the user typed. Both signals
    are required because either alone is a false positive: `pytest -q` executes code and
    analyses nothing, and reading a CSV is data with no computation over it.
    """

    @staticmethod
    def turn(*calls):
        return {
            "tools": [{"name": "Bash"}],
            "messages": [{"role": "assistant", "content": [
                {"type": "tool_use", "id": str(i), "name": n, "input": p}
                for i, (n, p) in enumerate(calls)
            ]}],
        }

    def test_a_script_over_a_spreadsheet_counts(self):
        body = self.turn(
            ("Write", {"file_path": "/w/a.py", "content": "import pandas as pd\nd = pd.read_excel('r.xlsx')"}),
            ("Bash", {"command": "python3 a.py"}),
        )
        self.assertTrue(augment.ran_an_analysis(body))

    def test_the_two_signals_may_arrive_in_different_calls(self):
        """The script that imports pandas is written by one call and run by the next."""
        body = self.turn(
            ("Write", {"file_path": "/w/a.py", "content": "import pandas"}),
            ("Bash", {"command": "python3 /w/a.py"}),
        )
        self.assertTrue(augment.ran_an_analysis(body))

    def test_r_counts_too(self):
        body = self.turn(("Write", {"file_path": "/w/m.R", "content": "d <- read.csv('g.csv')"}))
        self.assertTrue(augment.ran_an_analysis(body))

    def test_running_the_test_suite_is_not_an_analysis(self):
        self.assertFalse(augment.ran_an_analysis(self.turn(("Bash", {"command": "pytest -q"}))))

    def test_a_management_command_is_not_an_analysis(self):
        """`python3 manage.py migrate` executes code and computes nothing over data."""
        self.assertFalse(
            augment.ran_an_analysis(self.turn(("Bash", {"command": "python3 manage.py migrate"})))
        )

    def test_writing_ordinary_python_is_not_an_analysis(self):
        body = self.turn(("Write", {"file_path": "/w/server.py", "content": "from fastapi import FastAPI"}))
        self.assertFalse(augment.ran_an_analysis(body))

    def test_reading_a_csv_is_not_computing_over_it(self):
        self.assertFalse(augment.ran_an_analysis(self.turn(("Read", {"file_path": "/w/d.csv"}))))

    def test_a_turn_with_no_tools_gets_none_of_it(self):
        self.assertNotIn("Reporting an analysis", augment.guidance_for({"messages": []}))

    def test_the_contract_is_added_only_when_it_applies(self):
        analysis = self.turn(("Bash", {"command": "python3 -c 'import pandas'"}))
        ordinary = self.turn(("Bash", {"command": "git status"}))
        self.assertIn("Reporting an analysis", augment.guidance_for(analysis))
        self.assertNotIn("Reporting an analysis", augment.guidance_for(ordinary))

    def test_it_tells_the_model_the_conciseness_rule_does_not_apply_here(self):
        """Without this the two blocks contradict each other and the shorter wins."""
        body = self.turn(("Bash", {"command": "python3 -c 'import pandas'"}))
        self.assertIn("does not apply here", augment.guidance_for(body))


class TestTheAnalysisGateSeesWhatWasAsked(unittest.TestCase):
    """The data signal used to come only from tool inputs, so whether the reporting
    contract arrived depended on how the model happened to create its script:
    `python3 -c "import pandas"` fired it, `python3 analyze.py` did not.
    """

    @staticmethod
    def body(said: str, cmd: str):
        return {"tools": [{"name": "Bash"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": said}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash",
             "input": {"command": cmd}}]}]}

    def test_running_a_script_counts_when_the_request_named_a_dataset(self):
        b = self.body("Analyse rnd_2022-23.xlsx and report the Gini", "python3 analyze.py")
        self.assertTrue(augment.ran_an_analysis(b))

    def test_it_still_refuses_a_turn_with_no_data_anywhere(self):
        b = self.body("Fix the retry helper", "python3 manage.py migrate")
        self.assertFalse(augment.ran_an_analysis(b))

    def test_naming_a_dataset_without_running_anything_is_not_an_analysis(self):
        b = {"tools": [{"name": "Bash"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": "what is in data.csv?"}]}]}
        self.assertFalse(augment.ran_an_analysis(b))
