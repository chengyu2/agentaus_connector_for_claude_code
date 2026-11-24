"""This repo ships no active Claude Code settings; only an example.

A once-tracked `.claude/settings.json` set `ANTHROPIC_BASE_URL` to
`http://127.0.0.1:8787` and an `attribution.commit` trailer, so anyone opening the
clone had every Claude Code session rerouted to a loopback bridge and their commits
co-attributed to Agentaus whether they wanted it or not. That file and its tracked
backup were removed; users copy `.claude/settings.example.json` into their own
user- or project-level settings instead.

The checks below read only files next to the tests, never a git working tree, so they
hold equally for a checkout, an exported copy, or a clone where personal settings have
since been added and ignored.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, ".claude", "settings.example.json")
GITIGNORE = os.path.join(ROOT, ".gitignore")


class TheExampleIsTheOnlyShippedSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(EXAMPLE) as handle:
            cls.example = json.load(handle)

    def test_the_example_carries_the_bridge_env_block(self):
        env = self.example["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8787")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION"], "agentaus")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"],
                         "Agentaus (Trellis Data)")

    def test_the_example_sets_no_attribution(self):
        # An `attribution.commit` block changes what every commit claims, for every
        # model, so it is a personal choice, never something a shared repo ships.
        self.assertNotIn("attribution", self.example)


class PersonalSettingsCannotBeRecommitted(unittest.TestCase):
    @staticmethod
    def patterns():
        with open(GITIGNORE) as handle:
            return [line.strip() for line in handle
                    if line.strip() and not line.lstrip().startswith("#")]

    def test_the_ignore_rules_cover_active_and_backup_settings(self):
        patterns = self.patterns()
        for pattern in (".claude/settings.json", ".claude/settings.local.json",
                        ".claude/settings_*.json"):
            self.assertIn(pattern, patterns)

    def test_the_example_is_not_ignored(self):
        self.assertFalse(any("settings.example.json" in p for p in self.patterns()))


if __name__ == "__main__":
    unittest.main()
