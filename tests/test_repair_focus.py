"""Fixes from the benchmark pilot, run through the real Claude Code binary.

Four failures showed up in both Agentaus arms and none in Opus: tool names spelt wrong
(`agentaus_read`, `Python`), right tools called with wrong field names (`cmd`, `path`),
turns that ended on an announcement ("We will write file at same directory."), and a
system note answered instead of the user's request. Each is pinned here.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import augment, repair  # noqa: E402
from agentaus_bridge.server import _partition_tool_calls, _validate_tool_calls  # noqa: E402
from agentaus_bridge.translate import anthropic_request_to_agentaus  # noqa: E402

KNOWN = {"Read", "Write", "Edit", "Bash", "agentaus_search"}
SCHEMAS = {
    "Bash": {"type": "object", "properties": {"command": {"type": "string"},
                                              "description": {"type": "string"}},
             "required": ["command"]},
    "Write": {"type": "object", "properties": {"file_path": {"type": "string"},
                                               "content": {"type": "string"}},
              "required": ["file_path", "content"]},
    "Read": {"type": "object", "properties": {"file_path": {"type": "string"}},
             "required": ["file_path"]},
}


def args(call):
    return json.loads(call["arguments"])


class Names(unittest.TestCase):
    def test_observed_misspellings_resolve(self):
        for asked, want in [("agentaus_read", "Read"), ("read_file", "Read"),
                            ("functions.Write", "Write"), ("run_command", "Bash"),
                            ("Python", "Bash"), ("search", "agentaus_search")]:
            with self.subTest(asked=asked):
                self.assertEqual(repair.resolve(asked, KNOWN), want)

    def test_nothing_resolves_to_a_tool_that_was_not_offered(self):
        self.assertIsNone(repair.resolve("web_search", KNOWN))
        self.assertIsNone(repair.resolve("summon_the_oracle", KNOWN))


class Arguments(unittest.TestCase):
    def fix(self, name, arguments, asked=None, cwd="/repo"):
        mine, theirs, invented = _partition_tool_calls(
            [{"id": "1", "name": asked or name, "arguments": json.dumps(arguments)}], KNOWN)
        self.assertEqual(invented, [])
        broken = _validate_tool_calls(theirs, SCHEMAS, cwd)
        return theirs[0], broken

    def test_field_aliases(self):
        call, broken = self.fix("Bash", {"cmd": "ls"})
        self.assertEqual((broken, args(call)["command"]), ([], "ls"))
        call, broken = self.fix("Write", {"path": "/repo/a.R", "text": "x <- 1"})
        self.assertEqual(broken, [])
        self.assertEqual(args(call), {"file_path": "/repo/a.R", "content": "x <- 1"})

    def test_relative_paths_become_absolute(self):
        call, _ = self.fix("Write", {"file_path": "answers.json", "content": "{}"})
        self.assertEqual(args(call)["file_path"], "/repo/answers.json")

    def test_python_runs_through_bash(self):
        call, broken = self.fix("Bash", {"code": "print(1 + 1)"}, asked="Python")
        self.assertEqual(broken, [])
        self.assertEqual(call["name"], "Bash")
        command = args(call)["command"]
        self.assertTrue(command.startswith("python3 - <<'PY_EOF'"), command)
        self.assertIn("print(1 + 1)", command)

    def test_an_existing_field_is_never_overwritten(self):
        call, _ = self.fix("Write", {"file_path": "/repo/a", "content": "keep", "text": "no"})
        self.assertEqual(args(call)["content"], "keep")

    def test_what_cannot_be_repaired_is_still_reported(self):
        _, broken = self.fix("Write", {"content": "orphan"})
        self.assertEqual(len(broken), 1)


def _tool(name):
    return {"name": name, "description": name, "input_schema": {"type": "object"}}


class Focus(unittest.TestCase):
    def body(self, text, *names, history=()):
        return {"tools": [_tool(n) for n in names],
                "messages": list(history) + [{"role": "user", "content": text}]}

    def test_non_coding_tools_are_held_back(self):
        focused, dropped = augment.focus_tools(self.body("fix the bug", "Read", "Bash",
                                                         "Artifact", "CronCreate"))
        self.assertEqual([t["name"] for t in focused["tools"]], ["Read", "Bash"])
        self.assertEqual(sorted(dropped), ["Artifact", "CronCreate"])

    def test_a_tool_the_user_names_stays(self):
        focused, _ = augment.focus_tools(self.body("publish it as an Artifact", "Read", "Artifact"))
        self.assertIn("Artifact", [t["name"] for t in focused["tools"]])

    def test_a_tool_already_used_stays(self):
        used = {"role": "assistant", "content": [{"type": "tool_use", "id": "t",
                                                  "name": "CronCreate", "input": {}}]}
        focused, _ = augment.focus_tools(self.body("again", "Read", "CronCreate", history=[used]))
        self.assertIn("CronCreate", [t["name"] for t in focused["tools"]])


class SystemNotesJoinTheSystemPrompt(unittest.TestCase):
    def body(self):
        return {"system": "You are Claude Code.",
                "messages": [
                    {"role": "user", "content": "write model.R"},
                    {"role": "system", "content": [{"type": "text", "text":
                        "# Environment\n - Primary working directory: /work/repo\n"}]},
                ]}

    def test_the_conversation_ends_on_the_user(self):
        out = anthropic_request_to_agentaus(self.body())
        self.assertEqual([m["role"] for m in out["messages"]], ["system", "user"])
        self.assertIn("Primary working directory: /work/repo", out["messages"][0]["content"])
        self.assertIn("You are Claude Code.", out["messages"][0]["content"])

    def test_the_working_directory_is_found_there(self):
        self.assertEqual(augment.working_directory_of(self.body()), "/work/repo")


class StalledTurns(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(augment.read_turn_verdict("STALLED"), "stalled")
        self.assertEqual(augment.read_turn_verdict("**REFUSAL**"), "refusal")
        self.assertEqual(augment.read_turn_verdict("ANSWER"), "answer")
        self.assertEqual(augment.read_turn_verdict("maybe?"), "answer")

    def test_the_judge_sees_the_request(self):
        prompt = augment.CLASSIFY_REFUSAL_INSTRUCTION.format(
            request="write answers.json", answer="We will write file at same directory.")
        self.assertIn("write answers.json", prompt)
        self.assertIn("STALLED", prompt)


if __name__ == "__main__":
    unittest.main()
