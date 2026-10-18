"""Ultracode on Agentaus: a `Workflow` turn and the subagents it starts.

Measured on Claude Code 2.1.300, headless, with the user's prompt starting "ultracode:".
Each test below is a fault seen live on that run:

- Tool focus held `Workflow` back, because the user never typed the tool's name.
- After the launch, the stall judge read "it is running, I will report back" as a step
  announced and not taken, re-asked, and the model launched the same workflow again.
- The result arrived as a `<task-notification>`, which the grounding check could not see,
  so eight correct figures were rewritten as "the repo may contain ...; I have not read it".
- The answer was written to a path that spliced the session scratchpad onto the working
  directory, and landed outside it.
- Every subagent was planned, reviewed and grounding-checked like a main loop, and a
  nullable field in a workflow's schema crashed the argument check.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import augment, documents, ledger, repair, schema, server, sessions, workflow_script  # noqa: E402
from unittest import mock  # noqa: E402


def tool(name):
    return {"name": name, "description": "", "input_schema": {"type": "object"}}


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


SUBAGENT_SYSTEM = [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.300; "
                    "cc_entrypoint=claude-vscode; cc_is_subagent=true;\nYou are a subagent "
                    "spawned by a workflow orchestration script."}]

NOTIFICATION = (
    "<system-reminder>\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n\n<task-notification>\n"
    "<task-id>w1</task-id>\n<status>completed</status>\n<summary>Dynamic workflow "
    "\"rdti\" completed</summary>\n<result>{\"q1\":12956,\"q2\":16174122667}</result>\n"
    "</task-notification>\n</system-reminder>")


def launched():
    """A main loop just after its Workflow call came back."""
    return {"tools": [tool("Workflow"), tool("Bash")], "messages": [
        user("ultracode: total the claims"),
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Workflow", "input": {"script": "..."}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "Workflow launched in background. Task ID: w1"}]},
    ]}


class WorkflowIsOfferedWhenTheUserOptsIn(unittest.TestCase):
    def body(self, text, *, history=()):
        return {"tools": [tool("Workflow"), tool("Read"), tool("Artifact")],
                "messages": [user(t) for t in history] + [user(text)]}

    def offered(self, body):
        focused, _ = augment.focus_tools(body)
        return {t["name"] for t in focused["tools"]}

    def test_the_ultracode_keyword_keeps_workflow(self):
        self.assertIn("Workflow", self.offered(self.body("ultracode: analyse these files")))

    def test_asking_for_a_workflow_in_your_own_words_keeps_it(self):
        self.assertIn("Workflow", self.offered(self.body("Please use a workflow for this")))
        self.assertIn("Workflow", self.offered(self.body("fan out agents over the repo")))

    def test_a_ci_workflow_is_not_an_opt_in(self):
        for text in ("Can you write a workflow that runs pytest on every push?",
                     "Why does CI fail when I run the workflow on GitHub Actions?",
                     "use a workflow in .github/workflows/ci.yml as a template",
                     "Please orchestrate the deploy; no subagents"):
            self.assertNotIn("Workflow", self.offered(self.body(text)), text)

    def test_a_listed_skill_is_not_an_opt_in_but_an_invoked_one_is(self):
        listed = self.body("<system-reminder>- workflow-authoring: Reference</system-reminder>\nWhat is 2+2?")
        self.assertNotIn("Workflow", self.offered(listed))
        invoked = self.body("<command-name>workflow-authoring</command-name>\nmap the repo")
        self.assertIn("Workflow", self.offered(invoked))

    def test_the_ultracode_reminder_is_an_opt_in(self):
        body = self.body('<system-reminder>The user included the keyword "ultracode"</system-reminder>do it')
        self.assertIn("Workflow", self.offered(body))

    def test_an_opt_in_decided_before_compaction_survives_it(self):
        focused, _ = augment.focus_tools(self.body("[summary of earlier turns]"), workflow_opt_in=True)
        self.assertIn("Workflow", {t["name"] for t in focused["tools"]})

    def test_the_opt_in_stands_for_later_turns(self):
        body = self.body("now do the next file", history=["ultracode: audit the repo"])
        self.assertIn("Workflow", self.offered(body))

    def test_without_an_opt_in_workflow_is_still_held_back(self):
        offered = self.offered(self.body("what does this function do?"))
        self.assertNotIn("Workflow", offered)
        self.assertNotIn("Artifact", offered)

    def test_the_main_loop_is_told_how_a_workflow_runs(self):
        notes = augment.guidance_for(self.body("ultracode: analyse these files"))
        self.assertIn("Workflow launched in background", notes)
        self.assertIn("export const meta", notes)

    def test_no_workflow_notes_without_the_opt_in(self):
        notes = augment.guidance_for(self.body("what does this do?"))
        self.assertNotIn("export const meta", notes)


class SubagentTurns(unittest.TestCase):
    def body(self, *names):
        return {"system": SUBAGENT_SYSTEM, "tools": [tool(n) for n in names],
                "messages": [user("Compute the totals and return them.")]}

    def test_a_workflow_subagent_is_recognised(self):
        self.assertTrue(augment.is_subagent_turn(self.body("Bash", "StructuredOutput")))
        self.assertFalse(augment.is_subagent_turn(
            {"tools": [tool("Bash")], "messages": [user("hi")]}))

    def test_structured_output_alone_is_not_a_subagent(self):
        # A headless `--json-schema` main loop is offered StructuredOutput too.
        self.assertFalse(augment.is_subagent_turn(
            {"tools": [tool("Bash"), tool("StructuredOutput")], "messages": [user("hi")]}))

    def test_a_subagent_is_not_planned(self):
        self.assertFalse(augment.should_think(self.body("Bash", "StructuredOutput")))

    def test_a_subagent_answer_is_not_rewritten(self):
        body = self.body("Bash", "StructuredOutput")
        self.assertFalse(augment.worth_reviewing_turn(body))
        self.assertFalse(server._grounding_applies(body, "x" * 5000, {"agentaus_zoom": 1}))

    def test_a_subagent_is_told_to_return_through_structured_output(self):
        notes = augment.guidance_for(self.body("Bash", "StructuredOutput"))
        self.assertIn("exactly one call to `StructuredOutput`", notes)
        self.assertNotIn("Reporting an analysis", notes)

    def test_a_subagent_without_a_schema_returns_its_result_as_text(self):
        notes = augment.guidance_for(self.body("Bash", "Read"))
        self.assertIn("final message is the return value", notes)


class SubagentsSeeTheSessionsRequest(unittest.TestCase):
    class _Request:
        def __init__(self, headers):
            self.headers = headers

    def setUp(self):
        sessions.reset()

    tearDown = setUp

    def test_the_main_loops_request_is_remembered_for_its_subagents(self):
        body = {"model": "agentaus", "tools": [tool("Workflow")], "messages": [
            user("ultracode: count companies; an amendment of 0 still counts"
                 "<system-reminder>noise</system-reminder>")]}
        server._route(self._Request({"x-claude-code-session-id": "s9"}), body, "agentaus",
                      main_loop=True)
        remembered = sessions.request_of("s9")
        self.assertIn("an amendment of 0 still counts", remembered)
        self.assertNotIn("noise", remembered)

    def test_a_claude_session_is_not_remembered(self):
        body = {"model": "claude-opus-5-5", "tools": [tool("Bash")], "messages": [user("hi")]}
        server._route(self._Request({"x-claude-code-session-id": "s8"}), body,
                      "claude-opus-5-5", main_loop=True)
        self.assertIsNone(sessions.request_of("s8"))

    def test_the_request_is_framed_as_context_not_a_new_task(self):
        system = augment.with_parent_request("base", "ultracode: count the companies")
        self.assertIn("count the companies", system)
        self.assertIn("Your task is the one in your prompt", system)


class NoDeliverableWhileTheWorkflowRuns(unittest.TestCase):
    cwd = "/work/proj"

    def write(self, path):
        return [{"name": "Write", "arguments": {"file_path": path, "content": "draft"}}]

    def test_a_write_into_the_working_directory_waits_for_the_result(self):
        body = launched()
        body["messages"][-1]["content"][0]["content"] = "Workflow launched in background. Task ID: w1"
        held = augment.early_deliverable_writes(self.write("/work/proj/response.md"), body, self.cwd)
        self.assertEqual(len(held), 1)
        self.assertIn("still running", held[0][1])

    def test_scratch_files_elsewhere_are_allowed(self):
        self.assertEqual(augment.early_deliverable_writes(
            self.write("/tmp/scratch/baseline.py"), launched(), self.cwd), [])

    def test_once_the_result_arrives_the_write_goes_through(self):
        body = launched()
        body["messages"] += [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                             user(NOTIFICATION)]
        self.assertEqual(augment.pending_workflows(body), set())
        self.assertEqual(augment.early_deliverable_writes(
            self.write("/work/proj/response.md"), body, self.cwd), [])

    def test_a_rejected_launch_is_not_running(self):
        body = launched()
        body["messages"][-1]["content"][0].update(is_error=True)
        self.assertEqual(augment.pending_workflows(body), set())


class TheLaunchIsNotAStall(unittest.TestCase):
    def test_a_reply_after_a_workflow_launch_awaits_the_result(self):
        self.assertTrue(augment.awaiting_background(launched()))

    def test_a_rejected_script_is_not_a_launch(self):
        body = launched()
        body["messages"][-1]["content"][0].update(
            content="Invalid workflow script: Script parse error", is_error=True)
        self.assertFalse(augment.awaiting_background(body))

    def test_a_background_shell_command_is_not_a_launch(self):
        body = {"messages": [
            user("start the server"),
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": "Command running in background with ID: b1"}]},
        ]}
        self.assertFalse(augment.awaiting_background(body))

    def test_an_ordinary_tool_result_does_not(self):
        body = {"messages": [
            user("read it"),
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file text"}]},
        ]}
        self.assertFalse(augment.awaiting_background(body))


class TheWorkflowResultIsEvidence(unittest.TestCase):
    def body(self):
        body = launched()
        body["messages"] += [
            {"role": "assistant", "content": [{"type": "text", "text": "It is running."}]},
            user(NOTIFICATION),
        ]
        return body

    def test_a_notification_turn_is_recognised_and_not_replanned(self):
        self.assertTrue(augment.notification_turn(self.body()))
        self.assertFalse(augment.should_think(self.body()))
        self.assertFalse(augment.notification_turn(launched()))

    def test_the_notification_result_is_rendered_as_evidence(self):
        evidence = ledger.render_notifications(self.body()["messages"])
        self.assertIn("16174122667", evidence)
        self.assertIn("IS supported", evidence)

    def test_no_notification_no_evidence(self):
        self.assertEqual(ledger.render_notifications(launched()["messages"]), "")


class WorkflowSchemas(unittest.TestCase):
    def test_a_type_list_is_checked_not_crashed_on(self):
        nullable = {"type": "object", "required": ["note"],
                    "properties": {"note": {"type": ["string", "null"]}}}
        self.assertEqual(schema.validate({"note": None}, nullable)[1], [])
        self.assertEqual(schema.validate({"note": "ok"}, nullable)[1], [])
        problems = schema.validate({"note": ["a list"]}, nullable)[1]
        self.assertTrue(problems and "string" in problems[0])

    def test_null_in_a_non_nullable_required_field_is_still_missing(self):
        strict = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}
        self.assertTrue(schema.validate({"n": None}, strict)[1])

    def test_an_integral_float_is_an_integer(self):
        strict = {"type": "object", "properties": {"n": {"type": "integer"}}}
        fixed, problems = schema.validate({"n": 12956.0}, strict)
        self.assertEqual((fixed, problems), ({"n": 12956}, []))

    def test_an_unreadable_schema_forwards_the_call(self):
        calls = [{"name": "StructuredOutput", "arguments": '{"a": 1}'}]
        broken = server._validate_tool_calls(calls, {"StructuredOutput": {"type": "object",
                                                     "properties": "not a dict"}})
        self.assertEqual(broken, [])


class GarbledWorkingDirectory(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cwd = os.path.join(self.root, "-Users-me-proj-scratchpad", "runs", "wf1")
        # The splice observed live: the session scratchpad's prefix, which itself spells
        # out the working directory's components, glued to the working directory's tail.
        self.garbled = "/private/tmp/c-501/-private-tmp-c-501--Users-me-proj-scratchpad/runs/wf1"
        os.makedirs(self.cwd)

    def call(self, name, path):
        return repair.repair({"name": name, "arguments": {"file_path": path}}, name,
                             {"properties": {"file_path": {"type": "string"}}}, self.cwd)

    def test_a_spliced_prefix_is_moved_into_the_working_directory(self):
        out = self.call("Write", self.garbled + "/a.json")
        self.assertIn(os.path.join(self.cwd, "a.json"), out["arguments"])

    def test_a_new_directory_inside_the_working_directory_is_left_alone(self):
        path = os.path.join(self.cwd, "runs", "wf1", "a.json")
        self.assertEqual(self.call("Write", path)["arguments"], {"file_path": path})

    def test_a_volume_that_shares_the_tail_is_left_alone(self):
        path = "/Volumes/backup/runs/wf1/a.json"
        self.assertEqual(self.call("Write", path)["arguments"], {"file_path": path})

    def test_a_real_path_elsewhere_is_left_alone(self):
        other = os.path.join(self.root, "elsewhere")
        os.makedirs(other)
        path = os.path.join(other, "a.json")
        self.assertEqual(self.call("Write", path)["arguments"], {"file_path": path})

    def test_a_read_is_moved_only_to_a_file_that_exists(self):
        missing = self.garbled + "/nothing.txt"
        self.assertEqual(self.call("Read", missing)["arguments"], {"file_path": missing})
        open(os.path.join(self.cwd, "data.csv"), "w").close()
        out = self.call("Read", self.garbled + "/data.csv")
        self.assertIn(os.path.join(self.cwd, "data.csv"), out["arguments"])


class OneLineScripts(unittest.TestCase):
    fix = staticmethod(workflow_script.separate_statements)

    def test_a_brace_run_into_the_next_statement_gets_a_semicolon(self):
        script = "export const meta = { name: 'a', phases: [{ title: 'X' }] } const T = 1; return T"
        self.assertEqual(self.fix(script),
                         "export const meta = { name: 'a', phases: [{ title: 'X' }] }; const T = 1; return T")

    def test_text_inside_strings_and_templates_is_untouched(self):
        script = ("const a = '} const'; const b = `x ${ {k: 1}.k } const y`; "
                  "const c = \"} return\"; // } const\nreturn [a, b, c]")
        self.assertEqual(self.fix(script), script)

    def test_a_regex_after_return_is_not_read_as_code(self):
        script = 'const ok = (s) => { return /"/.test(s) }; const q = "Return {ok:true} if tests pass"; log(q)'
        self.assertEqual(self.fix(script), script)

    def test_a_template_expression_brace_is_not_a_statement_end(self):
        script = "const p = `in ${dir} const rules`; return p"
        self.assertEqual(self.fix(script), script)

    def test_a_valid_multi_line_script_is_unchanged(self):
        script = "export const meta = { name: 'a' }\nconst x = await agent('hi')\nreturn x"
        self.assertEqual(self.fix(script), script)

    def test_do_while_and_else_are_left_alone(self):
        script = "if (a) { b() } else { c() } do { d() } while (e)"
        self.assertEqual(self.fix(script), script)

    def test_the_repair_runs_on_workflow_calls(self):
        call = {"name": "Workflow", "arguments": {"script": "const m = {} const n = 1"}}
        out = repair.repair(call, "Workflow", {"properties": {"script": {"type": "string"}}}, None)
        self.assertIn("{}; const n", out["arguments"])


class TheStallBudgetRefillsWithProgress(unittest.TestCase):
    def body(self, calls):
        messages = [user("ultracode: fix the bugs")]
        for i in range(calls):
            messages += [{"role": "assistant", "content": [
                             {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}]},
                         {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]}]
        return {"messages": messages}

    def setUp(self):
        server._STALL_REASKS.clear()

    tearDown = setUp

    def test_two_reasks_without_progress_then_none(self):
        self.assertEqual([server._may_reask_stalled(self.body(1)) for _ in range(3)],
                         [True, True, False])

    def test_three_tool_calls_since_the_last_reask_earn_a_fresh_budget(self):
        server._may_reask_stalled(self.body(1))
        server._may_reask_stalled(self.body(1))
        self.assertFalse(server._may_reask_stalled(self.body(2)))
        self.assertTrue(server._may_reask_stalled(self.body(4)))


class NothingToGroundAfterALaunch(unittest.TestCase):
    def test_the_launch_report_is_not_grounding_checked(self):
        self.assertFalse(server._grounding_applies(launched(), "It is running. " * 60, None))


class TheInputsAreNamed(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cwd, "past"))
        os.makedirs(os.path.join(self.cwd, ".git"))
        for name in ("RFQ.md", "past/proposal.docx", ".hidden"):
            open(os.path.join(self.cwd, name), "w").close()

    def test_documents_are_listed_with_how_to_open_them(self):
        files = augment.input_files(self.cwd)
        self.assertIn("`RFQ.md`", files)
        doc = next(f for f in files if "proposal.docx" in f)
        self.assertIn("Word document", doc)
        self.assertTrue("Read" in doc or "Python" in doc)
        self.assertFalse(any(".hidden" in f or ".git" in f for f in files))

    def test_the_list_reaches_an_opted_in_main_loop(self):
        body = {"system": f"Primary working directory: {self.cwd}",
                "tools": [tool("Workflow"), tool("Read")], "messages": [user("ultracode: answer the RFQ")]}
        self.assertIn("proposal.docx", augment.guidance_for(body))


class ARefusedOfficeReadIsReplaced(unittest.TestCase):
    def body(self, result):
        return {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "r1", "name": "Read",
                                               "input": {"file_path": "/w/past/proposal.docx"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", **result}]}]}

    def test_the_binary_refusal_becomes_the_documents_text(self):
        refusal = {"is_error": True, "content": "This tool cannot read binary files."}
        with mock.patch.object(documents, "available", return_value=True), \
                mock.patch.object(documents, "extract", return_value="Total AUD $80,000"):
            out = documents.repair_tool_results(self.body(refusal))
        block = out["messages"][1]["content"][0]
        self.assertIn("$80,000", block["content"])
        self.assertNotIn("is_error", block)

    def test_a_missing_file_error_is_left_alone(self):
        missing = {"is_error": True, "content": "File does not exist."}
        with mock.patch.object(documents, "available", return_value=True), \
                mock.patch.object(documents, "extract", return_value="text"):
            out = documents.repair_tool_results(self.body(missing))
        self.assertEqual(out["messages"][1]["content"][0]["content"], "File does not exist.")


class MCPSchemasHaveABudget(unittest.TestCase):
    def big(self, name):
        return {"name": name, "description": "x" * 3000, "input_schema": {"type": "object"}}

    def body(self, text, *, used=()):
        tools = [tool("Read"), tool("Bash")] + [self.big(f"mcp__claude_ai_Slack__t{i}") for i in range(20)]
        tools += [self.big("mcp__notes__find")]
        messages = [user(text)]
        for name in used:
            messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": "u", "name": name, "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u", "content": "ok"}]}]
        return {"tools": tools, "messages": messages}

    def kept(self, body):
        return {t["name"] for t in augment.focus_tools(body)[0]["tools"]}

    def test_an_unnamed_server_over_the_budget_is_held_back(self):
        kept = self.kept(self.body("fix the failing test"))
        self.assertFalse(any("Slack" in n for n in kept))
        self.assertIn("Read", kept)

    def test_a_named_server_is_kept(self):
        self.assertTrue(any("Slack" in n for n in self.kept(self.body("post this to Slack"))))

    def test_a_used_server_is_kept(self):
        kept = self.kept(self.body("carry on", used=["mcp__claude_ai_Slack__t1"]))
        self.assertTrue(any("Slack" in n for n in kept))

    def test_under_the_budget_nothing_mcp_is_held_back(self):
        body = {"tools": [tool("Read"), tool("mcp__notes__find")], "messages": [user("hi")]}
        self.assertIn("mcp__notes__find", self.kept(body))


class RewritesSeeTheWholeAnswer(unittest.TestCase):
    def test_an_answer_longer_than_a_rewrite_sees_is_kept(self):
        long_answer = "x" * (server._REWRITE_CHARS + 1)
        self.assertTrue(server._too_long_to_rewrite(long_answer, "self-review"))
        self.assertFalse(server._too_long_to_rewrite("short", "self-review"))


if __name__ == "__main__":
    unittest.main()
