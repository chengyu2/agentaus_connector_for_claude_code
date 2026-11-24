"""AGENTAUS_PROMPT_DIET: shorter tool prose, irrelevant sections dropped, lean notes."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import diet  # noqa: E402
from agentaus_bridge.augment import guidance_for  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402

SYSTEM = ("Intro.\n\n# Harness\n - keep me\n\n# Memory\n\nWrite memories to x.\n\n"
          "# Environment\n - Primary working directory: /w\n"
          " - The most recent Claude models are the Claude 5 family.\n"
          "# Context management\nSummaries happen.\n")
LONG = "First paragraph.\n\n" + "\n".join(f"- rule {i} " + "x" * 80 for i in range(30))
TOOL = {"name": "WebFetch", "description": LONG, "input_schema": {
    "type": "object", "required": ["command"], "properties": {
        "command": {"type": "string", "description": "The command. More " + "y" * 400}}}}
# Shaped like Claude Code's Bash manual: a `# comment` in a code fence early on, then the
# instructions and the git safety rules well past the cap.
BASH = ("Executes a bash command.\n\n```\n# list files\nls\n```\n\n"
        + "Some guidance. " * 40 + "\n\n# Instructions\n - Quote paths with spaces.\n"
        + "\n# Committing changes with git\n\nOnly create commits when requested by the "
          "user. NEVER run force push. NEVER update the git config.\n" + "More. " * 200)


class SlimTests(unittest.TestCase):
    def test_drops_claude_only_sections(self):
        out = diet.slim({"system": [{"type": "text", "text": SYSTEM}]})["system"][0]["text"]
        self.assertIn("keep me", out)
        self.assertIn("Primary working directory: /w", out)
        self.assertNotIn("Write memories", out)
        self.assertNotIn("Claude 5 family", out)
        self.assertNotIn("Summaries happen", out)

    def test_caps_descriptions_but_keeps_schema(self):
        tool = diet.slim({"tools": [TOOL]})["tools"][0]
        self.assertLessEqual(len(tool["description"]), diet.DESCRIPTION_CAP)
        self.assertTrue(tool["description"].startswith("First paragraph."))
        prop = tool["input_schema"]["properties"]["command"]
        self.assertEqual(prop["description"], "The command.")
        self.assertEqual(tool["input_schema"]["required"], ["command"])

    def test_cuts_at_a_heading(self):
        text = "Use it well. " * 50 + "\n\n## When to use\n" + "z" * 2000
        self.assertEqual(diet._cap(text, 900), ("Use it well. " * 50).rstrip())

    def test_does_not_cut_at_a_heading_far_short_of_the_cap(self):
        text = "Use it well.\n\n## When to use\n" + "\n".join(["z" * 60] * 40)
        out = diet._cap(text, 900)
        self.assertGreater(len(out), 450)
        self.assertLessEqual(len(out), 900)

    def test_comment_in_a_code_fence_is_not_a_heading(self):
        text = ("Intro. " * 70 + "\n```\n# a shell comment\nls\n```\n"
                + "\n".join(["more text here"] * 80))
        out = diet._cap(text, 900)
        self.assertIn("# a shell comment", out)

    def test_core_tool_manuals_are_never_capped(self):
        for name in sorted(diet.UNCAPPED_TOOLS):
            tool = diet.slim({"tools": [{"name": name, "description": BASH}]})["tools"][0]
            self.assertEqual(tool["description"], BASH, name)
            self.assertIn("NEVER run force push", tool["description"])
        other = diet.slim({"tools": [{"name": "WebFetch", "description": BASH}]})["tools"][0]
        self.assertLessEqual(len(other["description"]), diet.DESCRIPTION_CAP)

    def test_memory_drop_keeps_a_headingless_env_block(self):
        system = ("Intro.\n\n# Memory\n\nWrite memories to x.\n\n"
                  "Here is useful information about the environment you are running in:\n"
                  "<env>\nWorking directory: /w\n</env>\n")
        out = diet.slim({"system": system})["system"]
        self.assertNotIn("Write memories", out)
        self.assertIn("Here is useful information", out)
        self.assertIn("Working directory: /w", out)
        bare = diet.slim({"system": "# Memory\nx\n<env>\nWorking directory: /w\n</env>"})
        self.assertIn("Working directory: /w", bare["system"])

    def test_memory_drop_stops_at_a_level_two_heading(self):
        out = diet.slim({"system": "# Memory\nx\n## Git status\nclean\n"})["system"]
        self.assertIn("clean", out)

    def test_first_sentence_does_not_cut_at_abbreviations(self):
        text = "Pass a path, e.g. src/a.py, to read. Then more. " + "y" * 300
        self.assertEqual(diet._first_sentence(text, 200), "Pass a path, e.g. src/a.py, to read.")
        self.assertTrue(diet._first_sentence("v1.2 is " + "y" * 300, 200).endswith("..."))

    def test_schema_data_values_are_not_rewritten(self):
        long = "A value. " + "Z" * 300
        schema = {"type": "object", "default": {"description": long},
                  "examples": [{"description": long}], "enum": [long], "const": long,
                  "description": long}
        out = diet._slim_schema(schema)
        self.assertEqual(out["default"], {"description": long})
        self.assertEqual(out["examples"], [{"description": long}])
        self.assertEqual(out["enum"], [long])
        self.assertEqual(out["const"], long)
        self.assertEqual(out["description"], "A value.")

    def test_short_input_untouched(self):
        body = {"system": "Hi.", "tools": [{"name": "Edit", "description": "Edits."}]}
        self.assertEqual(diet.slim(body), body)


class LeanGuidanceTests(unittest.TestCase):
    BODY = {"messages": [{"role": "user", "content": "fix it"}],
            "tools": [{"name": "Bash", "description": "run", "input_schema": {}}]}

    def test_lean_notes_replace_the_long_ones(self):
        with mock.patch.object(settings, "agentaus_prompt_diet", True):
            notes = guidance_for(self.BODY, routing=False)
        self.assertIn("Act, do not describe", notes)
        self.assertNotIn("Which tool, and when", notes)
        self.assertNotIn("Restate the requirement", notes)

    def test_off_keeps_the_long_notes(self):
        with mock.patch.object(settings, "agentaus_prompt_diet", False):
            notes = guidance_for(self.BODY, routing=False)
        self.assertNotIn("Act, do not describe", notes)
        self.assertIn("Which tool, and when", notes)


if __name__ == "__main__":
    unittest.main()
