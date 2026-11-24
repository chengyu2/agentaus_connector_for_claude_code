"""AGENTAUS_TEMPERATURE / AGENTAUS_TOP_P on main-loop requests."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge.translate import anthropic_request_to_agentaus  # noqa: E402

BODY = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.7}


class DecodingTests(unittest.TestCase):
    def build(self, temperature="", top_p="", body=BODY):
        with mock.patch.object(settings, "agentaus_temperature", temperature), \
                mock.patch.object(settings, "agentaus_top_p", top_p):
            return anthropic_request_to_agentaus(body)

    def test_unset_sends_nothing(self):
        payload = self.build()
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)

    def test_fixed_values(self):
        payload = self.build("0.2", "0.9")
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["top_p"], 0.9)

    def test_client_forwards_the_request_value(self):
        self.assertEqual(self.build("client")["temperature"], 0.7)
        # Claude Code with thinking on sends none: then nothing is forwarded.
        self.assertNotIn("temperature", self.build(
            "client", body={"messages": BODY["messages"]}))

    def test_garbage_is_ignored(self):
        self.assertNotIn("temperature", self.build("warm"))


if __name__ == "__main__":
    unittest.main()
