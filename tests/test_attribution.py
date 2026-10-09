"""Commits written on an Agentaus turn credit Agentaus, not Claude.

Claude Code asks a model it does not know - `agentaus` included - to end commits with
`Co-Authored-By: Claude Code <noreply@anthropic.com>`, in a reminder beside the user's
first message. The bridge swaps that trailer, and nothing that is history.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge.attribution import credit  # noqa: E402

AGENTAUS = "Agentaus <theteam@trellisdata.com>"

# Verbatim from Claude Code 2.1.293 with `--model agentaus`.
REMINDER = (
    "<system-reminder>\nAttribution for git commits and pull requests you create from here "
    "on (this replaces Claude Code's own earlier attribution guidance, such as a previous "
    "copy of this reminder; the user's own instructions about these lines, such as a "
    "CLAUDE.md or memory rule, take precedence over this reminder, but do not add "
    "attribution lines this reminder leaves out):\n- End git commit messages with:\n"
    "Co-Authored-By: Claude Code <noreply@anthropic.com>\n- End pull request descriptions "
    "with:\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    "</system-reminder>\n"
)


def request(*blocks, system=None) -> dict:
    body = {"model": "agentaus", "messages": [{"role": "user", "content": list(blocks)}]}
    if system is not None:
        body["system"] = system
    return body


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def first_text(body: dict, index: int = 0) -> str:
    return body["messages"][0]["content"][index]["text"]


class ReminderTrailer(unittest.TestCase):
    def test_the_captured_reminder_credits_agentaus(self):
        out, swapped = credit(request(text(REMINDER), text("commit the readme")), AGENTAUS)
        self.assertEqual(swapped, 1)
        self.assertIn("Co-Authored-By: Agentaus <theteam@trellisdata.com>\n", first_text(out))
        self.assertNotIn("noreply@anthropic.com", first_text(out))
        self.assertEqual(first_text(out, 1), "commit the readme")

    def test_the_pr_footer_is_left_alone(self):
        out, _ = credit(request(text(REMINDER)), AGENTAUS)
        self.assertIn("🤖 Generated with [Claude Code](https://claude.com/claude-code)",
                      first_text(out))

    def test_a_model_named_trailer_is_swapped_too(self):
        reminder = REMINDER.replace("Claude Code <", "Claude Opus 5.5 <")
        out, swapped = credit(request(text(reminder)), AGENTAUS)
        self.assertEqual(swapped, 1)
        self.assertIn("Co-Authored-By: Agentaus <theteam@trellisdata.com>", first_text(out))

    def test_a_reminder_sent_as_a_plain_string_is_swapped(self):
        body = {"messages": [{"role": "user", "content": REMINDER + "commit it"}]}
        out, swapped = credit(body, AGENTAUS)
        self.assertEqual(swapped, 1)
        self.assertIn("Co-Authored-By: Agentaus", out["messages"][0]["content"])

    def test_the_input_is_not_mutated(self):
        body = request(text(REMINDER))
        credit(body, AGENTAUS)
        self.assertIn("noreply@anthropic.com", first_text(body))

    def test_a_second_pass_changes_nothing(self):
        once, _ = credit(request(text(REMINDER)), AGENTAUS)
        twice, swapped = credit(once, AGENTAUS)
        self.assertEqual(swapped, 0)
        self.assertIs(twice, once)


class ClaudeCodesOwnInstructions(unittest.TestCase):
    def test_the_system_prompt_is_swapped(self):
        system = [text("End the commit message with:\n"
                       "Co-Authored-By: Claude <noreply@anthropic.com>\n")]
        out, swapped = credit(request(text("hi"), system=system), AGENTAUS)
        self.assertEqual(swapped, 1)
        self.assertIn("Co-Authored-By: Agentaus", out["system"][0]["text"])

    def test_a_system_role_entry_is_swapped(self):
        body = {"messages": [
            {"role": "system", "content": "Co-Authored-By: Claude <noreply@anthropic.com>"},
            {"role": "user", "content": "commit"},
        ]}
        out, swapped = credit(body, AGENTAUS)
        self.assertEqual(swapped, 1)
        self.assertEqual(out["messages"][0]["content"], f"Co-Authored-By: {AGENTAUS}")


class HistoryIsLeftAsItIs(unittest.TestCase):
    LOG = "commit b45987c\n\n    Merge\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"

    def test_a_tool_result_is_untouched(self):
        body = {"messages": [
            {"role": "user", "content": [text(REMINDER)]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git log"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": self.LOG},
                {"type": "tool_result", "tool_use_id": "t2", "content": [text(self.LOG)]},
            ]},
        ]}
        out, swapped = credit(body, AGENTAUS)
        self.assertEqual(swapped, 1)
        self.assertEqual(out["messages"][2], body["messages"][2])

    def test_what_the_user_typed_is_untouched(self):
        pasted = "Reword this commit:\n\nFix it\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
        out, swapped = credit(request(text(pasted)), AGENTAUS)
        self.assertEqual(swapped, 0)
        self.assertEqual(first_text(out), pasted)

    def test_a_file_quoted_in_a_reminder_is_untouched(self):
        quoted = ("<system-reminder>\nNote: README.md was modified:\n"
                  "    12→Co-Authored-By: Claude <noreply@anthropic.com>\n</system-reminder>")
        out, swapped = credit(request(text(quoted)), AGENTAUS)
        self.assertEqual(swapped, 0)
        self.assertEqual(first_text(out), quoted)


class WhenThereIsNothingToSwap(unittest.TestCase):
    def test_a_trailer_set_in_settings_is_left_alone(self):
        own = REMINDER.replace("Claude Code <noreply@anthropic.com>", AGENTAUS)
        body = request(text(own))
        out, swapped = credit(body, AGENTAUS)
        self.assertEqual(swapped, 0)
        self.assertIs(out, body)

    def test_an_empty_coauthor_turns_it_off(self):
        body = request(text(REMINDER))
        out, swapped = credit(body, "")
        self.assertEqual(swapped, 0)
        self.assertIs(out, body)


if __name__ == "__main__":
    unittest.main()
