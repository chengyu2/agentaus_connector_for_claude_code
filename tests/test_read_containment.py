"""Where the bridge's own file reads may go.

agentaus_search, agentaus_inventory, agentaus_investigate and agentaus_zoom read files without
Claude Code's permission prompts, so they are confined to the session's working directory (or
AGENTAUS_SEARCH_ROOTS). These tests cover the ways that boundary was wrong after it was
introduced: a symlink inside the project leading out of it, a working directory with a space
in it cut short at the space, and a native Windows working directory not recognised at all.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock
from xml_style import setUpModule, tearDownModule  # noqa: E402,F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import augment, tools  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402
from windows_os import Proxy, windows_os  # noqa: E402

OUTSIDE = "rotation token hunter2-outside-the-project"


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def responder():
    """A stub Agentaus that records every prompt and finds nothing."""
    seen: list[str] = []

    async def call(prompt: str) -> str:
        seen.append(prompt)
        if "<question>" in prompt and "<excerpt" not in prompt:
            return "rotation\ntoken\nhunter2"
        return "NONE"

    call.seen = seen  # type: ignore[attr-defined]
    return call


@unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "POSIX symlinks")
class ASymlinkCannotLeadOut(unittest.TestCase):
    """`proj/notes.txt -> ../other/plans.txt`: the project path passes the check, the file it
    opens does not belong to the project."""

    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.base.name)
        self.proj = os.path.join(base, "proj")
        self.other = os.path.join(base, "other")
        os.makedirs(os.path.join(self.other, "deep"))
        os.makedirs(self.proj)
        with open(os.path.join(self.other, "plans.txt"), "w") as fh:
            fh.write(OUTSIDE + "\n")
        with open(os.path.join(self.other, "deep", "more.txt"), "w") as fh:
            fh.write(OUTSIDE + "\n")
        with open(os.path.join(self.proj, "a.py"), "w") as fh:
            fh.write("rotation = 'inside'\n")
        with open(os.path.join(self.proj, "real.txt"), "w") as fh:
            fh.write("rotation token kept inside\n")
        os.symlink(os.path.join(self.other, "plans.txt"), os.path.join(self.proj, "notes.txt"))
        os.symlink(os.path.join(self.other, "deep"), os.path.join(self.proj, "linked-dir"))
        os.symlink(os.path.join(self.proj, "real.txt"), os.path.join(self.proj, "alias.txt"))

    def tearDown(self):
        self.base.cleanup()

    def leaked(self, call) -> list:
        return [p for p in call.seen if "hunter2-outside" in p]

    def test_search_does_not_read_a_link_out_of_the_project(self):
        call = responder()
        out = run(tools.run_search("where is the rotation token", self.proj, None, call,
                                   default_path=self.proj))
        self.assertEqual(self.leaked(call), [])
        self.assertNotIn("hunter2-outside", out)
        self.assertNotIn("notes.txt", out)

    def test_inventory_does_not_list_a_link_out_of_the_project(self):
        out = run(tools.run_inventory(self.proj, None, default_path=self.proj))
        self.assertIn("a.py", out)
        self.assertNotIn("notes.txt", out)
        self.assertNotIn("hunter2-outside", out)

    def test_investigate_does_not_read_a_link_out_of_the_project(self):
        call = responder()
        run(tools.run_investigate("where is the rotation token", self.proj, call,
                                  default_path=self.proj))
        self.assertEqual(self.leaked(call), [])

    def test_zoom_refuses_a_link_out_of_the_project(self):
        out = run(tools.run_zoom(os.path.join(self.proj, "notes.txt"), 1, 1,
                                 default_path=self.proj))
        self.assertIn("outside the session's working directory", out)

    def test_a_linked_directory_is_not_walked(self):
        files = tools.enumerate_files(self.proj, default_path=self.proj)
        self.assertFalse([f for f in files if "linked-dir" in f], files)

    def test_a_windows_junction_out_of_the_project_is_not_walked(self):
        """os.walk descends a junction - it is not a symlink to os.path.islink. Emulated: a
        real folder that os.path.isjunction calls a junction and realpath resolves outside."""
        junction = os.path.join(self.proj, "junction")
        os.makedirs(junction)
        with open(os.path.join(junction, "j.txt"), "w") as fh:
            fh.write(OUTSIDE + "\n")

        def realpath(p, **kw):
            if p == junction or p.startswith(junction + os.sep):
                return os.path.join(self.other, "deep") + p[len(junction):]
            return os.path.realpath(p, **kw)

        path = Proxy(os.path, isjunction=lambda p: p == junction, realpath=realpath)
        with mock.patch.object(tools, "os", Proxy(os, path=path)):
            files = tools.enumerate_files(self.proj, default_path=self.proj)
        self.assertFalse([f for f in files if "junction" in f], files)
        self.assertIn(os.path.join(self.proj, "a.py"), files)

    def test_a_link_that_stays_inside_the_project_is_still_read(self):
        files = {os.path.basename(f) for f in tools.enumerate_files(self.proj, default_path=self.proj)}
        self.assertIn("alias.txt", files)
        self.assertIn("real.txt", files)

    def test_a_link_into_another_configured_root_is_read(self):
        previous = settings.agentaus_search_roots
        settings.agentaus_search_roots = os.pathsep.join([self.proj, self.other])
        try:
            files = {os.path.basename(f) for f in tools.enumerate_files(self.proj)}
        finally:
            settings.agentaus_search_roots = previous
        self.assertIn("notes.txt", files)


class TheWorkingDirectory(unittest.TestCase):
    """The path Claude Code names is the boundary, so it has to be read whole."""

    def test_a_path_with_spaces_is_read_to_the_end_of_the_line(self):
        system = ("Here is useful information about the environment you are running in:\n"
                  "<env>\nPrimary working directory: /Users/me/GitHub Repo/proj\n"
                  "Is directory a git repo: Yes\n</env>")
        self.assertEqual(augment.working_directory(system), "/Users/me/GitHub Repo/proj")

    def test_other_phrasings_keep_their_spaces(self):
        for text in ("Working directory: /Users/me/My Projects/app  ",
                     "cwd: /Users/me/My Projects/app",
                     "Current directory: /Users/me/My Projects/app.",
                     'cwd: "/Users/me/My Projects/app", "shell": "zsh"',
                     "Working directory: `/Users/me/My Projects/app`"):
            self.assertEqual(augment.working_directory(text), "/Users/me/My Projects/app", text)

    def test_paths_without_spaces_are_unchanged(self):
        self.assertEqual(augment.working_directory("Working directory: /work/repo\n"), "/work/repo")
        self.assertEqual(augment.working_directory("cwd: ~/repo"), "~/repo")
        self.assertIsNone(augment.working_directory("Working directory: relative/path"))

    def test_a_mention_before_the_real_line_is_skipped(self):
        system = ("Your working directory: see below.\n"
                  "Primary working directory: /Users/me/GitHub Repo/proj")
        self.assertEqual(augment.working_directory(system), "/Users/me/GitHub Repo/proj")

    def test_native_windows_paths_are_recognised(self):
        for path in ("C:\\Users\\me\\GitHub Repo\\proj", "c:/Users/me/proj",
                     "\\\\fileserver\\share\\proj"):
            self.assertEqual(
                augment.working_directory(f"Primary working directory: {path}\n"), path)

    def test_a_spaced_working_directory_confines_reads_to_itself(self):
        """Cut at the space, `/…/GitHub Repo/proj` became `/…/GitHub`: the project was refused
        and its parent's other folders were readable."""
        with tempfile.TemporaryDirectory() as base:
            base = os.path.realpath(base)
            proj = os.path.join(base, "GitHub Repo", "proj")
            sibling = os.path.join(base, "GitHub", "other")
            for folder in (proj, sibling):
                os.makedirs(folder)
                with open(os.path.join(folder, "a.py"), "w") as fh:
                    fh.write("x = 1\n")
            cwd = augment.working_directory(f"Primary working directory: {proj}\n")
            inside = run(tools.run_inventory(proj, None, default_path=cwd))
            outside = run(tools.run_inventory(sibling, None, default_path=cwd))
        self.assertIn("a.py", inside)
        self.assertIn("outside the session's working directory", outside)


class NativeWindowsContainment(unittest.TestCase):
    """The bridge on native Windows, emulated on any OS by giving tools.py Windows' `os`."""

    CWD = "c:\\Users\\me\\GitHub Repo\\proj"

    def allowed(self, path: str, cwd: str | None = CWD) -> bool:
        with mock.patch.object(tools, "os", windows_os()):
            return tools._allowed_root(path, cwd)

    def test_the_working_directory_claude_code_names_is_the_root(self):
        cwd = augment.working_directory(f"Primary working directory: {self.CWD}\n")
        self.assertEqual(cwd, self.CWD)
        self.assertTrue(self.allowed("c:\\Users\\me\\GitHub Repo\\proj\\src\\a.py", cwd))

    def test_case_and_slash_direction_do_not_matter(self):
        for path in ("C:\\Users\\Me\\GitHub Repo\\proj\\a.py",
                     "C:/USERS/me/github repo/PROJ/src/a.py",
                     "C:\\Users\\me\\GitHub Repo\\proj"):
            self.assertTrue(self.allowed(path), path)

    def test_outside_paths_are_refused(self):
        for path in ("C:\\Users\\me\\GitHub Repo\\proj2\\a.py",
                     "C:\\Users\\me\\GitHub Repo\\proj\\..\\other\\a.py",
                     "C:\\Users\\me\\GitHub\\a.py",
                     "D:\\Users\\me\\GitHub Repo\\proj\\a.py",
                     "\\\\server\\share\\proj\\a.py"):
            self.assertFalse(self.allowed(path), path)

    def test_a_unc_working_directory_is_the_root(self):
        cwd = augment.working_directory("Primary working directory: \\\\server\\share\\proj")
        self.assertTrue(self.allowed("\\\\SERVER\\share\\proj\\a.py", cwd))
        self.assertFalse(self.allowed("\\\\server\\share\\other\\a.py", cwd))

    def test_search_roots_are_separated_by_semicolons(self):
        previous = settings.agentaus_search_roots
        settings.agentaus_search_roots = "C:\\work\\one;D:\\two"
        try:
            self.assertTrue(self.allowed("c:\\WORK\\one\\a.py", None))
            self.assertTrue(self.allowed("D:\\two\\b\\c.py", None))
            self.assertFalse(self.allowed("C:\\work\\a.py", None))
        finally:
            settings.agentaus_search_roots = previous


if __name__ == "__main__":
    unittest.main()
