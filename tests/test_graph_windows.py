"""The agent graph's scratch folder on Windows.

Measured (24 Nov 2026, Windows 11): the bridge wrote each run's snapshot to /tmp/agentaus-graph,
which Python reads as C:\\tmp, while the graph's agents run Git Bash, which reads /tmp as
%TEMP%. Every solver's copy failed with "cp: cannot stat", and the guard then refused the
solvers' paths. These tests pretend to be Windows on any OS through graph._on_windows.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import graph  # noqa: E402

TEMP = "C:\\Users\\me\\AppData\\Local\\Temp"
CWD = "c:\\Users\\me\\GitHub Repo\\proj"
OWN = "C:/Users/me/AppData/Local/Temp/agentaus-graph/c_Users_me_GitHub_Repo_proj-dyn"


def windows():
    return mock.patch.multiple(graph, _on_windows=lambda: True), \
        mock.patch.object(graph.tempfile, "gettempdir", lambda: TEMP)


class ScratchRoot(unittest.TestCase):
    def test_posix_is_unchanged(self):
        with mock.patch.object(graph, "_on_windows", lambda: False):
            self.assertEqual(graph.scratch_root(), "/tmp/agentaus-graph")
            self.assertEqual(graph.scratch_of("/Users/me/proj"), "/tmp/agentaus-graph/Users_me_proj-dyn")

    def test_windows_uses_temp_with_forward_slashes(self):
        a, b = windows()
        with a, b:
            self.assertEqual(graph.scratch_root(), "C:/Users/me/AppData/Local/Temp/agentaus-graph")
            self.assertEqual(graph.scratch_of(CWD), OWN)

    def test_the_graph_is_told_the_root(self):
        call = {"name": "Workflow", "arguments": {"script": "..."}}
        with mock.patch.object(graph, "prepare", return_value={}):
            args = json.loads(graph.expand(call, "fix it", "/Users/me/proj")["arguments"])["args"]
        self.assertEqual(args["scratch_root"], graph.scratch_root())


class TheGuard(unittest.TestCase):
    def test_own_scratch_in_any_spelling_is_allowed(self):
        a, b = windows()
        with a, b:
            for path in (OWN + "/s1/1/repo", OWN.replace("/", "\\") + "\\pristine",
                         OWN.lower() + "/try", "/tmp/agentaus-graph/c_Users_me_GitHub_Repo_proj-dyn/s2"):
                self.assertIsNone(graph.foreign_scratch(json.dumps({"command": f"cp -R '{path}' x"}), CWD), path)

    def test_another_runs_scratch_is_still_refused(self):
        a, b = windows()
        with a, b:
            other = "C:/Users/me/AppData/Local/Temp/agentaus-graph/c_Users_me_old_proj-dyn/s1"
            self.assertEqual(graph.foreign_scratch(json.dumps({"command": f"ls '{other}'"}), CWD),
                             "C:/Users/me/AppData/Local/Temp/agentaus-graph/c_Users_me_old_proj-dyn")

    def test_restoring_from_the_snapshot_is_not_an_overwrite(self):
        a, b = windows()
        with a, b:
            command = f"cp '{OWN}/pristine/bank.py' '{CWD}\\bank.py'"
            self.assertFalse(graph.overwrites_source(command, CWD))


class AMainLoopIsNeverAGraphAgent(unittest.TestCase):
    """Measured (25 Nov 2026): a report quoting the docs' `[agentaus-graph]` line reached the
    main session, and the bridge then focused the main loop like a graph agent - no Agent, no
    Workflow, no ToolSearch."""

    def body(self, first):
        tools = [{"name": n, "input_schema": {"type": "object"}}
                 for n in ("Agent", "Workflow", "ToolSearch", "Bash", "Read", "Edit")]
        quoted = ("Knowledgebase.md §13 (the `[agentaus-graph]` mark, graphs/solve.js) and the "
                  "`[think-only]` mark are documented.")
        return {"system": "You are Claude Code.", "tools": tools, "messages": [
            {"role": "user", "content": first},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a1", "name": "Agent",
                                               "input": {"prompt": "audit"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a1", "content": "ok"},
                                         {"type": "text", "text": quoted}]}]}

    def test_a_main_loop_quoting_the_mark_keeps_its_tools(self):
        self.assertFalse(graph.is_graph_task(self.body("Update the docs.")))

    def test_a_graph_agent_is_still_recognised(self):
        body = self.body(f"{graph.MARK}\n  ## Your skill: solve\n  Implement add()")
        self.assertTrue(graph.is_graph_task(body))
        focused, dropped = graph.focus(body)
        self.assertIn("Agent", dropped)


class PreparedPaths(unittest.TestCase):
    def test_paths_handed_to_agents_have_no_backslashes(self):
        project = tempfile.mkdtemp()
        root = tempfile.mkdtemp().replace("\\", "/")
        try:
            with open(os.path.join(project, "bank.py"), "w", encoding="utf-8") as fh:
                fh.write("def total(xs):\n    return sum(xs)\n")
            with mock.patch.object(graph, "scratch_root", lambda: root):
                extra = graph.prepare(project)
            self.assertTrue(extra["pristine"].startswith(root + "/"))
            self.assertNotIn("\\", extra["pristine"])
            self.assertNotIn("\\", extra["examples"])
            self.assertTrue(os.path.isfile(os.path.join(extra["pristine"], "bank.py")))
        finally:
            shutil.rmtree(project, ignore_errors=True)
            shutil.rmtree(root, ignore_errors=True)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TheGraphsAgree(unittest.TestCase):
    """dynamic.js and solve.js build the scratch path themselves; it must be the bridge's."""

    def js_scratch(self, script: str, args: dict) -> str:
        with open(os.path.join(os.path.dirname(graph.__file__), "graphs", script), encoding="utf-8") as fh:
            source = fh.read()
        lines = [l for l in source.splitlines()
                 if re.match(r"const (CWD|SCRATCH_ROOT|SCRATCH) =", l)]
        program = f"const args = {json.dumps(args)};\n" + "\n".join(lines) + "\nconsole.log(SCRATCH)"
        return subprocess.run(["node", "-e", program], capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_dynamic_js_matches_scratch_of(self):
        a, b = windows()
        with a, b:
            args = {"cwd": CWD, "scratch_root": graph.scratch_root()}
            self.assertEqual(self.js_scratch("dynamic.js", args), graph.scratch_of(CWD))

    def test_an_older_bridge_without_the_root_still_gets_tmp(self):
        self.assertEqual(self.js_scratch("dynamic.js", {"cwd": "/Users/me/proj"}),
                         "/tmp/agentaus-graph/Users_me_proj-dyn")
        self.assertEqual(self.js_scratch("solve.js", {"cwd": "/Users/me/proj"}),
                         "/tmp/agentaus-graph/Users_me_proj")


if __name__ == "__main__":
    unittest.main()
