"""An opted-in ("ultracode") Agentaus main loop whose `Workflow` tool is still deferred.

Measured (24 Nov 2026, Claude Code 2.1.331): max effort opted the session in, Claude Code's
reminder said "use the Workflow tool", and Workflow was never on the wire - it stays deferred
until loaded. The bridge treated the session as a graph main loop anyway: it skipped the
planning pass, would have hidden `Agent`, and its guidance pointed only at Workflow. Agentaus
worked alone for 32 turns.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import augment, graph, graph_turn, server, sessions  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402


def tool(name):
    return {"name": name, "description": "", "input_schema": {"type": "object"}}


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def opted(*tools):
    return {"model": "agentaus", "tools": [tool(t) for t in tools],
            "messages": [user("ultracode: fix the compaction bugs in the bridge")]}


# Claude Code's deferred-tools listing, in the newer and the older form.
LISTING = ("<system-reminder>\nThe following deferred tools are now available via ToolSearch. "
           "Their schemas are NOT loaded:\nCronCreate\nWorkflow\nWebSearch\n</system-reminder>")
OLD_LISTING = "<available-deferred-tools>\nTaskStop\nWorkflow\n</available-deferred-tools>"


def deferred(*tools, listing=LISTING):
    """Opted in, `Workflow` deferred: ToolSearch offered and the listing names Workflow."""
    body = opted(*tools, "ToolSearch")
    body["messages"].insert(0, user(listing))
    return body


def guidance(body):
    notes = augment.with_guidance("You are Claude Code.", body)
    return notes if isinstance(notes, str) else str(notes)


def call(name, id, **input):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": id, "name": name, "input": input}]}


def result(id, text="done"):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": id, "content": text}]}


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
        self.assertFalse(graph_turn._graph_main_loop(opted("Agent", "Read")))
        self.assertTrue(graph_turn._graph_main_loop(opted("Agent", "Workflow")))
        # So the planning pass runs, and writes are not held for a Workflow call it cannot make.
        self.assertFalse(graph_turn._graph_not_started(opted("Agent", "Read")))

    def test_the_guidance_says_to_delegate_with_agent_or_load_workflow(self):
        for listing in (LISTING, OLD_LISTING):
            text = guidance(deferred("Agent", "Read", listing=listing))
            self.assertIn("the `Workflow` tool is not loaded", text)
            self.assertIn("Delegate with `Agent`", text)
            self.assertIn("select:Workflow", text)
        loaded = guidance(opted("Agent", "Workflow", "Read", "ToolSearch"))
        self.assertNotIn("the `Workflow` tool is not loaded", loaded)

    def test_a_trivial_turn_is_told_to_work_alone(self):
        self.assertIn("**Trivial or conversational turn** → work alone, no `Agent`.",
                      guidance(deferred("Agent", "Read")))

    def test_no_note_without_tool_search(self):
        # Every tool is on the wire then: a missing Workflow is one this client does not have.
        body = deferred("Agent", "Read")
        body["tools"] = [t for t in body["tools"] if t["name"] != "ToolSearch"]
        self.assertFalse(augment.workflow_deferred(body))
        self.assertNotIn("the `Workflow` tool is not loaded", guidance(body))

    def test_no_note_when_the_listing_does_not_name_workflow(self):
        body = deferred("Agent", "Read", listing=LISTING.replace("Workflow\n", ""))
        self.assertFalse(augment.workflow_deferred(body))
        self.assertNotIn("the `Workflow` tool is not loaded", guidance(body))

    def test_mentioning_the_workflow_tool_is_not_a_deferred_workflow(self):
        # ingoau's case: the opt-in heuristic fires on this message, and there is no listing.
        body = {"model": "agentaus", "tools": [tool("Agent"), tool("Read"), tool("Edit"), tool("ToolSearch")],
                "messages": [user("Please refactor the Workflow tool's description in tools.py")]}
        self.assertFalse(augment.workflow_deferred(body))
        text = guidance(body)
        self.assertNotIn("opted into multi-agent orchestration", text)
        self.assertNotIn("Delegate with `Agent`", text)

    def test_without_agent_the_note_only_says_to_load_workflow(self):
        text = guidance(deferred("Read", "Edit"))
        self.assertIn("the `Workflow` tool is not loaded", text)
        self.assertIn("select:Workflow", text)
        self.assertNotIn("Delegate with `Agent`", text)

    def test_having_delegated_with_agent_loading_workflow_does_not_make_a_graph_main_loop(self):
        # The model followed the note: two Agent subagents, then loaded Workflow as well.
        body = opted("Agent", "Workflow", "ToolSearch", "Write")
        body["messages"] += [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "a1", "name": "Agent", "input": {"prompt": "survey graph.py"}},
                {"type": "tool_use", "id": "a2", "name": "Agent", "input": {"prompt": "survey server.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a1", "content": "notes"},
                                         {"type": "tool_result", "tool_use_id": "a2", "content": "notes"}]},
            call("ToolSearch", "t1", query="select:Workflow"), result("t1")]
        self.assertFalse(graph_turn._graph_main_loop(body))
        # So the merge write is not refused, and no graph is launched over the subagents' work.
        self.assertFalse(graph_turn._graph_not_started(body))
        self.assertIn("Agent", self.offered(body))


class AStoppedWorkflowIsNotStillRunning(unittest.TestCase):
    """Claude Code 2.1.331 delivers a stopped workflow's notification as a `system` entry.
    Read only from user messages, the launch never reported back, and the bridge refused every
    Edit after it with "The workflow you launched (w5iah888s) is still running"."""

    def body(self, notice_role):
        launch = ("Workflow launched in background. Task ID: w5iah888s\n"
                  "Summary: Scout, plan twice, review the plan, then run it")
        notice = ("<task-notification>\n<task-id>w5iah888s</task-id>\n<status>killed</status>\n"
                  "<summary>Task was stopped by the user.</summary>\n</task-notification>")
        return {"model": "agentaus", "tools": [tool("Edit")], "messages": [
            user("fix the compaction bugs"),
            {"role": "assistant", "content": [{"type": "tool_use", "id": "w1", "name": "Workflow",
                                               "input": {"script": "export const meta = {}"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "w1", "content": launch}]},
            {"role": notice_role, "content": notice},
            user("carry on with the Agent tool")]}

    def test_a_notice_in_a_system_entry_counts(self):
        self.assertEqual(augment.pending_workflows(self.body("system")), set())

    def test_a_notice_in_a_user_message_still_counts(self):
        self.assertEqual(augment.pending_workflows(self.body("user")), set())

    def test_with_no_notice_it_is_still_running(self):
        body = self.body("system")
        body["messages"].pop(3)
        self.assertEqual(augment.pending_workflows(body), {"w5iah888s"})


class TheGraphRecordCountsASystemEntryReport(unittest.TestCase):
    """`graph.note_run` keeps a launch "running" across compaction (the remembered set in
    `pending_workflows`). A report in a `system` entry must close it there too."""

    CWD = "/w/delegation-proj"

    def setUp(self):
        graph.forget_runs()
        self.addCleanup(graph.forget_runs)

    def body(self, notice=None, as_blocks=False):
        launch = "Workflow launched in background. Task ID: w5iah888s\nSummary: the graph"
        messages = [
            {"role": "system", "content": f"Environment\n - Primary working directory: {self.CWD}\n"},
            user("ultracode: fix the compaction bugs"),
            call("Workflow", "w1", script=graph.DYNAMIC_SCRIPT, args={"task": "fix the compaction bugs"}),
            result("w1", launch)]
        if notice:
            text = ("<task-notification>\n<task-id>w5iah888s</task-id>\n<status>killed</status>\n"
                    "<summary>Task was stopped by the user.</summary>\n</task-notification>")
            content = [{"type": "text", "text": text}] if as_blocks else text
            messages.append({"role": notice, "content": content})
        messages.append(user("carry on"))
        return {"model": "agentaus", "tools": [tool("Edit")], "messages": messages,
                "metadata": {"user_id": "user_x_session_delegation"}}

    def test_the_launch_is_running_until_it_reports(self):
        self.assertEqual(graph.run_state(graph.note_run(self.body())), "running")

    def test_a_report_in_a_system_entry_is_reported(self):
        self.assertEqual(graph.run_state(graph.note_run(self.body("system"))), "reported")

    def test_a_report_in_system_text_blocks_is_reported(self):
        self.assertEqual(graph.run_state(graph.note_run(self.body("system", as_blocks=True))), "reported")

    def test_a_report_in_a_user_message_still_is(self):
        self.assertEqual(graph.run_state(graph.note_run(self.body("user"))), "reported")


class WindowsWorkingDirectory(unittest.TestCase):
    """`main` reads these since #36/#61; kept here as the cases this PR was opened for."""

    def test_a_drive_letter_path_with_spaces_is_read_whole(self):
        system = ("Environment\n - Primary working directory: "
                  "C:\\Users\\Archi\\Desktop\\Agentus IT thingy\\GitHub Repo\\bridge\n - Platform: win32")
        self.assertEqual(augment.working_directory(system),
                         "C:\\Users\\Archi\\Desktop\\Agentus IT thingy\\GitHub Repo\\bridge")

    def test_forward_slashes_and_shares_are_paths_too(self):
        self.assertEqual(augment.working_directory("Primary working directory: c:/work/repo"), "c:/work/repo")
        self.assertEqual(augment.working_directory("cwd: \\\\nas\\share\\repo"), "\\\\nas\\share\\repo")

    def test_a_relative_path_is_not_a_working_directory(self):
        self.assertIsNone(augment.working_directory("Primary working directory: relative/path"))


class SubagentsOnlyOnAgentaus(unittest.TestCase):
    def setUp(self):
        self._saved = settings.agentaus_subagents_only, settings.agentaus_subagents_follow_session
        sessions.reset()
        self.addCleanup(sessions.reset)

    def tearDown(self):
        settings.agentaus_subagents_only, settings.agentaus_subagents_follow_session = self._saved

    def route(self, model, headers):
        body = {"model": model, "messages": [user("survey the tests")], "tools": [tool("Read")]}
        return server._route(SimpleNamespace(headers=headers), body, model, main_loop=True)

    def test_off_a_claude_sessions_subagent_stays_on_claude(self):
        settings.agentaus_subagents_only = False
        sessions.note_main_loop("claude-session", False)
        headers = {"x-claude-code-session-id": "claude-session", "x-claude-code-agent-id": "a1"}
        self.assertEqual(self.route("claude-haiku-5-5", headers), (False, None))

    def test_on_every_subagent_goes_to_agentaus_even_with_no_session_record(self):
        settings.agentaus_subagents_only = True
        unknown = {"x-claude-code-session-id": "forgotten-by-a-restart", "x-claude-code-agent-id": "a2"}
        self.assertEqual(self.route("claude-haiku-5-5", unknown), (True, server.ROUTE_SUBAGENTS_ONLY))
        # The main loop still goes where its model says.
        self.assertEqual(self.route("claude-opus-5-5", {"x-claude-code-session-id": "s"}), (False, None))

    def test_a_claude_sessions_subagent_is_logged_as_subagents_only_not_as_following(self):
        settings.agentaus_subagents_only = True
        sessions.note_main_loop("claude-session", False)
        headers = {"x-claude-code-session-id": "claude-session", "x-claude-code-agent-id": "a3"}
        self.assertEqual(self.route("claude-haiku-5-5", headers), (True, server.ROUTE_SUBAGENTS_ONLY))

    def test_an_agentaus_sessions_subagent_is_still_following_its_session_when_off(self):
        settings.agentaus_subagents_only = False
        sessions.note_main_loop("agentaus-session", True)
        headers = {"x-claude-code-session-id": "agentaus-session", "x-claude-code-agent-id": "a4"}
        self.assertEqual(self.route("claude-haiku-5-5", headers), (True, server.ROUTE_FOLLOWS_SESSION))

    def test_on_holds_with_following_the_session_turned_off(self):
        settings.agentaus_subagents_only, settings.agentaus_subagents_follow_session = True, False
        headers = {"x-claude-code-session-id": "s5", "x-claude-code-agent-id": "a5"}
        self.assertEqual(self.route("claude-haiku-5-5", headers), (True, server.ROUTE_SUBAGENTS_ONLY))
        settings.agentaus_subagents_only = False
        self.assertEqual(self.route("claude-haiku-5-5", headers), (False, None))


if __name__ == "__main__":
    unittest.main()
