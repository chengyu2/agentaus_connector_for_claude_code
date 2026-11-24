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
TOOL = {"name": "Bash", "description": LONG, "input_schema": {
    "type": "object", "required": ["command"], "properties": {
        "command": {"type": "string", "description": "The command. " + "y" * 400}}}}


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
        text = "Use it well.\n\n## When to use\n" + "z" * 2000
        self.assertEqual(diet._cap(text, 900), "Use it well.")

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
