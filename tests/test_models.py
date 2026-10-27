"""GET /v1/models: Anthropic's own list, live, plus Agentaus - what Claude Code's gateway model
discovery (CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1) builds the model picker from."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

PORT = 9923
STATE = {"status": 200, "delay": 0.0, "seen": []}
LIVE = [
    {"id": "claude-opus-9-9", "type": "model", "display_name": "Claude Opus 9.9", "created_at": "2027-01-01T00:00:00Z"},
    {"id": "claude-haiku-9-1", "type": "model", "display_name": "Claude Haiku 9.1", "created_at": "2027-01-01T00:00:00Z"},
]


class _Anthropic(BaseHTTPRequestHandler):
    """Stands in for api.anthropic.com/v1/models."""

    def do_GET(self) -> None:
        STATE["seen"].append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        time.sleep(STATE["delay"])
        body = json.dumps({"data": LIVE, "has_more": False} if STATE["status"] == 200
                          else {"type": "error", "error": {"type": "authentication_error"}}).encode()
        self.send_response(STATE["status"])
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class ModelList(unittest.TestCase):
    server: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", PORT), _Anthropic)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        from agentaus_bridge import server
        from agentaus_bridge.config import settings
        STATE.update({"status": 200, "delay": 0.0, "seen": []})
        server._MODELS_LAST["data"] = None
        self._saved = settings.anthropic_base_url
        settings.anthropic_base_url = f"http://127.0.0.1:{PORT}"

    def tearDown(self) -> None:
        from agentaus_bridge.config import settings
        settings.anthropic_base_url = self._saved

    def get(self, headers=None):
        from agentaus_bridge import server
        with TestClient(server.app) as client:
            return client.get("/v1/models?limit=1000", headers=headers or {})

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return [m["id"] for m in response.json()["data"]]

    def test_anthropics_live_list_is_served_with_agentaus_after_it(self):
        ids = self.ids(self.get({"x-api-key": "sk-test", "anthropic-beta": "oauth-2025-04-20"}))
        self.assertEqual(ids, ["claude-opus-9-9", "claude-haiku-9-1", "agentaus"])
        seen = STATE["seen"][-1]
        self.assertEqual(seen["path"], "/v1/models?limit=1000")
        # The caller's credentials and betas go through; the bridge adds a version if missing.
        self.assertEqual(seen["headers"]["x-api-key"], "sk-test")
        self.assertEqual(seen["headers"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")

    def test_a_bearer_token_is_forwarded_too(self):
        self.ids(self.get({"authorization": "Bearer oauth-token"}))
        self.assertEqual(STATE["seen"][-1]["headers"]["authorization"], "Bearer oauth-token")

    def test_without_credentials_anthropic_is_not_asked(self):
        self.assertEqual(self.ids(self.get()), ["agentaus"])
        self.assertEqual(STATE["seen"], [])

    def test_a_refusal_or_a_slow_answer_serves_the_last_good_list(self):
        self.ids(self.get({"x-api-key": "k"}))
        STATE["status"] = 401
        self.assertEqual(self.ids(self.get({"x-api-key": "k"})), ["claude-opus-9-9", "claude-haiku-9-1", "agentaus"])
        STATE.update({"status": 200, "delay": 3.0})
        started = time.monotonic()
        self.assertEqual(self.ids(self.get({"x-api-key": "k"})), ["claude-opus-9-9", "claude-haiku-9-1", "agentaus"])
        # Inside the picker's 3 s.
        self.assertLess(time.monotonic() - started, 2.95)

    def test_never_a_list_without_agentaus_and_never_a_redirect(self):
        STATE["status"] = 500
        response = self.get({"x-api-key": "k"})
        self.assertEqual(self.ids(response), ["agentaus"])
        self.assertEqual(response.json()["first_id"], "agentaus")


if __name__ == "__main__":
    unittest.main()
