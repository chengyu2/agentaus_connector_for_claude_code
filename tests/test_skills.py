"""Injecting the project's procedures, because this model will not fetch them itself."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from xml_style import setUpModule, tearDownModule  # noqa: E402,F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import skills  # noqa: E402
from agentaus_bridge.augment import skills_for, with_guidance  # noqa: E402


def project(*named: str) -> str:
    """A throwaway project directory carrying the named skills."""
    root = tempfile.mkdtemp()
    for name in named:
        folder = os.path.join(root, ".claude", "skills", name)
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "w") as handle:
            handle.write(
                f"---\nname: {name}\ndescription: what {name} is for\n---\n\n"
                f"# How to do {name}\n\nStep one.\n"
            )
    return root


def turn(text: str, cwd: str | None = None) -> dict:
    said = text if cwd is None else f"{text}\nfiles live under {cwd}/data"
    return {"tools": [{"name": "Bash"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": said}]}]}


class TestWhichProcedureApplies(unittest.TestCase):
    """Structural gates, because the model will not choose. Measured: `Skill` offered
    among 28 tools and named first in the guidance, and Agentaus never called it in five
    runs, while Opus called it on the same prompt in the same directory.
    """

    def test_a_named_data_file_asks_for_the_analysis_procedure(self):
        self.assertEqual(skills_for(turn("Analyse macro.csv")), ["analyse-data"])
        self.assertEqual(skills_for(turn("summarise Report.xlsx")), ["analyse-data"])

    def test_ordinary_code_work_asks_for_nothing(self):
        self.assertEqual(skills_for(turn("Refactor the retry helper in server.py")), [])

    def test_an_enumerated_request_asks_for_the_long_task_procedure(self):
        body = turn("Do this:\n1. load\n2. clean\n3. fit\n4. plot\n5. write up")
        self.assertIn("multi-step-work", skills_for(body))

    def test_a_short_list_is_not_a_long_task(self):
        self.assertEqual(skills_for(turn("Fix:\n1. typo\n2. spacing")), [])


class TestFindingTheSkillsDirectory(unittest.TestCase):
    """The bridge used to read the working directory out of a labelled line in Claude
    Code's system prompt. That label is gone in 2.1.278, so the parse returned None and
    every lookup silently found nothing. A directory either has a skills folder or it
    does not, so this asks the filesystem.
    """

    def test_it_finds_the_directory_that_actually_has_skills(self):
        root = project("analyse-data")
        found = skills.locate(f"scratch at {root}/notes", {"messages": []})
        self.assertEqual(found, root)

    def test_it_prefers_the_more_specific_path(self):
        root = project("analyse-data")
        deep = os.path.join(root, "sub")
        os.makedirs(os.path.join(deep, ".claude", "skills", "analyse-data"))
        with open(os.path.join(deep, ".claude", "skills", "analyse-data", "SKILL.md"), "w") as h:
            h.write("---\nname: analyse-data\n---\nbody\n")
        found = skills.locate(f"paths {root}/x and {deep}/x", {"messages": []})
        self.assertEqual(found, deep)

    def test_a_path_with_no_skills_folder_is_not_chosen(self):
        self.assertIsNone(skills.locate("/tmp/definitely/not/a/project/x", {"messages": []}))

    def test_it_reads_paths_out_of_the_conversation_too(self):
        root = project("analyse-data")
        body = {"messages": [{"role": "user",
                              "content": [{"type": "text", "text": f"open {root}/data.csv"}]}]}
        self.assertEqual(skills.locate("no paths here", body), root)


class TestRenderingTheProcedure(unittest.TestCase):
    def test_the_body_is_handed_over_not_the_name(self):
        root = project("analyse-data")
        text = skills.render(root, ["analyse-data"])
        self.assertIn("How to do analyse-data", text)
        self.assertIn('<skill name="analyse-data">', text)

    def test_a_skill_the_project_does_not_have_is_silent(self):
        """Describing a procedure that is not there is the same mistake as describing a
        tool that is not on the wire."""
        self.assertEqual(skills.render(project("analyse-data"), ["nonexistent"]), "")
        self.assertEqual(skills.render(None, ["analyse-data"]), "")
        self.assertEqual(skills.render(project("analyse-data"), []), "")

    def test_at_most_two_are_injected(self):
        root = project("a", "b", "c", "d")
        text = skills.render(root, ["a", "b", "c", "d"])
        self.assertEqual(text.count("<skill name="), 2)

    def test_the_whole_thing_is_wired_into_the_prompt(self):
        root = project("analyse-data")
        out = with_guidance(f"working in {root}/here", turn("Analyse macro.csv", root))
        self.assertIn("<applicable_procedures>", out)
        self.assertIn("How to do analyse-data", out)

    def test_nothing_is_added_to_an_ordinary_coding_turn(self):
        root = project("analyse-data")
        out = with_guidance(f"working in {root}/here", turn("Refactor server.py", root))
        self.assertNotIn("<applicable_procedures>", out)


if __name__ == "__main__":
    unittest.main()


class TestTheSkillsAreNotReReadEveryTurn(unittest.TestCase):
    """Disk work on the request path. The commit that moved search into worker threads
    established that such work must not block the event loop; a read that repeats every
    turn to return the same bytes is worth doing once.
    """

    def test_a_second_lookup_does_not_touch_the_files(self):
        root = project("analyse-data")
        first = skills.available(root)
        folder = os.path.join(root, ".claude", "skills", "analyse-data", "SKILL.md")
        os.remove(folder)                      # the files are gone...
        self.assertEqual(skills.available(root), first)   # ...and the answer stands

    def test_adding_a_skill_is_picked_up(self):
        root = project("analyse-data")
        skills.available(root)
        new = os.path.join(root, ".claude", "skills", "multi-step-work")
        os.makedirs(new)
        with open(os.path.join(new, "SKILL.md"), "w") as handle:
            handle.write("---\nname: multi-step-work\n---\nbody\n")
        os.utime(os.path.join(root, ".claude", "skills"), None)
        self.assertIn("multi-step-work", skills.available(root))

    def test_the_cache_is_bounded(self):
        for _ in range(skills._CACHE_MAX + 6):
            skills.available(project("analyse-data"))
        self.assertLessEqual(len(skills._CACHE), skills._CACHE_MAX)


class TestTheGateDoesNotOverReach(unittest.TestCase):
    """Injecting a procedure that does not apply costs more than injecting none.

    Measured: a two-panel chart task listed two numbered panels and two bulleted JSON
    keys. The gate counted four "steps", pulled in the long-task procedure on top of the
    analysis one, and the run that followed announced "We will write the script ... then
    run it" and stopped - one turn, no tool calls, nothing on disk, against 6/6 for the
    same task without it.
    """

    def test_bulleted_output_fields_are_not_steps(self):
        body = turn("Make a chart.\n1. a histogram\n2. a Lorenz curve\n"
                    'Then write answer.json with:\n- "png_path"\n- "gini"')
        self.assertNotIn("multi-step-work", skills_for(body))

    def test_a_genuinely_long_task_still_gets_it(self):
        body = turn("data.csv\n1. load\n2. clean\n3. fit\n4. chart\n5. write up\n6. check")
        self.assertIn("multi-step-work", skills_for(body))

    def test_the_inline_notes_stand_down_when_the_skill_covers_them(self):
        """Two voices giving the same instruction is the failure the README records
        between two skills; this model handles a long list of rules badly."""
        root = project("analyse-data")
        body = {"tools": [{"name": "Bash"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": f"analyse {root}/d.csv"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash",
             "input": {"command": "python3 -c 'import pandas'"}}]}]}
        out = with_guidance(f"in {root}/x", body)
        self.assertIn("How to do analyse-data", out)
        self.assertNotIn("Reporting an analysis", out)

    def test_the_inline_notes_still_apply_when_no_skill_is_there(self):
        body = {"tools": [{"name": "Bash"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": "analyse /nowhere/d.csv"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash",
             "input": {"command": "python3 -c 'import pandas'"}}]}]}
        self.assertIn("Reporting an analysis", with_guidance("in /nowhere", body))
