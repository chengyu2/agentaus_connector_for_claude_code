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

    def test_non_finite_or_out_of_range_is_ignored(self):
        import json
        for t, p in (("nan", "nan"), ("inf", "-inf"), ("-0.1", "0"), ("2.5", "1.5")):
            payload = self.build(t, p)
            self.assertNotIn("temperature", payload, t)
            self.assertNotIn("top_p", payload, p)
            json.dumps(payload, allow_nan=False)  # must still serialise
        payload = self.build("0", "1")
        self.assertEqual((payload["temperature"], payload["top_p"]), (0.0, 1.0))
        self.assertEqual(self.build("2")["temperature"], 2.0)

    def test_bad_value_warns_once(self):
        with self.assertLogs("agentaus-bridge", "WARNING") as logs:
            self.build("nan-once-test")
            self.build("nan-once-test")
            self.build(temperature="", top_p="7")
        self.assertEqual(len(logs.records), 2)


if __name__ == "__main__":
    unittest.main()
