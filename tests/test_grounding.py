"""Checking an answer against what the turn actually did.

Ordinary self-review has to sit out tool-derived turns: a reviewer shown only the request
and the answer reads a well-grounded reply as an unverified claim, and rewrites a correct
answer into a refusal. That left the one kind of turn where a claim can outrun the
evidence with nothing watching it.

Observed on a real session: asked to survey a repository, the model ran one `find`, was
handed a 2 KB preview of a 1.6 MB listing, and then wrote that "the repo's `_notes.md`
usually enforces a single consistent fit label" and that nothing breached "the
prohibitions live once rule" - a file it never opened and a policy that does not exist.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import persisted  # noqa: E402
from agentaus_bridge.augment import (  # noqa: E402
    GROUNDING_INSTRUCTION,
    grounding_verdict,
    worth_grounding_check,
    worth_reviewing_turn,
)
from agentaus_bridge.config import settings  # noqa: E402


def tool_turn(result="some output"):
    return {"messages": [
        {"role": "user", "content": "survey the repo"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "find ."}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": result}]},
    ]}


class TestWhenGroundingRuns(unittest.TestCase):
    def test_it_runs_exactly_where_review_cannot(self):
        turn = tool_turn()
        self.assertFalse(worth_reviewing_turn(turn),
                         "review is meant to sit out a tool turn")
        self.assertTrue(worth_grounding_check(turn, "x" * 600),
                        "so grounding must not sit it out too")

    def test_a_prose_turn_is_left_to_ordinary_review(self):
        prose = {"messages": [{"role": "user", "content": "write a function"}]}
        self.assertTrue(worth_reviewing_turn(prose))
        self.assertFalse(worth_grounding_check(prose, "x" * 600))

    def test_a_short_answer_is_not_worth_a_call(self):
        self.assertFalse(worth_grounding_check(tool_turn(), "done"))

    def test_a_turn_that_ran_a_tool_earlier_still_qualifies(self):
        """The final turn of an agentic loop is where the fabrication appears."""
        turn = tool_turn()
        turn["messages"].append({"role": "assistant", "content": "here is my survey"})
        turn["messages"].append({"role": "user", "content": "and the rest?"})
        self.assertTrue(worth_grounding_check(turn, "x" * 600))


class TestReadingTheVerdict(unittest.TestCase):
    def test_grounded_means_no_change(self):
        self.assertEqual(grounding_verdict("GROUNDED"), "")
        self.assertEqual(grounding_verdict("  grounded  "), "")

    def test_gaps_are_returned(self):
        gaps = grounding_verdict(
            "GAPS\n- claims _notes.md enforces a label; never read it\n- cites a policy")
        self.assertIn("_notes.md", gaps)
        self.assertIn("cites a policy", gaps)
        self.assertNotIn("GAPS", gaps)

    def test_anything_unreadable_counts_as_grounded(self):
        """A check that cannot be read must not be allowed to rewrite an answer."""
        for reply in ("hmm maybe", "", None, "I think it's fine?"):
            self.assertEqual(grounding_verdict(reply), "", repr(reply))

    def test_the_prompt_shows_the_reviewer_what_ran(self):
        prompt = GROUNDING_INSTRUCTION.format(
            ledger="- Bash(find .) -> ok", answer="the repo's _notes.md enforces labels")
        self.assertIn("Bash(find .)", prompt)
        self.assertIn("never read", prompt.lower())


class TestRestoringTruncatedOutput(unittest.TestCase):
    """A 2 KB preview of a 1.6 MB listing is worse than useless: the model answers from a
    fragment and invents the rest. The client saved the real thing; the bridge can read it."""

    def setUp(self):
        self._saved = settings.agentaus_restore_persisted
        settings.agentaus_restore_persisted = True

    def tearDown(self):
        settings.agentaus_restore_persisted = self._saved

    def _preview(self, path):
        return (f"<persisted-output>\nOutput too large (1.6MB). Full output saved to: "
                f"{path}\n\nPreview (first 2KB):\n/a/one.md\n/a/two.md\n...\n"
                f"</persisted-output>")

    def test_the_real_output_replaces_the_preview(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.txt")
            with open(path, "w") as fh:
                fh.write("\n".join(f"/repo/file{i}.md" for i in range(3000)))
            body = {"messages": [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t",
                 "content": self._preview(path)}]}]}
            got = persisted.restore(body)["messages"][0]["content"][0]["content"]
        self.assertIn("file2999.md", got, "only the preview survived")
        self.assertIn("the bridge read that file", got.lower())

    def test_an_ordinary_result_is_untouched(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "x = 1"}]}]}
        self.assertIs(persisted.restore(body), body)

    def test_a_preview_naming_a_missing_file_is_left_alone(self):
        """A preview is bad; a preview replaced by nothing is worse."""
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": self._preview("/nonexistent/out.txt")}]}]}
        self.assertIs(persisted.restore(body), body)

    def test_an_enormous_file_is_capped_and_says_so(self):
        import tempfile
        previous = settings.agentaus_restore_max_bytes
        settings.agentaus_restore_max_bytes = 5000
        try:
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "out.txt")
                with open(path, "w") as fh:
                    fh.write("\n".join(f"/repo/file{i}.md" for i in range(20000)))
                body = {"messages": [{"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t",
                     "content": self._preview(path)}]}]}
                got = persisted.restore(body)["messages"][0]["content"][0]["content"]
        finally:
            settings.agentaus_restore_max_bytes = previous
        self.assertIn("first 5,000 of", got)
        self.assertIn("Narrow the command", got)

    def test_it_is_a_no_op_when_disabled(self):
        settings.agentaus_restore_persisted = False
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": self._preview("/tmp/x")}]}]}
        self.assertIs(persisted.restore(body), body)

    def test_detection_needs_a_truncation_marker_not_just_a_path(self):
        """A result that merely mentions a path is not a truncated result."""
        self.assertFalse(persisted.looks_truncated(
            "I wrote the report; it is saved to: /tmp/report.md"))


class TestBashIsRestrictedForSearching(unittest.TestCase):
    def test_the_restriction_leads_the_description(self):
        from agentaus_bridge import tools as bt
        from agentaus_bridge.translate import inject_bridge_tools
        body = inject_bridge_tools(
            {"tools": [{"name": "Bash", "description": "Run a shell command.",
                        "input_schema": {"type": "object"}}]},
            [bt.SEARCH_SCHEMA])
        bash = next(t for t in body["tools"] if t["name"] == "Bash")
        self.assertTrue(bash["description"].startswith("RESTRICTED"),
                        "a caveat after a paragraph loses to a strong prior")
        self.assertIn("agentaus_search", bash["description"])
        self.assertIn("Run a shell command.", bash["description"],
                      "the original description must survive")

    def test_running_things_is_still_endorsed(self):
        from agentaus_bridge.augment import tool_selection
        advice = tool_selection({"tools": [{"name": "Bash"}]})
        self.assertIn("tests, builds, git", advice)


if __name__ == "__main__":
    unittest.main(verbosity=2)
