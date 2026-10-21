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

from agentaus_bridge import augment, documents, graph, ledger, repair, schema, server, sessions, workflow_script  # noqa: E402
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

    def test_by_default_the_main_loop_is_told_to_run_the_graph(self):
        notes = augment.guidance_for(self.body("ultracode: analyse these files"))
        self.assertIn("Workflow launched in background", notes)
        self.assertIn('"script": "agentaus-solve"', notes)

    def test_with_the_graph_off_it_is_taught_to_write_the_script(self):
        from agentaus_bridge.config import settings
        saved, settings.agentaus_workflow_graph = settings.agentaus_workflow_graph, False
        try:
            notes = augment.guidance_for(self.body("ultracode: analyse these files"))
        finally:
            settings.agentaus_workflow_graph = saved
        self.assertIn("export const meta", notes)

    def test_with_the_graph_on_it_is_told_to_run_the_graph(self):
        from agentaus_bridge.config import settings
        saved, settings.agentaus_workflow_graph = settings.agentaus_workflow_graph, True
        try:
            notes = augment.guidance_for(self.body("ultracode: analyse these files"))
        finally:
            settings.agentaus_workflow_graph = saved
        self.assertIn('"script": "agentaus-solve"', notes)
        self.assertNotIn("export const meta", notes)

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


class TheBridgeRunsItsOwnGraph(unittest.TestCase):
    def setUp(self):
        from agentaus_bridge.config import settings
        self._saved = settings.agentaus_workflow_graph, settings.agentaus_graph_kind
        settings.agentaus_workflow_graph, settings.agentaus_graph_kind = True, "fixed"

    def tearDown(self):
        from agentaus_bridge.config import settings
        settings.agentaus_workflow_graph, settings.agentaus_graph_kind = self._saved

    def call(self, **arguments):
        return {"id": "w", "name": "Workflow", "arguments": __import__("json").dumps(arguments)}

    def body(self):
        return {"system": "Primary working directory: /work/proj", "tools": [tool("Workflow")],
                "messages": [user("ultracode: total the claims in claims.xlsx")]}

    def test_the_models_script_is_replaced_by_the_graph(self):
        import json
        calls = [self.call(script="export const meta = {} broken(", description="mine")]
        server._run_graph_instead(calls, self.body())
        args = json.loads(calls[0]["arguments"])
        self.assertEqual(args["script"], graph.SCRIPT)
        self.assertEqual(args["args"]["cwd"], "/work/proj")
        self.assertIn("total the claims", args["args"]["task"])
        self.assertEqual(args["description"], "mine")

    def test_the_dynamic_graph_takes_code_work_too(self):
        from agentaus_bridge.config import settings
        saved, settings.agentaus_graph_kind = settings.agentaus_graph_kind, "dynamic"
        try:
            self.assertTrue(graph.suits("ultracode: fix the bug in tools/bank.py", None))
        finally:
            settings.agentaus_graph_kind = saved

    def test_code_work_keeps_agentaus_own_workflow(self):
        body = {"system": "Primary working directory: /work/proj", "tools": [tool("Workflow")],
                "messages": [user("ultracode: implement the functions in solutions.py")]}
        calls = [self.call(script="export const meta = { name: 'own' }")]
        server._run_graph_instead(calls, body)
        self.assertIn("own", calls[0]["arguments"])
        self.assertIn("export const meta", augment.guidance_for(body))

    def test_data_work_gets_the_graph(self):
        self.assertTrue(graph.suits("ultracode: total the claims in claims.xlsx", None))
        self.assertFalse(graph.suits("ultracode: fix the bug in tools/bank.py", None))

    def test_a_script_the_user_wrote_is_not_replaced(self):
        script = "export const meta = { name: 'mine' }\n" + "// the user's own orchestration\n" * 12
        body = self.body()
        body["messages"].append(user("ultracode: run exactly this:\n" + script))
        calls = [self.call(script=script)]
        server._run_graph_instead(calls, body)
        self.assertIn("the user's own orchestration", calls[0]["arguments"])

    def test_a_follow_up_carries_the_opening_request(self):
        import json
        body = self.body()
        body["messages"].append(user("now also do the 2021 file"))
        calls = [self.call(script="agentaus-solve")]
        server._run_graph_instead(calls, body)
        task = json.loads(calls[0]["arguments"])["args"]["task"]
        self.assertIn("total the claims", task)
        self.assertIn("now also do the 2021 file", task)

    def test_a_saved_or_resumed_workflow_is_left_alone(self):
        for kept in (self.call(name="review"), self.call(scriptPath="/x.js"),
                     self.call(script="s", resumeFromRunId="wf_1")):
            before = kept["arguments"]
            server._run_graph_instead([kept], self.body())
            self.assertEqual(kept["arguments"], before)

    def test_not_without_an_opt_in_or_on_a_subagent(self):
        plain = {"tools": [tool("Workflow")], "messages": [user("what is this?")]}
        sub = {**self.body(), "system": SUBAGENT_SYSTEM}
        for body in (plain, sub):
            calls = [self.call(script="mine")]
            server._run_graph_instead(calls, body)
            self.assertIn("mine", calls[0]["arguments"])

    def test_a_graph_subagent_gets_only_the_tools_its_job_needs(self):
        body = {"tools": [tool(n) for n in ("Bash", "Read", "Agent", "Workflow", "StructuredOutput",
                                             "mcp__claude_ai_Slack__x")],
                "messages": [user(graph.MARK)]}
        kept = {t["name"] for t in graph.focus(body)[0]["tools"]}
        self.assertEqual(kept, {"Bash", "Read", "StructuredOutput"})

    def test_a_graph_subagents_oversized_tool_result_is_cut(self):
        big = "row\n" * 20000
        body = {"messages": [user(graph.MARK), {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": big}]}]}
        out, cut = graph.trim_results(body)
        self.assertEqual(cut, 1)
        text = out["messages"][1]["content"][0]["content"]
        self.assertLess(len(text), graph.RESULT_CHARS + 500)
        self.assertIn("characters cut", text)

    def test_a_think_only_graph_agent_gets_only_structured_output(self):
        body = {"tools": [tool(n) for n in ("Bash", "Read", "StructuredOutput")],
                "messages": [user(graph.MARK + "\n" + graph.THINK + "\n# Plan one item")]}
        self.assertEqual({t["name"] for t in graph.focus(body)[0]["tools"]}, {"StructuredOutput"})

    def test_a_graph_subagent_is_recognised_by_its_mark(self):
        self.assertTrue(graph.is_graph_task({"messages": [user(graph.MARK + "\n# Read one file")]}))
        self.assertFalse(graph.is_graph_task({"messages": [user("compute it")]}))

    def test_every_graph_prompt_carries_the_mark_and_the_graph_runs_end_to_end(self):
        import shutil, subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(os.path.dirname(here), "agentaus_bridge", "graphs", "solve.js")
        r = subprocess.run([node, os.path.join(here, "graph_dry_run.mjs"), script],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        out = __import__("json").loads(r.stdout)
        self.assertGreater(out["agents"], 30)
        self.assertLessEqual(out["peak"], 8, "the graph must hold its concurrency cap")
        self.assertEqual(out["deliverables"], ["answers.json"])


class TheDynamicGraph(unittest.TestCase):
    def test_every_role_has_a_skill(self):
        for role in ("scout", "plan", "review-plan", "solve", "merge", "check", "assemble", "fix"):
            self.assertIn(role, graph.SKILLS)
            self.assertIn("Markdown", graph.SKILLS[role])

    def test_the_dynamic_graph_is_the_default_kind_and_carries_the_skills(self):
        import json
        from agentaus_bridge.config import settings
        self.assertEqual(settings.agentaus_graph_kind, "dynamic")
        call = {"id": "w", "name": "Workflow", "arguments": json.dumps({"script": "agentaus-solve"})}
        args = json.loads(graph.expand(call, "ultracode: total it", "/w")["arguments"])
        self.assertEqual(args["script"], graph.DYNAMIC_SCRIPT)
        self.assertIn("merge", args["args"]["skills"])

    def test_an_opted_in_main_loop_orchestrates_through_workflow_not_agent(self):
        body = {"tools": [tool("Workflow"), tool("Agent"), tool("Read")],
                "messages": [user("ultracode: implement the functions")]}
        offered = {t["name"] for t in augment.focus_tools(body)[0]["tools"]}
        self.assertIn("Workflow", offered)
        self.assertNotIn("Agent", offered)
        plain = {"tools": [tool("Agent"), tool("Read")], "messages": [user("explain this")]}
        self.assertIn("Agent", {t["name"] for t in augment.focus_tools(plain)[0]["tools"]})

    def test_a_think_only_agent_without_a_schema_gets_no_tools(self):
        body = {"tools": [tool("Bash"), tool("Read")],
                "messages": [user(graph.MARK + "\n" + graph.THINK + "\n# Plan the work")]}
        self.assertEqual(graph.focus(body)[0]["tools"], [])

    def test_graph_agents_cannot_write_where_their_role_forbids(self):
        cwd = tempfile.mkdtemp()
        src = os.path.join(cwd, "bank.py")
        open(src, "w").write("x = 1\n")
        def body(role):
            return {"messages": [user(f"{graph.MARK}\n# Job\n\n## Your skill: {role}\n")]}
        def call(name, path):
            return [{"name": name, "arguments": {"file_path": path, "content": "y"}}]
        self.assertTrue(graph.guarded_writes(call("Edit", src), body("solve"), cwd))
        self.assertFalse(graph.guarded_writes(call("Write", "/tmp/copy/bank.py"), body("solve"), cwd))
        self.assertTrue(graph.guarded_writes(call("Write", src), body("assemble"), cwd))
        self.assertFalse(graph.guarded_writes(call("Edit", src), body("assemble"), cwd))
        self.assertFalse(graph.guarded_writes(call("Write", os.path.join(cwd, "answers.json")), body("assemble"), cwd))
        self.assertFalse(graph.guarded_writes(call("Edit", src), {"messages": [user("fix it")]}, cwd))

    def test_the_role_is_read_from_an_indented_prompt(self):
        # The harness indents every prompt line by two spaces; the heading never sat at column 0.
        body = {"messages": [user(f"{graph.MARK}\n  # Job\n\n  ---\n  ## Your skill: solve\n")]}
        self.assertEqual(graph.role_of(body), "solve")
        self.assertEqual(graph.role_of({"messages": [user(f"{graph.MARK}\n> ### Your skill:  review-plan")]}),
                         "review-plan")

    def test_read_only_roles_cannot_write_into_the_project_through_bash(self):
        cwd = "/Users/x/proj"
        s = graph.scratch_of(cwd)
        blocked = [
            "sed -i '' 's/a/b/' tools/bank.py",
            "perl -pi -e 's/a/b/' tools/bank.py",
            "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: tools/bank.py\n*** End Patch\nEOF",
            "patch -p1 < /tmp/fix.patch",
            "cat > tools/bank.py <<'EOF'\nprint(1)\nEOF",
            "python3 tools/bank.py --json > answers.json",
            "python3 - <<'EOF'\np = 'tools/bank.py'\nopen(p, 'w').write('x')\nEOF",
            "python3 -c \"import json; json.dump({}, open('answers.json', 'w'))\"",
            "python3 -c \"from pathlib import Path; Path('out.txt').write_text('x')\"",
            "cp x tools/bank.py",
            "cp /tmp/sol/bank.py /Users/x/proj/tools/bank.py",
            "mv tools/bank.py tools/old.py",
            "rm -f answers.json",
            "touch figures/x.png",
            "echo x | tee report.md",
            "git apply /tmp/fix.patch",
            "git checkout -- tools/bank.py",
            "cd /Users/x/proj/tools && sed -i 's/a/b/' bank.py",
            "cd /tmp/work && cp result.json /Users/x/proj/answers.json",
            "bash -c \"sed -i 's/a/b/' tools/bank.py\"",
        ]
        allowed = [
            "cd /Users/x/proj && python3 tools/bank.py --json | head",
            "grep -n def tools/bank.py",
            "cat tools/bank.py",
            "sed -n '1,40p' tools/bank.py",
            "python3 -c \"import json; json.load(open('answers.json'))\"",
            "python3 tools/bank.py > /dev/null 2>&1",
            "python3 -c 'print(1 if 2 > 1 else 0)'",
            f"rm -rf {s}/s1/a && mkdir -p {s}/s1/a && cp -R /Users/x/proj {s}/s1/a/repo",
            f"cp -R {s}/pristine {s}/m1/c1/repo",
            f"cd {s}/s1/a/repo && sed -i '' 's/a/b/' tools/bank.py",
            f"cd {s}/s1/a/repo && patch -p1 < {s}/s1/a/fix.patch",
            f"cd {s}/s1/a && diff -ruN {s}/pristine repo > fix.patch",
            f"python3 - <<'EOF'\nOUT = '{s}/out.json'\nimport json\njson.dump({{}}, open(OUT, 'w'))\nEOF",
            "ls -la > /tmp/listing.txt",
            "git diff --stat",
        ]
        for command in blocked:
            self.assertTrue(graph.bash_writes_into(command, cwd), command)
        for command in allowed:
            self.assertEqual(graph.bash_writes_into(command, cwd), [], command)

        def body(role):
            return {"messages": [user(f"{graph.MARK}\n  ## Your skill: {role}\n")]}
        bash = [{"name": "Bash", "arguments": {"command": "sed -i '' 's/a/b/' tools/bank.py"}}]
        refused = graph.guarded_writes(bash, body("scout"), cwd)
        self.assertEqual(len(refused), 1)
        self.assertIn("read-only for your role (scout)", refused[0][1])
        # The roles that put the result in place may write; so may a prompt with no role.
        self.assertEqual(graph.guarded_writes(bash, body("assemble"), cwd), [])
        self.assertEqual(graph.guarded_writes(bash, {"messages": [user(graph.MARK)]}, cwd), [])

    def test_the_bridge_prepares_the_scratch_folder_before_the_graph_starts(self):
        import json, shutil, subprocess, zipfile
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd, True)
        self.addCleanup(shutil.rmtree, graph.scratch_of(cwd), True)
        os.makedirs(os.path.join(cwd, "past"))
        os.makedirs(os.path.join(cwd, ".git"))
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        cell = lambda t: f"<w:tc><w:p><w:r><w:t>{t}</w:t></w:r></w:p></w:tc>"
        document = (f'<w:document {w}><w:body><w:p><w:r><w:t>Pricing</w:t></w:r></w:p>'
                    f'<w:tbl><w:tr>{cell("Milestone 1")}{cell("30%")}</w:tr>'
                    f'<w:tr>{cell("Total")}{cell("AUD $88,000.00")}</w:tr></w:tbl>'
                    f'<w:p><w:r><w:t>After</w:t><w:tab/><w:t>table</w:t></w:r></w:p></w:body></w:document>')
        with zipfile.ZipFile(os.path.join(cwd, "past", "old.docx"), "w") as z:
            z.writestr("word/document.xml", document)
        with open(os.path.join(cwd, "solutions.py"), "w") as fh:
            fh.write('RATES = {"a": 1}\nlog = object()\n\ndef add(a, b):\n    """Add.\n\n'
                     '    >>> add(1, 2)\n    3\n    """\n    raise NotImplementedError\n\n'
                     'def sub(a, b):\n    return a - b\n')
        with open(os.path.join(cwd, "test_solutions.py"), "w") as fh:
            fh.write("def test_x():\n    pass\n")
        old = os.path.join(graph.scratch_of(cwd), "leftover")
        os.makedirs(old, exist_ok=True)
        from agentaus_bridge.config import settings
        self.assertEqual(settings.agentaus_graph_kind, "dynamic")
        call = {"id": "w", "name": "Workflow", "arguments": json.dumps({"script": "x"})}
        args = json.loads(graph.expand(call, "ultracode: do it", cwd)["arguments"])["args"]

        self.assertFalse(os.path.exists(old), "the scratch folder starts empty")
        self.assertTrue(os.path.isfile(os.path.join(args["pristine"], "solutions.py")))
        self.assertFalse(os.path.exists(os.path.join(args["pristine"], ".git")))
        [text] = args["texts"]
        self.assertEqual(text["source"], os.path.join("past", "old.docx"))
        with open(text["text"]) as fh:
            written = fh.read()
        self.assertEqual(written, "Pricing\nMilestone 1 | 30%\nTotal | AUD $88,000.00\nAfter\ttable\n")
        self.assertEqual(text["lines"], 4)
        self.assertEqual([(f["fn"], f["stub"], f["signature"]) for f in args["functions"]],
                         [("add", True, "def add(a, b)"), ("sub", False, "def sub(a, b)")])
        self.assertIn(">>> add(1, 2)", args["functions"][0]["doc"])
        self.assertEqual(args["constants"], [{"file": "solutions.py", "names": ["RATES"]}])

        candidate = os.path.join(cwd, "candidate.py")
        for source, want in (("def add(a, b):\n    return a + b\n", "1/1 docstring examples pass"),
                             ("def add(a, b):\n    return a * b\n", "0/1 docstring examples pass"),
                             ("def add(a, b:\n", "0/1 docstring examples pass"),
                             ("if __name__ == '__main__':\n    input()\ndef add(a, b):\n    return a + b\n",
                              "1/1 docstring examples pass")):
            with open(candidate, "w") as fh:
                fh.write(source)
            r = subprocess.run([sys.executable, args["examples"], os.path.join(args["pristine"], "solutions.py"),
                                candidate], capture_output=True, text=True, timeout=30, stdin=subprocess.PIPE)
            self.assertEqual(r.stdout.strip().splitlines()[-1], want, r.stdout + r.stderr)
            if "add(a, b:" in source:
                self.assertTrue(r.stdout.startswith("MISMATCH import"), r.stdout)

    def test_the_scratch_folder_is_named_as_the_graph_names_it(self):
        self.assertEqual(graph.scratch_of("/Users/x/my proj"), "/tmp/agentaus-graph/Users_x_my_proj-dyn")
        self.assertEqual(graph.scratch_of(""), "/tmp/agentaus-graph/task-dyn")

    def test_an_agent_past_its_time_is_asked_for_its_result_without_tools(self):
        from agentaus_bridge.config import settings
        prompt = f"{graph.MARK}\n  ## Your skill: solve\n  Implement add() - {id(self)}"
        first = {"tools": [tool("Bash")], "messages": [user(prompt)]}
        later = {"tools": [tool("Bash")], "tool_choice": {"type": "auto"}, "messages": [
            user(prompt),
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                               "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]}]}
        self.assertEqual(graph.overdue(first, now=1000.0), (0.0, False))
        self.assertEqual(graph.overdue(later, now=1000.0 + settings.agentaus_graph_agent_max_s - 1)[0], 0.0)
        over, news = graph.overdue(later, now=1000.0 + settings.agentaus_graph_agent_max_s + 30)
        self.assertEqual((over, news), (30.0, True))
        self.assertFalse(graph.overdue(later, now=1000.0 + settings.agentaus_graph_agent_max_s + 60)[1],
                         "said once per agent")
        stopped = graph.time_up(later)
        self.assertNotIn("tools", stopped)
        self.assertNotIn("tool_choice", stopped)
        self.assertEqual(len(stopped["messages"]), 3, "no second user turn in a row")
        self.assertEqual(stopped["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertEqual(stopped["messages"][-1]["content"][-1]["text"], graph.TIME_UP)
        self.assertEqual(graph.time_up(stopped), stopped, "a refit is not told twice")
        # The translated payload stays valid: the tool result, then the instruction.
        from agentaus_bridge.translate import anthropic_request_to_agentaus
        sent = anthropic_request_to_agentaus(stopped)
        self.assertEqual([m["role"] for m in sent["messages"]][-2:], ["tool", "user"])
        self.assertNotIn("tools", sent)
        # A retry with the same prompt starts its own clock; the writers get longer.
        self.assertEqual(graph.overdue(first, now=5000.0)[0], 0.0)
        assemble = {"messages": [user(f"{graph.MARK}\n  ## Your skill: assemble\n  {id(self)}"),
                                 {"role": "assistant", "content": "x"}, user("go on")]}
        graph.overdue({"messages": assemble["messages"][:1]}, now=0.0)
        self.assertEqual(graph.overdue(assemble, now=settings.agentaus_graph_agent_max_s + 60)[0], 0.0)
        self.assertGreater(graph.overdue(assemble, now=settings.agentaus_graph_writer_max_s + 60)[0], 0)

    def test_the_planned_graph_runs_end_to_end(self):
        import json, shutil, subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(here)
        r = subprocess.run([node, os.path.join(here, "graph_dynamic_dry_run.mjs"),
                            os.path.join(root, "agentaus_bridge", "graphs", "dynamic.js"),
                            os.path.join(root, "agentaus_bridge", "graphs", "skills")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        out = json.loads(r.stdout)
        self.assertEqual(out["missingSkill"], 0)
        # The plan's choices, not the script's: 3 solvers for the hard subtask, 1 for the easy.
        solvers = lambda n: len({l for l in out["labels"] if l.startswith(f"solve-{n}.") and l.split(".")[-1].isdigit()})
        self.assertEqual(solvers(1), 3)
        self.assertEqual(solvers(2), 1)
        self.assertIn("solve-1.redo", out["labels"])
        self.assertLess(out["labels"].index("merge-1"), out["labels"].index("solve-2.1"))
        self.assertIn("# Result:", out["result"])


class TheGraphRoundD9Fixes(unittest.TestCase):
    """The bridge-side fixes from the d9 benchmark diagnosis."""

    def graph_prompt(self, title, *lines, backup=False, skill="assemble"):
        # As the harness sends it: a frame line, then every line of the prompt indented.
        text = "\n".join([graph.MARK, f"# {title}", "",
                          "**Project folder:** `/Users/x/proj` - every input file is there.",
                          "", "---", f"## Your skill: {skill}", "", "# Skill", "---", "", *lines])
        if backup:
            text += "\n\n**Backup attempt:** the previous agent returned nothing usable."
        return ("[Workflow harness - computed task] The computed task text follows:\n"
                + "\n".join("  " + l for l in text.split("\n")))

    def test_an_agents_clock_survives_reminders_added_to_its_first_message(self):
        from agentaus_bridge.config import settings
        prompt = self.graph_prompt(f"Solve subtask 3: `count_top` (solver 1) {id(self)}", skill="solve")
        first = {"messages": [user(prompt)]}
        # Claude Code adds reminder blocks to the opening message as a run goes on.
        grown = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>new tools</system-reminder>"},
                                             {"type": "text", "text": prompt}]}
        later = {"messages": [grown, {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}]}
        with self.assertLogs("agentaus-bridge", "INFO") as logs:
            graph.overdue(first, now=0.0)
            over, news = graph.overdue(later, now=settings.agentaus_graph_agent_max_s + 10.0)
        self.assertEqual((over, news), (10.0, True))
        self.assertEqual(sum("graph agent clock started: Solve subtask 3" in l for l in logs.output), 1)
        # The backup attempt is another agent with its own clock.
        backup = {"messages": [user(self.graph_prompt(f"Solve subtask 3: `count_top` (solver 1) {id(self)}",
                                                      backup=True, skill="solve")), *later["messages"][1:]]}
        graph.overdue({"messages": backup["messages"][:1]}, now=500.0)
        self.assertEqual(graph.overdue(backup, now=600.0)[0], 0.0)

    def test_the_graph_waits_longer_than_the_bridge_lets_an_agent_run(self):
        import json
        from agentaus_bridge.config import settings
        call = {"id": "w", "name": "Workflow", "arguments": json.dumps({"script": "x"})}
        args = json.loads(graph.expand(call, "ultracode: do it", None)["arguments"])["args"]
        self.assertEqual(args["deadline_ms"], (settings.agentaus_graph_agent_max_s + 120) * 1000)
        self.assertGreater(args["deadline_ms"], settings.agentaus_graph_agent_max_s * 1000)

    def test_every_question_the_documents_ask_is_listed(self):
        import json, shutil
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd, True)
        self.addCleanup(shutil.rmtree, graph.scratch_of(cwd), True)
        os.makedirs(os.path.join(cwd, "past"))
        with open(os.path.join(cwd, "RFQ.md"), "w") as fh:
            fh.write("# Request for Quotation\n\nAnswer every question below.\n\n"
                     "Q1. Identify your organisation: legal entity name and ABN.\n"
                     "**Q2.** Describe a comparable engagement you have delivered.\n"
                     "- Q3: Describe your method and the evidence base it draws on.\n"
                     "## Q4 - Provide a fixed price showing GST\n\n"
                     "### Q5\n\nState your position on using psychometric assessments.\n"
                     "Q1 2024 revenue was up, which is not a question.\n")
        with open(os.path.join(cwd, "past", "old_response.md"), "w") as fh:
            fh.write("Q1. Some other tender's question one, from years ago.\n"
                     "Q6. Describe your accreditation with any assessment publisher.\n")
        args = graph.prepare(cwd)
        self.assertEqual([q["n"] for q in args["questions"]], [1, 2, 3, 4, 5, 6])
        by = {q["n"]: q for q in args["questions"]}
        self.assertEqual(by[1], {"n": 1, "text": "Q1. Identify your organisation: legal entity name and ABN.",
                                 "file": "RFQ.md", "line": 5})
        self.assertEqual(by[2]["text"], "Q2. Describe a comparable engagement you have delivered.")
        self.assertEqual(by[4]["text"], "Q4. Provide a fixed price showing GST")
        self.assertEqual(by[5]["text"], "Q5. State your position on using psychometric assessments.")
        self.assertEqual((by[6]["file"], by[6]["line"]), (os.path.join("past", "old_response.md"), 2))
        json.dumps(args["questions"])

    def test_a_function_with_no_examples_is_not_reported_as_passing(self):
        import shutil, subprocess
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        runner = graph._write(os.path.join(d, "examples.py"), graph.EXAMPLES_SCRIPT)
        spec = os.path.join(d, "spec.py")
        with open(spec, "w") as fh:
            fh.write('def count_top(xs, k):\n    """How many ranks are in the top k."""\n    return 0\n')
        r = subprocess.run([sys.executable, runner, spec, spec, "count_top"], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.stdout.strip().splitlines()[-1],
                         "no docstring examples - write the docstring's prose examples as asserts yourself")
        self.assertNotIn("0/0", r.stdout)

    def test_text_writes_get_plain_characters(self):
        import json
        nb = "GST‑registered since 3 July 【README】 − 5"
        calls = [
            {"name": "Write", "arguments": json.dumps({"file_path": "/p/response.md", "content": nb})},
            {"name": "Edit", "arguments": {"file_path": "/p/notes.txt", "old_string": "a‑b",
                                           "new_string": "c‑d"}},
            {"name": "MultiEdit", "arguments": {"file_path": "/p/r.markdown", "edits": [
                {"old_string": "x y", "new_string": "x z"}]}},
            {"name": "Write", "arguments": {"file_path": "/p/solutions.py", "content": nb}},
            {"name": "Bash", "arguments": {"command": f"echo '{nb}' > a.md"}},
        ]
        self.assertEqual(graph.plain_text_writes(calls), 3)
        self.assertIsInstance(calls[0]["arguments"], str, "a JSON string stays a JSON string")
        self.assertEqual(json.loads(calls[0]["arguments"])["content"],
                         "GST-registered since 3 July [README] - 5")
        self.assertEqual(calls[1]["arguments"]["new_string"], "c-d")
        self.assertEqual(calls[1]["arguments"]["old_string"], "a‑b", "old_string must match the file")
        self.assertEqual(calls[2]["arguments"]["edits"], [{"old_string": "x y", "new_string": "x z"}])
        self.assertEqual(calls[3]["arguments"]["content"], nb, "code is left alone")
        self.assertIn(nb, calls[4]["arguments"]["command"])

    def test_only_graph_requests_get_plain_text_writes(self):
        from agentaus_bridge.config import settings
        saved, settings.agentaus_workflow_graph = settings.agentaus_workflow_graph, True
        try:
            agent = {"system": SUBAGENT_SYSTEM, "messages": [user(self.graph_prompt("Write it"))]}
            opted = {"tools": [tool("Workflow")], "messages": [user("ultracode: answer the RFQ")]}
            plain = {"tools": [tool("Write")], "messages": [user("tidy up notes.md")]}
            other = {"system": SUBAGENT_SYSTEM, "messages": [user("explore the repo")]}
            self.assertTrue(server._writes_plain_text(agent))
            self.assertTrue(server._writes_plain_text(opted))
            self.assertFalse(server._writes_plain_text(plain))
            self.assertFalse(server._writes_plain_text(other))
        finally:
            settings.agentaus_workflow_graph = saved

    def test_a_writer_may_not_replace_a_source_file_whole_with_bash(self):
        import shutil
        cwd = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cwd, True)
        solver = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, solver, True)
        for d in (cwd, solver):
            os.makedirs(os.path.join(d, "tools"))
            for rel in ("solutions.py", os.path.join("tools", "bank.py")):
                with open(os.path.join(d, rel), "w") as fh:
                    fh.write("x = 1\n")
        s = graph.scratch_of(cwd)
        blocked = [
            f"cp '{solver}/solutions.py' '{cwd}/solutions.py'",
            f"cp {solver}/solutions.py solutions.py",
            f"cp {solver}/tools/bank.py tools/",
            f"cp -f {solver}/solutions.py solutions.py",
            "cat > solutions.py <<'EOF'\nx = 2\nEOF",
            f"python3 {solver}/gen.py > tools/bank.py",
            "echo x | tee solutions.py",
            f"mv {solver}/solutions.py {cwd}/solutions.py",
            f"rsync -a {solver}/ {cwd}/",
            f"cp -R {solver}/. {cwd}/",
            f"bash -c \"cp {solver}/solutions.py solutions.py\"",
        ]
        allowed = [
            f"rsync -ai --checksum --exclude .git --exclude '*.pyc' {s}/pristine/ {cwd}/ | head -40; echo restored",
            f"cp -R {s}/before-fix/. {cwd}/ && echo undone",
            f"cd {cwd} && patch -p1 --forward -F 3 < {solver}/fix.patch",
            f"python3 - {cwd}/solutions.py add {solver}/add.py <<'PY'\nimport sys\nopen(sys.argv[1], 'w').write('x')\nPY",
            f"sed -i '' 's/a/b/' solutions.py",
            f"cp {solver}/solutions.py {cwd}/new_module.py",
            f"cp {solver}/solutions.py {s}/s1/1/solutions.py",
            "cat >> solutions.py <<'EOF'\n\ndef extra():\n    pass\nEOF",
            f"python3 solutions.py > {cwd}/output.txt 2>&1",
            f"cat {s}/sections/*.md > {cwd}/response.md",
        ]
        for command in blocked:
            self.assertTrue(graph.overwrites_source(command, cwd), command)
        for command in allowed:
            self.assertFalse(graph.overwrites_source(command, cwd), command)

        def body(role):
            return {"messages": [user(f"{graph.MARK}\n  ## Your skill: {role}\n")]}
        bash = [{"name": "Bash", "arguments": {"command": blocked[0]}}]
        for role in ("assemble", "fix"):
            [(_, why)] = graph.guarded_writes(bash, body(role), cwd)
            self.assertIn("replaces an existing source file in the project whole", why)
        self.assertEqual(graph.guarded_writes(bash, {"messages": [user("fix it")]}, cwd), [])
        # rsync and ditto now count as writes for the read-only roles too.
        self.assertTrue(graph.bash_writes_into(f"rsync -a --exclude .git {solver}/ ./", cwd))
        self.assertEqual(graph.bash_writes_into(f"rsync -a ./ {solver}/copy/", cwd), [])

    # --- Exact-command agents ---------------------------------------------------------------

    COMMAND = ("mkdir -p /tmp/s && cat > /tmp/s/01.md <<'AGENTAUS_SECTION_END'\n## Q1\n\n"
               "```python\nprint(1)\n```\n\nAGENTAUS_SECTION_END\nwc -l /tmp/s/01.md")

    def exact_body(self, *more, tools=("Bash", "Read")):
        prompt = self.graph_prompt("Write section 1 of 3 of response.md",
                                   "Run exactly this command - nothing else - then reply with what it printed:",
                                   "", graph.EXACT, "```bash", *self.COMMAND.split("\n"), "```", "",
                                   "Your final message is your result.")
        return {"model": "agentaus", "max_tokens": 64, "system": SUBAGENT_SYSTEM,
                "tools": [tool(n) for n in tools], "messages": [user(prompt), *more]}

    def ran(self, call_id="toolu_1", output="3 /tmp/s/01.md\n"):
        return ({"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": "Bash",
                                                   "input": {"command": self.COMMAND}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                              "content": [{"type": "text", "text": output}]}]})

    def test_the_exact_command_is_read_back_as_the_graph_wrote_it(self):
        self.assertEqual(graph.exact_command(self.exact_body()), ("run", self.COMMAND))
        self.assertEqual(graph.exact_command(self.exact_body(*self.ran())), ("report", "3 /tmp/s/01.md"))
        self.assertEqual(graph.exact_command(self.exact_body(*self.ran(output=""))), ("report", "(no output)"))
        long = graph.exact_command(self.exact_body(*self.ran(output="x" * 9000)))
        self.assertEqual(len(long[1]), graph.EXACT_OUTPUT_CHARS)
        # Any other shape goes to the model.
        self.assertIsNone(graph.exact_command(self.exact_body(tools=("Read",))))
        assistant, result = self.ran()
        other = {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                                   "input": {"command": "ls"}}]}
        self.assertIsNone(graph.exact_command(self.exact_body(other, result)))
        self.assertIsNone(graph.exact_command(self.exact_body(assistant, result, other, result)))
        self.assertIsNone(graph.exact_command(self.exact_body(assistant, user("go on"))))
        plain = {"tools": [tool("Bash")], "messages": [user(self.graph_prompt("Solve it", "```bash", "ls", "```"))]}
        self.assertIsNone(graph.exact_command(plain))

    def post(self, body, stream):
        from fastapi.testclient import TestClient
        def upstream(*a, **k):
            raise AssertionError("an exact-command agent must not reach the model")
        with mock.patch.object(server, "_post_with_retry", upstream), \
                mock.patch.object(server, "_agentaus_event_stream", upstream), \
                TestClient(server.app) as client:
            return client.post("/v1/messages", json={**body, "stream": stream})

    def events(self, response):
        import json
        out = []
        for chunk in response.text.split("\n\n"):
            lines = dict(l.split(": ", 1) for l in chunk.strip().split("\n") if ": " in l)
            if "data" in lines:
                out.append(json.loads(lines["data"]))
        return out

    def test_an_exact_command_agent_runs_without_the_model(self):
        import json
        r = self.post(self.exact_body(), stream=False)
        self.assertEqual(r.status_code, 200, r.text)
        message = r.json()
        self.assertEqual(message["stop_reason"], "tool_use")
        [block] = message["content"]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["name"], "Bash")
        self.assertTrue(block["id"].startswith("toolu_"))
        self.assertEqual(block["input"], {"command": self.COMMAND, "description": "Run the graph's exact command",
                                          "timeout": 600000})
        r = self.post(self.exact_body(*self.ran()), stream=False)
        self.assertEqual(r.json()["stop_reason"], "end_turn")
        self.assertEqual(r.json()["content"], [{"type": "text", "text": "3 /tmp/s/01.md"}])

        r = self.post(self.exact_body(), stream=True)
        self.assertEqual(r.status_code, 200, r.text)
        events = self.events(r)
        self.assertEqual([e["type"] for e in events][0], "message_start")
        self.assertEqual([e["type"] for e in events][-2:], ["message_delta", "message_stop"])
        start = next(e for e in events if e["type"] == "content_block_start")
        self.assertEqual((start["content_block"]["type"], start["content_block"]["name"], start["content_block"]["input"]),
                         ("tool_use", "Bash", {}))
        sent = "".join(e["delta"]["partial_json"] for e in events if e["type"] == "content_block_delta")
        self.assertEqual(json.loads(sent)["command"], self.COMMAND)
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")

        r = self.post(self.exact_body(*self.ran()), stream=True)
        events = self.events(r)
        text = "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta")
        self.assertEqual(text, "3 /tmp/s/01.md")
        self.assertEqual(events[-2]["delta"]["stop_reason"], "end_turn")


class RewritesSeeTheWholeAnswer(unittest.TestCase):
    def test_an_answer_longer_than_a_rewrite_sees_is_kept(self):
        long_answer = "x" * (server._REWRITE_CHARS + 1)
        self.assertTrue(server._too_long_to_rewrite(long_answer, "self-review"))
        self.assertFalse(server._too_long_to_rewrite("short", "self-review"))


if __name__ == "__main__":
    unittest.main()
