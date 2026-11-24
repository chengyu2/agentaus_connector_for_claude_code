"""An opted-in ("ultracode") Agentaus main loop whose `Workflow` tool is still deferred.

Measured (24 Nov 2026, Claude Code 2.1.331): max effort opted the session in, Claude Code's
reminder said "use the Workflow tool", and Workflow was never on the wire - it stays deferred
until loaded. The bridge treated the session as a graph main loop anyway: it skipped the
planning pass, would have hidden `Agent`, and its guidance pointed only at Workflow. Agentaus
worked alone for 32 turns. Also: a Windows working directory was never read at all.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import augment, server, sessions  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402


def tool(name):
    return {"name": name, "description": "", "input_schema": {"type": "object"}}


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def opted(*tools):
    return {"model": "agentaus", "tools": [tool(t) for t in tools],
            "messages": [user("ultracode: fix the compaction bugs in the bridge")]}


class WorkflowStillDeferred(unittest.TestCase):
    def setUp(self):
        self._graph = settings.agentaus_workflow_graph
        settings.agentaus_workflow_graph = True

    def tearDown(self):
        settings.agentaus_workflow_graph = self._graph

    def offered(self, body):
        return {t["name"] for t in augment.focus_tools(body)[0]["tools"]}

    def test_agent_stays_on_the_wire_until_workflow_is(self):
        self.assertIn("Agent", self.offered(opted("Agent", "Read", "Edit")))
        # Workflow loaded: the graph orchestrates, so Agent is held back as before.
        self.assertNotIn("Agent", self.offered(opted("Agent", "Workflow", "Read")))

    def test_it_is_a_graph_main_loop_only_once_workflow_is_offered(self):
        self.assertTrue(augment.workflow_opted_in(opted("Agent")))
        self.assertFalse(server._graph_main_loop(opted("Agent", "Read")))
        self.assertTrue(server._graph_main_loop(opted("Agent", "Workflow")))
        # So the planning pass runs, and writes are not held for a Workflow call it cannot make.
        self.assertFalse(server._graph_not_started(opted("Agent", "Read")))

    def test_the_guidance_says_to_delegate_with_agent_or_load_workflow(self):
        notes = augment.with_guidance("You are Claude Code.", opted("Agent", "Read", "ToolSearch"))
        text = notes if isinstance(notes, str) else str(notes)
        self.assertIn("the `Workflow` tool is not loaded", text)
        self.assertIn("select:Workflow", text)
        loaded = augment.with_guidance("You are Claude Code.", opted("Agent", "Workflow", "Read"))
        self.assertNotIn("the `Workflow` tool is not loaded", str(loaded))


class WindowsWorkingDirectory(unittest.TestCase):
    def test_a_drive_letter_path_with_spaces_is_read_whole(self):
        system = ("Environment\n - Primary working directory: "
                  "C:\\Users\\Archi\\Desktop\\Agentus IT thingy\\GitHub Repo\\bridge\n - Platform: win32")
        self.assertEqual(augment.working_directory(system),
                         "C:\\Users\\Archi\\Desktop\\Agentus IT thingy\\GitHub Repo\\bridge")

    def test_forward_slashes_and_shares_are_paths_too(self):
        self.assertEqual(augment.working_directory("Primary working directory: c:/work/repo"), "c:/work/repo")
        self.assertEqual(augment.working_directory("cwd: \\\\nas\\share\\repo"), "\\\\nas\\share\\repo")

    def test_posix_paths_are_unchanged(self):
        self.assertEqual(augment.working_directory("Primary working directory: /Users/me/repo now"),
                         "/Users/me/repo")
        self.assertIsNone(augment.working_directory("Primary working directory: relative/path"))


class SubagentsOnlyOnAgentaus(unittest.TestCase):
    def setUp(self):
        self._only = settings.agentaus_subagents_only

    def tearDown(self):
        settings.agentaus_subagents_only = self._only

    def route(self, model, headers):
        body = {"model": model, "messages": [user("survey the tests")], "tools": [tool("Read")]}
        return server._route(SimpleNamespace(headers=headers), body, model, main_loop=True)

    def test_off_a_claude_sessions_subagent_stays_on_claude(self):
        settings.agentaus_subagents_only = False
        sessions.note_main_loop("claude-session", False)
        headers = {"x-claude-code-session-id": "claude-session", "x-claude-code-agent-id": "a1"}
        self.assertEqual(self.route("claude-haiku-5-5", headers), (False, False))

    def test_on_every_subagent_goes_to_agentaus_even_with_no_session_record(self):
        settings.agentaus_subagents_only = True
        unknown = {"x-claude-code-session-id": "forgotten-by-a-restart", "x-claude-code-agent-id": "a2"}
        self.assertEqual(self.route("claude-haiku-5-5", unknown), (True, True))
        # The main loop still goes where its model says.
        self.assertEqual(self.route("claude-opus-5-5", {"x-claude-code-session-id": "s"})[0], False)


if __name__ == "__main__":
    unittest.main()
