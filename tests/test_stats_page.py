"""/stats and /stats.json (Feature 2): live in-memory counters for the operator.

Counters live in agentaus_bridge/stats and are recorded at the points that already
log each event. The page answers only a loopback client - a remote host gets 403 even
when BRIDGE_TOKEN is set - and shows counters only, never prompts or keys.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.requests import Request  # noqa: E402

from agentaus_bridge import server as server_mod  # noqa: E402
from agentaus_bridge import stats as stats_mod  # noqa: E402


def _request(host: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/stats.json",
        "headers": [],
        "client": (host, 50000),
    })


class TestStatsCounters(unittest.TestCase):
    def setUp(self):
        stats_mod.reset()

    def tearDown(self):
        stats_mod.reset()

    def test_counters_increase_after_a_stubbed_request(self):
        stats_mod.record_request("agentaus", 1.5, input_tokens=100, output_tokens=20)
        snap = stats_mod.snapshot()
        self.assertEqual(snap["requests"]["agentaus"]["count"], 1)
        self.assertEqual(snap["requests"]["agentaus"]["average"], 1.5)
        self.assertEqual(snap["requests"]["agentaus"]["slowest"], 1.5)
        self.assertEqual(snap["tokens"]["input"], 100)
        self.assertEqual(snap["tokens"]["output"], 20)

    def test_compaction_and_error_counters_recorded(self):
        stats_mod.record_compact("compact", 3000)
        stats_mod.record_compact("trim", 500)
        stats_mod.record_error("401")
        stats_mod.record_error("429")
        stats_mod.record_error("5xx")
        stats_mod.record_error("timeout")
        snap = stats_mod.snapshot()
        self.assertEqual(snap["compaction"]["count"], 1)
        self.assertEqual(snap["compaction"]["tokens_saved"], 3000)
        self.assertEqual(snap["compaction"]["trims"], 1)
        self.assertEqual(snap["compaction"]["trim_tokens_saved"], 500)
        self.assertEqual(snap["errors"]["401"], 1)
        self.assertEqual(snap["errors"]["429"], 1)
        self.assertEqual(snap["errors"]["5xx"], 1)
        self.assertEqual(snap["errors"]["timeout"], 1)

    def test_stats_json_shape_from_loopback(self):
        import asyncio
        stats_mod.record_request("agentaus", 0.5, input_tokens=10, output_tokens=2)
        response = asyncio.run(server_mod.stats_json(_request("127.0.0.1")))
        self.assertEqual(response.status_code, 200)
        data = response.body  # JSONResponse already serialised
        import json
        payload = json.loads(data)
        self.assertIn("requests", payload)
        self.assertIn("tokens", payload)
        self.assertIn("compaction", payload)
        self.assertIn("errors", payload)
        self.assertIn("uptime", payload)
        self.assertIn("version", payload)
        self.assertIn("agentaus", payload["requests"])
        self.assertIn("anthropic", payload["requests"])
        self.assertEqual(payload["tokens"]["input"], 10)

    def test_remote_client_gets_403_even_with_token(self):
        import asyncio
        from agentaus_bridge.config import settings
        old = settings.bridge_token
        settings.bridge_token = "s3cret"
        try:
            for path in (server_mod.stats_json, server_mod.stats_page):
                response = asyncio.run(path(_request("192.168.15.20")))
                self.assertEqual(response.status_code, 403)
        finally:
            settings.bridge_token = old

    def test_stats_page_is_self_contained_html_from_loopback(self):
        import asyncio
        response = asyncio.run(server_mod.stats_page(_request("::1")))
        self.assertEqual(response.status_code, 200)
        text = response.body.decode("utf-8", "replace")
        self.assertIn("<html", text)
        self.assertIn("Auto-refresh", text)
        # No external assets, no secrets.
        self.assertNotIn("http://", text)
        self.assertNotIn("AGENTAUS_API_KEY", text)
        self.assertNotIn("Bearer", text)


if __name__ == "__main__":
    unittest.main()
