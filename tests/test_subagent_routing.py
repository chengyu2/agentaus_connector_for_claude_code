"""A subagent runs on the model its session is on, not on Claude by default.

Measured on Claude Code 2.1.281: a session on `agentaus` started an Explore subagent, and
the subagent's requests asked for `claude-opus-5-5` - Claude Code cannot inherit a custom
model id - so the bridge forwarded them to Anthropic. The subagent's requests are marked
(`x-claude-code-agent-id`, `cc_is_subagent=true`) and share the session id, so the bridge
sends them where the session's main loop last went.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import server, sessions  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge.translate import frame_skill_text  # noqa: E402


class _Request:
    def __init__(self, headers):
        self.headers = headers


def main_loop(model, session="s1"):
    body = {"model": model, "tools": [{"name": "Read"}], "messages": []}
    return server._route(_Request({"x-claude-code-session-id": session}), body, model,
                         main_loop=True)


def subagent(model="claude-opus-5-5", session="s1", marker="header"):
    headers = {"x-claude-code-session-id": session}
    body = {"model": model, "tools": [{"name": "Grep"}], "messages": []}
    if marker == "header":
        headers["x-claude-code-agent-id"] = "a7b2"
    else:
        body["system"] = [{"type": "text", "text": "x-anthropic-billing-header: "
                           "cc_version=2.1.281; cc_is_subagent=true;"}]
    return server._route(_Request(headers), body, model, main_loop=True)


class SubagentsFollowTheirSession(unittest.TestCase):
    def setUp(self):
        sessions.reset()
        self._saved = (settings.agentaus_subagents_follow_session, settings.force_all_to_agentaus,
                       settings.passthrough_enabled)
        settings.agentaus_subagents_follow_session = True
        settings.force_all_to_agentaus = False
        settings.passthrough_enabled = True

    def tearDown(self):
        (settings.agentaus_subagents_follow_session, settings.force_all_to_agentaus,
         settings.passthrough_enabled) = self._saved
        sessions.reset()

    def test_a_subagent_of_an_agentaus_session_runs_on_agentaus(self):
        self.assertEqual(main_loop("agentaus"), (True, False))
        self.assertEqual(subagent(), (True, True))

    def test_the_billing_line_marks_a_subagent_too(self):
        main_loop("agentaus")
        self.assertEqual(subagent(marker="system"), (True, True))

    def test_switching_the_session_to_claude_sends_its_subagents_to_claude(self):
        main_loop("agentaus")
        main_loop("claude-opus-5")
        self.assertEqual(subagent(), (False, False))

    def test_switching_back_to_agentaus_takes_them_back(self):
        main_loop("claude-opus-5")
        main_loop("agentaus")
        self.assertEqual(subagent(), (True, True))

    def test_other_sessions_are_not_affected(self):
        main_loop("agentaus", session="s1")
        self.assertEqual(subagent(session="s2"), (False, False))

    def test_a_subagent_does_not_change_its_session(self):
        main_loop("claude-opus-5")
        subagent(model="agentaus")
        self.assertEqual(sessions.main_loop_on_agentaus("s1"), False)

    def test_a_call_without_tools_does_not_change_the_session(self):
        # Title and summary calls carry no tools and may use another model.
        main_loop("agentaus")
        server._route(_Request({"x-claude-code-session-id": "s1"}),
                      {"model": "claude-haiku-4-5", "messages": []}, "claude-haiku-4-5",
                      main_loop=True)
        self.assertEqual(subagent(), (True, True))

    def test_off_means_by_model_id_alone(self):
        settings.agentaus_subagents_follow_session = False
        main_loop("agentaus")
        self.assertEqual(subagent(), (False, False))

    def test_the_session_table_is_bounded(self):
        for i in range(sessions._MAX_SESSIONS + 10):
            sessions.note_main_loop(f"s{i}", True)
        self.assertLessEqual(len(sessions._main_loop), sessions._MAX_SESSIONS)
        self.assertIsNone(sessions.main_loop_on_agentaus("s0"))


class ALoadedSkillIsAProcedureToFollow(unittest.TestCase):
    """Observed: Agentaus passed a .docx as a skill's argument, expecting the document
    back, got a procedure, did not follow it, and loaded the same skill three times more."""

    def test_the_procedure_is_labelled_as_instructions_to_carry_out(self):
        text = frame_skill_text("Base directory for this skill: /p/.claude/skills/read-documents"
                                "\n\n# Reading office documents")
        self.assertIn("read-documents", text.splitlines()[0])
        self.assertIn("not a result", text)
        self.assertIn("Carry out its steps now", text)
        self.assertTrue(text.rstrip().endswith("# Reading office documents"))

    def test_a_repeat_load_is_told_not_to_repeat(self):
        text = frame_skill_text("Skill /read-documents is already loaded above; "
                                "instructions unchanged.")
        self.assertIn("Do not call `Skill` for it again", text)

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(frame_skill_text("Where is the retry?"), "Where is the retry?")


if __name__ == "__main__":
    unittest.main()
