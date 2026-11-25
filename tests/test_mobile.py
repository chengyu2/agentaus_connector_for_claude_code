"""Test for the OpenAI-style /v1/chat/completions endpoint (the iOS path).

Stands up a stub Agentaus upstream that speaks OpenAI chat-completions JSON and SSE,
points the bridge at it, and checks both the buffered and streamed mobile paths.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

PORT = 9931


class _FakeAgentaus(BaseHTTPRequestHandler):
    """Stands in for https://agentaus.com.au /api/v1/chat/completions."""

    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        stream = body.get("stream", False)
        reply = {
            "id": "test-completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from Agentaus (test)."},
                "finish_reason": "stop",
            }],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        if stream:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            chunk = {"choices": [{"index": 0, "delta": {"content": "Hello from "}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            chunk = {"choices": [{"index": 0, "delta": {"content": "Agentaus (test)."}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

    def log_message(self, *args):
        pass


class _TestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", PORT), _FakeAgentaus)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()


class TestMobileBuffered(_TestCase):
    def test_buffered_chat_completions(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        old_base, old_path, old_token, old_key = (settings.agentaus_base_url,
                                                  settings.agentaus_path, settings.bridge_token,
                                                  settings.agentaus_api_key)
        settings.agentaus_base_url = f"http://127.0.0.1:{PORT}"
        settings.agentaus_path = "/api/v1/chat/completions"
        settings.bridge_token = "s3cret"
        settings.agentaus_api_key = "test-key"
        try:
            with TestClient(server.app) as client:
                r = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer s3cret"},
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
        finally:
            settings.agentaus_base_url, settings.agentaus_path = old_base, old_path
            settings.bridge_token, settings.agentaus_api_key = old_token, old_key

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello from Agentaus (test).")

    def test_bad_token_is_401(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        old_token = settings.bridge_token
        settings.bridge_token = "s3cret"
        try:
            with TestClient(server.app) as client:
                r = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer wrong"},
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
        finally:
            settings.bridge_token = old_token
        self.assertEqual(r.status_code, 401)


class TestMobileStreamed(_TestCase):
    def test_streamed_chat_completions(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        old_base, old_path, old_token, old_key = (settings.agentaus_base_url,
                                                  settings.agentaus_path, settings.bridge_token,
                                                  settings.agentaus_api_key)
        settings.agentaus_base_url = f"http://127.0.0.1:{PORT}"
        settings.agentaus_path = "/api/v1/chat/completions"
        settings.bridge_token = "s3cret"
        settings.agentaus_api_key = "test-key"
        try:
            with TestClient(server.app) as client:
                r = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer s3cret"},
                    json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
                )
        finally:
            settings.agentaus_base_url, settings.agentaus_path = old_base, old_path
            settings.bridge_token, settings.agentaus_api_key = old_token, old_key

        self.assertEqual(r.status_code, 200)
        text = r.text
        self.assertIn("data: [DONE]", text)
        # Content arrives split across content deltas; reassemble to assert on the full text.
        deltas = []
        for line in text.splitlines():
            if line.startswith("data: ") and "delta" in line:
                try:
                    deltas.append(json.loads(line[6:])["choices"][0]["delta"].get("content"))
                except (ValueError, IndexError):
                    continue
        self.assertEqual("".join(d for d in deltas if d), "Hello from Agentaus (test).")


if __name__ == "__main__":
    unittest.main()
