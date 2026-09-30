"""The bridge's own skill library: listed for Agentaus in any project, answered by the bridge.

Claude Code has never heard of these skills, so the bridge adds them to the skill listing
and runs a `Skill` call naming one itself. A project skill of the same name stays the
project's. The real library is also checked against the Agent Skills spec, because a
description without a "Use when" clause produces a routing line with nothing to route on.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import server, skills, tools  # noqa: E402
from agentaus_bridge.augment import routes, with_library_skills  # noqa: E402

HEAD = "The following skills are available for use with the Skill tool:"
REAL_LIBRARY = skills.LIBRARY


def fake_library(**described):
    root = tempfile.mkdtemp()
    for name, about in described.items():
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "SKILL.md"), "w") as handle:
            handle.write(f"---\nname: {name}\ndescription: {about}\n---\n\n# {name}\n\n1. Step.\n")
    return root


def request(listing: str, tools_offered=("Skill", "Read")):
    return {"tools": [{"name": t} for t in tools_offered],
            "messages": [{"role": "user", "content": "review my change"},
                         {"role": "system", "content": listing}]}


class TheLibraryIsListedAndAnswered(unittest.TestCase):
    def setUp(self):
        self._saved = skills.LIBRARY
        skills.LIBRARY = fake_library(**{
            "thorough-code-review": "Review code with parallel lenses. Use when asked to review a change.",
            "math-reasoning": "Compute and verify. Use when a question needs arithmetic."})

    def tearDown(self):
        skills.LIBRARY = self._saved

    def test_library_skills_join_the_listing_and_the_routes(self):
        body, served = with_library_skills(request(HEAD + "\n\n- dataviz: charts\n"))
        self.assertEqual(served, {"thorough-code-review", "math-reasoning"})
        lines = routes(body)
        self.assertTrue(any('skill: "thorough-code-review"' in line for line in lines))
        # Before the client's bundled skills, which are about Claude Code itself.
        text = body["messages"][1]["content"]
        self.assertLess(text.index("thorough-code-review"), text.index("dataviz"))

    def test_a_skill_the_client_already_lists_stays_the_clients(self):
        body, served = with_library_skills(
            request(HEAD + "\n\n- math-reasoning: the project's own\n"))
        self.assertEqual(served, {"thorough-code-review"})

    def test_nothing_is_added_without_the_skill_tool(self):
        body = request(HEAD + "\n\n- dataviz: charts\n", tools_offered=("Read",))
        self.assertEqual(with_library_skills(body)[1], frozenset())

    def test_a_call_for_a_served_skill_is_the_bridges(self):
        calls = [
            {"id": "1", "name": "Skill", "arguments": json.dumps({"skill": "math-reasoning"})},
            {"id": "2", "name": "Skill", "arguments": json.dumps({"skill": "read-documents"})},
        ]
        mine, theirs, _ = server._partition_tool_calls(
            calls, library=frozenset({"math-reasoning"}))
        self.assertEqual([c["id"] for c in mine], ["1"])
        self.assertEqual([c["id"] for c in theirs], ["2"])
        # Nothing offered, nothing taken: every Skill call stays Claude Code's.
        self.assertEqual(server._partition_tool_calls(calls)[0], [])

    def test_the_bridge_serves_the_procedure_framed_to_be_followed(self):
        async def no_model(_):
            return ""
        out = asyncio.new_event_loop().run_until_complete(
            tools.execute("Skill", {"skill": "math-reasoning"}, no_model))
        self.assertIn("Procedure to follow now: math-reasoning", out)
        self.assertIn("1. Step.", out)
        self.assertIn("agentaus_zoom", out)

    def test_an_unknown_name_says_what_exists(self):
        self.assertIn("math-reasoning", skills.serve("nope"))


class TheRealLibraryMeetsTheSpec(unittest.TestCase):
    """agentskills.io: name 1-64 of [a-z0-9-] matching its directory, description at most
    1024 characters - and here, a "Use when" clause for the routing line to be built from."""

    def test_every_skill(self):
        if not os.path.isdir(REAL_LIBRARY):
            self.skipTest("no library yet")
        names = sorted(d for d in os.listdir(REAL_LIBRARY)
                       if os.path.isfile(os.path.join(REAL_LIBRARY, d, "SKILL.md")))
        self.assertTrue(names)
        for name in names:
            with self.subTest(skill=name):
                with open(os.path.join(REAL_LIBRARY, name, "SKILL.md")) as handle:
                    text = handle.read()
                front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
                self.assertIsNotNone(front, "no front matter")
                self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertLessEqual(len(name), 64)
                self.assertRegex(front.group(1), rf"(?m)^name:\s*{re.escape(name)}\s*$")
                about = skills._description(front.group(1))
                self.assertTrue(0 < len(about) <= 1024, f"description is {len(about)} chars")
                self.assertIn("Use when", about)
                self.assertRegex(front.group(1), r"(?m)^license:\s*Apache-2\.0")
                self.assertLessEqual(text.count("\n"), 520, "body over 500 lines")


if __name__ == "__main__":
    unittest.main()
