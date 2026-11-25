"""The agent graph's scratch folder on Windows.

Measured (24 Nov 2026, Windows 11): the bridge wrote each run's snapshot to /tmp/agentaus-graph,
which Python reads as C:\\tmp, while the graph's agents run Git Bash, which reads /tmp as
%TEMP%. Every solver's copy failed with "cp: cannot stat", and the guard then refused the
solvers' paths. These tests pretend to be Windows on any OS by giving graph.py the `os` it would
have there (ntpath, a backslash separator, os.name "nt"; see windows_os.py), so a check that
only works with POSIX paths fails here too.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import graph  # noqa: E402
from windows_os import Proxy, windows_os  # noqa: E402

TEMP = "C:\\Users\\me\\AppData\\Local\\Temp"
CWD = "c:\\Users\\me\\GitHub Repo\\proj"
NAME = "c_Users_me_GitHub_Repo_proj-dyn"
OWN = f"C:/Users/me/AppData/Local/Temp/agentaus-graph/{NAME}"
# Files that exist in the emulated project, for os.path.isfile.
EXISTING = {ntpath.normcase(CWD + "\\bank.py")}


def windows(**path):
    """graph.py as it runs on Windows: its `os` is Windows' and %TEMP% is TEMP."""
    path_module = Proxy(ntpath, **path) if path else ntpath
    return mock.patch.object(graph, "os", windows_os(path_module)), \
        mock.patch.object(graph.tempfile, "gettempdir", lambda: TEMP)


def windows_with_project():
    return windows(isfile=lambda p: ntpath.normcase(p) in EXISTING)


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
    # The scratch root as each program spells it: the bridge, Windows with backslashes, any
    # case, Git Bash's /tmp and /c/ (what `pwd`, `realpath` and `find` print), and the 8.3
    # short name %TEMP% often holds (C:\\Users\\RUNNER~1\\...).
    ROOTS = ("C:/Users/me/AppData/Local/Temp/agentaus-graph",
             "C:\\Users\\me\\AppData\\Local\\Temp\\agentaus-graph",
             "c:/users/ME/appdata/local/temp/AGENTAUS-GRAPH",
             "/tmp/agentaus-graph",
             "/c/Users/me/AppData/Local/Temp/agentaus-graph",
             "C:/Users/ME~1/AppData/Local/Temp/agentaus-graph",
             "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\agentaus-graph")

    def refused(self, path: str) -> str | None:
        return graph.foreign_scratch(json.dumps({"command": f"cp -R '{path}' x"}), CWD)

    def test_own_scratch_in_any_spelling_is_allowed(self):
        a, b = windows()
        with a, b:
            for root in self.ROOTS:
                sep = "\\" if "\\" in root else "/"
                for path in (f"{root}/{NAME}/s1/1/repo", f"{root}{sep}{NAME}{sep}pristine",
                             f"{root}/{NAME.lower()}/try"):
                    self.assertIsNone(self.refused(path), path)

    def test_another_runs_scratch_is_refused_in_any_spelling(self):
        a, b = windows()
        with a, b:
            for root in self.ROOTS:
                sep = "\\" if "\\" in root else "/"
                found = self.refused(f"{root}{sep}c_Users_me_old_proj-dyn{sep}s1")
                self.assertIsNotNone(found, root)
                self.assertTrue(found.lower().endswith("c_users_me_old_proj-dyn"), found)

    def test_another_runs_scratch_is_named_in_full(self):
        a, b = windows()
        with a, b:
            other = "C:/Users/me/AppData/Local/Temp/agentaus-graph/c_Users_me_old_proj-dyn/s1"
            self.assertEqual(graph.foreign_scratch(json.dumps({"command": f"ls '{other}'"}), CWD),
                             "C:/Users/me/AppData/Local/Temp/agentaus-graph/c_Users_me_old_proj-dyn")

    def test_restoring_from_the_snapshot_is_not_an_overwrite(self):
        a, b = windows_with_project()
        with a, b:
            for root in self.ROOTS:
                for target in (f"'{CWD}\\bank.py'", "bank.py"):
                    command = f"cp '{root}/{NAME}/pristine/bank.py' {target}"
                    self.assertFalse(graph.overwrites_source(command, CWD), command)

    def test_replacing_a_source_file_whole_is_an_overwrite(self):
        """Without this, the exemption above passes for any command: on Windows no target used
        to count as inside the project, so nothing was ever an overwrite."""
        a, b = windows_with_project()
        with a, b:
            for command in ("cp /c/work/solutions.py 'C:\\Users\\me\\GitHub Repo\\proj\\bank.py'",
                            "cp /c/work/solutions.py 'C:/USERS/me/GitHub Repo/proj/bank.py'",
                            "cp /c/work/solutions.py '/c/Users/me/GitHub Repo/proj/bank.py'",
                            "cat /c/work/solutions.py > bank.py",
                            "cp '/tmp/agentaus-graph/c_Users_me_old_proj-dyn/pristine/bank.py' bank.py"):
                self.assertTrue(graph.overwrites_source(command, CWD), command)
            self.assertFalse(graph.overwrites_source("cp x.py /c/work/bank.py", CWD))

    def test_a_read_only_role_writing_into_the_project_is_seen(self):
        a, b = windows()
        with a, b:
            self.assertTrue(graph.bash_writes_into("echo x > notes.txt", CWD))
            self.assertTrue(graph.bash_writes_into("echo x > '/c/Users/me/GitHub Repo/proj/n.txt'", CWD))
            self.assertTrue(graph.bash_writes_into("echo x > 'C:\\USERS\\me\\GitHub Repo\\proj\\n.txt'", CWD))
            self.assertFalse(graph.bash_writes_into(f"echo x > '{OWN}/s1/n.txt'", CWD))
            self.assertFalse(graph.bash_writes_into("echo x > '/c/Users/me/GitHub Repo/proj2/n.txt'", CWD))

    def test_a_read_only_role_editing_the_project_is_refused(self):
        body = {"messages": [{"role": "user", "content": f"{graph.MARK}\n## Your skill: solve\n"}]}
        edit = [{"name": "Edit", "arguments": {"file_path": "C:\\Users\\me\\GitHub Repo\\proj\\bank.py"}}]
        copy = [{"name": "Edit", "arguments": {"file_path": f"{OWN}/s1/bank.py"}}]
        a, b = windows()
        with a, b:
            self.assertTrue(graph.guarded_writes(edit, body, CWD))
            self.assertFalse(graph.guarded_writes(copy, body, CWD))


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


def _docx(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", body)


class PreparedPaths(unittest.TestCase):
    def test_paths_handed_to_agents_have_no_backslashes(self):
        """On Windows os.path.join puts a "\\" into a path, which Git Bash misreads. graph.py gets
        an `os` whose join does that for paths under the scratch root - the agent-facing ones -
        and whose sep is "\\", so a path built with os.path.join, or a relative path not turned
        back to "/", shows up here. Paths into the project stay real, so the files are read."""
        project = tempfile.mkdtemp()
        root = tempfile.mkdtemp().replace("\\", "/")

        def join(first, *rest):
            return (ntpath.join if str(first).startswith(root) else posixpath.join)(first, *rest)

        try:
            with open(os.path.join(project, "bank.py"), "w", encoding="utf-8") as fh:
                fh.write("def total(xs):\n    return sum(xs)\n")
            _docx(os.path.join(project, "docs", "rfq.docx"), "Q1. What does the bank total?")
            nested = os.name == "posix"
            if nested:
                # How a document in a subfolder is named relative to the project on Windows:
                # with a backslash. A POSIX file name may contain one.
                _docx(os.path.join(project, "brief\\notes.docx"), "Q2. Which accounts count?")
            with mock.patch.object(graph, "os", Proxy(os, sep="\\", path=Proxy(os.path, join=join))), \
                    mock.patch.object(graph, "scratch_root", lambda: root):
                extra = graph.prepare(project)
                scratch = graph.scratch_of(project)
            self.assertTrue(extra["pristine"].startswith(root + "/"))
            self.assertNotIn("\\", extra["pristine"])
            self.assertNotIn("\\", extra["examples"])
            self.assertTrue(os.path.isfile(os.path.join(extra["pristine"], "bank.py")))
            texts = {t["text"] for t in extra["texts"]}
            expected = {f"{scratch}/inputs/{rel}" for rel in ["docs/rfq.docx.txt"] + (["brief/notes.docx.txt"] if nested else [])}
            self.assertEqual(texts, expected)
            for text in texts:
                self.assertTrue(os.path.isfile(text), text)
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
