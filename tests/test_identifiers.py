"""Identifiers must survive summarisation byte-for-byte.

Models reformat punctuation when writing prose. Observed from Agentaus while building
the compaction layer:

    EU-WEST-2        ->  EU‑WEST‑2     (U+2011 NON-BREAKING HYPHEN)
    retry_budget_ms  ->  $$retry_budget_ms$$   (wrapped as if it were mathematics)

In prose that is cosmetic. In a summary a coding agent reads back and acts on, it is
corruption - a region code or file path with a typographic hyphen looks right and is
not, which is worse than it being absent. Normalising deterministically beats asking
the model not to do it.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge.compact import normalise_identifiers  # noqa: E402


class TestNormalisation(unittest.TestCase):
    def test_the_exact_failure_observed_from_agentaus(self):
        mangled = "Data residency rule: deploy to EU‑WEST‑2"

        self.assertIn("EU-WEST-2", normalise_identifiers(mangled))

    def test_every_unicode_dash_becomes_ascii(self):
        for dash in "‐‑‒–—−":
            self.assertIn("EU-WEST-2", normalise_identifiers(f"EU{dash}WEST{dash}2"),
                          f"U+{ord(dash):04X} not normalised")

    def test_math_wrapped_identifiers_are_unwrapped(self):
        self.assertEqual(normalise_identifiers("$$retry_budget_ms$$"), "`retry_budget_ms`")

    def test_smart_quotes_become_ascii(self):
        out = normalise_identifiers("the “config” file and it’s path")

        self.assertIn('"config"', out)
        self.assertIn("it's", out)

    def test_non_breaking_space_becomes_a_space(self):
        self.assertEqual(normalise_identifiers("40 req/min"), "40 req/min")

    def test_plain_text_is_untouched(self):
        text = "The bridge listens on port 9473; config at /etc/agentaus/bridge.toml"

        self.assertEqual(normalise_identifiers(text), text)

    def test_code_punctuation_is_preserved(self):
        """Normalisation must not damage legitimate code."""
        code = "x = a - b; y = {'k': [1,2]}; path = '/a/b-c_d.py'; flag = '--no-cache'"

        self.assertEqual(normalise_identifiers(code), code)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(normalise_identifiers(""), "")

    def test_a_realistic_mangled_summary_recovers_every_identifier(self):
        mangled = (
            "**Configuration**\n- Bridge port: 9473\n"
            "- Config file: /etc/agentaus/bridge.toml\n"
            "- Tuning knob: $$retry_budget_ms$$\n"
            "- Up‑stream rate‑limit: 40 req/min\n"
            "- Data residency rule: deploy to EU‑WEST‑2\n"
            "- Chose exponential back‑off with a 12‑second ceiling\n"
        )
        out = normalise_identifiers(mangled)

        for needle in ("9473", "/etc/agentaus/bridge.toml", "retry_budget_ms",
                       "40 req/min", "EU-WEST-2", "12-second"):
            self.assertIn(needle, out, f"{needle} did not survive normalisation")


if __name__ == "__main__":
    unittest.main()
