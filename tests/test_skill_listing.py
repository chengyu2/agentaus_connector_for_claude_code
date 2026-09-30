"""Claude Code lists project skills by bare name; Agentaus needs to see what each is for.

Since 2.1.281 the listing reads "- find-in-code" with no description, for every model.
Opus chose the right skill from the name alone on three of three probes; Agentaus called
none. With the descriptions restored in a direct test it chose correctly - so the bridge
puts them back, from the skills' own front matter, and shows the planner the same list.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import skills  # noqa: E402
from agentaus_bridge.augment import (  # noqa: E402
    plan_prompt, routes, routing_section, skill_listing, skills_for, with_skill_descriptions,
)

HEAD = "The following skills are available for use with the Skill tool:"


def project(**described: str) -> str:
    root = tempfile.mkdtemp()
    for name, about in described.items():
        folder = os.path.join(root, ".claude", "skills", name)
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "w") as handle:
            handle.write(f"---\nname: {name}\ndescription: {about}\n---\n\n# {name}\n\nStep.\n")
    return root


def listing(*entries: str) -> str:
    return HEAD + "\n\n" + "\n".join(f"- {e}" for e in entries) + "\n\nAvailable agent types:\n- Explore: x"


def request(root: str, note: str) -> dict:
    """The 2.1.281 shape: the environment and the listing in a system-role message."""
    return {
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "tools": [{"name": "Skill", "description": "Run a skill"}, {"name": "Read"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Where is the retry?"}]},
            {"role": "system", "content": f"Primary working directory: {root}\n\n{note}"},
        ],
    }


class DescriptionsAreRestored(unittest.TestCase):
    def test_a_bare_project_entry_gets_its_description(self):
        root = project(**{"find-in-code": "Locate something. Use when asked where."})
        body, filled = with_skill_descriptions(request(root, listing("find-in-code")))
        self.assertEqual(filled, 1)
        self.assertIn("- find-in-code: Locate something. Use when asked where.",
                      body["messages"][1]["content"])

    def test_a_description_the_client_sent_is_never_replaced(self):
        root = project(dataviz="from disk")
        body, filled = with_skill_descriptions(request(root, listing("dataviz: from client")))
        self.assertEqual(filled, 0)
        self.assertIn("- dataviz: from client", body["messages"][1]["content"])

    def test_a_name_with_no_file_behind_it_is_left_alone(self):
        root = project(**{"find-in-code": "Locate"})
        body, _ = with_skill_descriptions(request(root, listing("find-in-code", "loop")))
        self.assertIn("\n- loop\n", body["messages"][1]["content"])

    def test_the_rest_of_the_prompt_is_untouched(self):
        root = project(**{"find-in-code": "Locate"})
        original = request(root, listing("find-in-code"))
        body, _ = with_skill_descriptions(original)
        self.assertEqual(body["system"], original["system"])
        self.assertEqual(body["messages"][0], original["messages"][0])
        self.assertIn("- Explore: x", body["messages"][1]["content"])
        self.assertIn("- find-in-code\n", original["messages"][1]["content"], "input mutated")

    def test_a_listing_in_system_is_found_too(self):
        root = project(**{"find-in-code": "Locate"})
        body = {"system": f"cwd {root}/src/x.py\n\n" + listing("find-in-code"),
                "messages": [{"role": "user", "content": "hi"}]}
        out, filled = with_skill_descriptions(body)
        self.assertEqual(filled, 1)
        self.assertIn("- find-in-code: Locate", out["system"])

    def test_no_skills_directory_changes_nothing(self):
        body = request(tempfile.mkdtemp(), listing("find-in-code"))
        self.assertIs(with_skill_descriptions(body)[0], body)

    def test_folded_descriptions_are_read(self):
        root = tempfile.mkdtemp()
        folder = os.path.join(root, ".claude", "skills", "tidy")
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "w") as handle:
            handle.write("---\nname: tidy\ndescription: >\n  Tidy a module.\n  Use when asked.\n"
                         "license: Apache-2.0\n---\n\nBody.\n")
        self.assertEqual(skills.described(root)["tidy"], "Tidy a module. Use when asked.")


class ThePlannerSeesTheSkills(unittest.TestCase):
    def test_the_routes_reach_the_plan_prompt_and_it_must_name_a_choice(self):
        root = project(**{"find-in-code": "Locate something. Use when asked where."})
        body, _ = with_skill_descriptions(request(root, listing("find-in-code")))
        self.assertEqual(skill_listing(body)[0][0], "find-in-code")
        prompt = plan_prompt("Where is the retry?", body)
        self.assertIn('If asked where \u2192 `Skill` with `skill: "find-in-code"`', prompt)
        self.assertIn("`Skill: <name>`", prompt)
        self.assertIn("`Skill: none`", prompt)

    def test_no_skill_section_without_the_skill_tool(self):
        root = project(**{"find-in-code": "Locate"})
        body, _ = with_skill_descriptions(request(root, listing("find-in-code")))
        body["tools"] = [{"name": "Read"}]
        self.assertNotIn("`Skill: <name>`", plan_prompt("x", body))


class RoutesReadIfThisThenThat(unittest.TestCase):
    """The user's suggestion, measured against a bare list: one "if this -> use that" line
    per skill and agent type, taken from each one's own description."""

    def _body(self, *entries, agents=""):
        root = project(**{"find-in-code": "Locate code. Use when asked \"where is\" X.",
                          "tidy": "Use this skill to tidy a module."})
        note = listing(*entries).replace("\n\nAvailable agent types:\n- Explore: x", "")
        if agents:
            note += "\n\nAvailable agent types for the Agent tool:\n" + agents
        body = request(root, note)
        body["tools"] = [{"name": "Skill"}, {"name": "Agent"}, {"name": "Read"}]
        return with_skill_descriptions(body)[0]

    def test_the_when_clause_becomes_the_if(self):
        lines = routes(self._body("find-in-code"))
        self.assertIn('- If asked "where is" X \u2192 `Skill` with `skill: "find-in-code"`', lines)

    def test_a_purpose_without_when_still_reads_as_a_condition(self):
        lines = routes(self._body("tidy"))
        self.assertIn('- If you need to tidy a module \u2192 `Skill` with `skill: "tidy"`', lines)

    def test_agent_types_route_to_agent_and_drop_the_tool_list(self):
        agents = ("- Explore: Read-only search agent \u2014 when answering means sweeping "
                  "many files. (Tools: Read, Grep)")
        lines = routes(self._body("find-in-code", agents=agents))
        self.assertIn('- If answering means sweeping many files \u2192 `Agent` with '
                      '`subagent_type: "Explore"`', lines)
        self.assertFalse(any("Tools:" in line for line in lines))

    def test_the_section_carries_the_budget_and_a_way_out(self):
        section = routing_section(self._body("find-in-code"))
        self.assertIn("no API or token limit", section)
        self.assertIn("If none of these fits", section)
        self.assertIn("returns instructions, not results", section)

    def test_nothing_without_the_tools(self):
        body = self._body("find-in-code")
        body["tools"] = [{"name": "Read"}]
        self.assertEqual(routing_section(body), "")


class TheDataGateReadsTheConversationOnly(unittest.TestCase):
    def test_a_loaded_skill_mentioning_xlsx_does_not_fire_it(self):
        body = {"tools": [{"name": "Read"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": "What does the docx say?"}]},
            {"role": "user", "content": [{"type": "text", "text":
                "Base directory for this skill: /p/.claude/skills/read-documents\n\n"
                "Read .docx and .xlsx files correctly."}]}]}
        self.assertEqual(skills_for(body), [])

    def test_a_reminder_mentioning_csv_does_not_fire_it(self):
        body = {"tools": [{"name": "Read"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text":
                "<system-reminder>notes: exports go to out.csv</system-reminder>\n"
                "Fix the retry helper."}]}]}
        self.assertEqual(skills_for(body), [])

    def test_the_users_own_csv_still_fires_it(self):
        body = {"tools": [{"name": "Read"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Analyse sales.csv"}]}]}
        self.assertEqual(skills_for(body), ["analyse-data"])

    def test_a_description_mentioning_csv_does_not_fire_the_data_gate(self):
        root = project(**{"analyse-data": "Analyse a CSV or .xlsx. Use when asked."})
        body, _ = with_skill_descriptions(request(root, listing("analyse-data")))
        self.assertEqual(skills_for(body), [])


if __name__ == "__main__":
    unittest.main()
