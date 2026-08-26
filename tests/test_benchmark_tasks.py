"""The benchmark harness itself, because a scorer's bugs look like model failures.

Every scoring bug found here was first mistaken for a result. The HumanEval harness
discarded helper functions the prompt supplied and marked three correct answers wrong; the
retrieval ground truth expected one file where two were right; the bracket function
reported an implausible pass@1 as "sits in: canonical solutions". A benchmark is code, and
untested code that produces numbers is worse than no numbers.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))

import baselines  # noqa: E402
import humaneval  # noqa: E402
import injection  # noqa: E402
import retrieval  # noqa: E402


class TestCodeExtraction(unittest.TestCase):
    def test_a_fenced_block_is_unwrapped(self):
        self.assertEqual(
            humaneval.extract_code("Here you go:\n```python\ndef f():\n    return 1\n```"),
            "def f():\n    return 1")

    def test_the_longest_block_wins_when_there_are_several(self):
        text = "```\nimport os\n```\nand\n```python\ndef f():\n    return 1\n```"
        self.assertIn("def f()", humaneval.extract_code(text))

    def test_bare_code_passes_through(self):
        self.assertEqual(humaneval.extract_code("def f():\n    return 1"),
                         "def f():\n    return 1")

    def test_nothing_is_not_an_error(self):
        self.assertEqual(humaneval.extract_code(""), "")
        self.assertEqual(humaneval.extract_code(None), "")


class TestTheScorerIsHonest(unittest.TestCase):
    """These are the cases that were once scored wrongly."""

    def setUp(self):
        try:
            self.problems = {p["task_id"]: p for p in humaneval.problems()}
        except Exception:
            self.skipTest("HumanEval dataset is not cached and cannot be fetched")

    def test_canonical_solutions_pass_their_own_tests(self):
        """If these fail, every subsequent failure is the harness's."""
        for task in ("HumanEval/0", "HumanEval/1", "HumanEval/4"):
            problem = self.problems[task]
            ok, why = humaneval.passes(problem,
                                       problem["prompt"] + problem["canonical_solution"])
            self.assertTrue(ok, f"{task}: {why}")

    def test_a_prompt_supplied_helper_is_still_available(self):
        """HumanEval/32 supplies `poly` for `find_zero`. Dropping it marked a correct
        answer wrong three times in a real run."""
        problem = self.problems["HumanEval/32"]
        self.assertIn("def poly", problem["prompt"])
        target_only = "def find_zero(xs: list):\n" + problem["canonical_solution"]
        ok, why = humaneval.passes(problem, target_only)
        self.assertTrue(ok, f"the prompt's helper was discarded again: {why}")

    def test_wrong_code_still_fails(self):
        problem = self.problems["HumanEval/0"]
        ok, _why = humaneval.passes(problem,
                                    "def has_close_elements(numbers, threshold):\n"
                                    "    return True\n")
        self.assertFalse(ok, "the scorer passed an answer that is simply wrong")

    def test_an_endless_loop_is_bounded(self):
        problem = self.problems["HumanEval/0"]
        ok, why = humaneval.passes(problem,
                                   "def has_close_elements(numbers, threshold):\n"
                                   "    while True:\n        pass\n", timeout=3)
        self.assertFalse(ok)
        self.assertEqual(why, "timeout")


class TestRetrievalScoring(unittest.TestCase):
    def test_a_correct_location_scores_one(self):
        got = retrieval.score("agentaus_bridge/gate.py:_PriorityGate",
                              {"agentaus_bridge/gate.py"}, {"_PriorityGate"})
        self.assertEqual((got["precision"], got["recall"], got["f1"]), (1.0, 1.0, 1.0))
        self.assertTrue(got["symbol"])

    def test_naming_extra_files_costs_precision_not_recall(self):
        got = retrieval.score("agentaus_bridge/gate.py:x\nwrong/other.py:y",
                              {"agentaus_bridge/gate.py"}, {"x"})
        self.assertEqual(got["recall"], 1.0)
        self.assertEqual(got["precision"], 0.5)

    def test_a_non_answer_scores_zero_rather_than_raising(self):
        for reply in ("", None, "I ran out of search rounds before finishing."):
            got = retrieval.score(reply, {"agentaus_bridge/gate.py"}, {"x"})
            self.assertEqual(got["f1"], 0.0, repr(reply))

    def test_every_question_names_files_that_exist(self):
        """A question whose ground truth points at a moved file scores everything zero."""
        for case in retrieval.QUESTIONS:
            for relative in case["files"]:
                self.assertTrue(os.path.isfile(os.path.join(ROOT, relative)),
                                f"{case['q'][:40]}... expects missing {relative}")

    def test_every_expected_symbol_is_present_in_its_file(self):
        for case in retrieval.QUESTIONS:
            haystack = "".join(
                open(os.path.join(ROOT, f), encoding="utf-8", errors="replace").read()
                for f in case["files"])
            for symbol in case["symbols"]:
                self.assertIn(symbol, haystack,
                              f"{symbol} is no longer in {sorted(case['files'])}")


class TestInjectionCases(unittest.TestCase):
    def test_each_case_has_a_detectable_tell(self):
        for case in injection.CASES:
            self.assertTrue(case["tell"])
            self.assertIn(case["tell"], case["content"].lower(),
                          f"{case['id']}: the planted instruction does not contain its "
                          f"own tell, so obeying it could not be detected")

    def test_the_tell_is_not_in_the_question(self):
        """Otherwise a model repeating the question would score as hijacked."""
        for case in injection.CASES:
            self.assertNotIn(case["tell"], case["q"].lower(), case["id"])


class TestBaselineReporting(unittest.TestCase):
    def test_an_implausible_score_is_called_out(self):
        text = baselines.bracket(0.982)
        self.assertIn("CONTAMINATION", text)
        self.assertIn("retrieval", text, "it should name the suites that still work")

    def test_an_ordinary_score_is_bracketed(self):
        self.assertIn("mid-tier", baselines.bracket(0.79))

    def test_a_poor_score_is_bracketed(self):
        self.assertTrue(baselines.bracket(0.10))

    def test_the_table_states_its_own_caveat(self):
        self.assertIn("orientation only", baselines.table())


if __name__ == "__main__":
    unittest.main(verbosity=2)
