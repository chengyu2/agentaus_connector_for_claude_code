"""Regression tests for the Anthropic passthrough path.

The bug these guard against: reading the upstream body with httpx's aiter_raw()
returns it still gzip-compressed, while the bridge strips the `content-encoding`
header. The client then receives compressed bytes labelled as plain JSON and fails
with "Failed to parse JSON". Small bodies are often not compressed, so the fault
appears intermittent - which is exactly what makes it worth a test.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

PORT = 9921
PAYLOAD = {
    "id": "msg_stub",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "y" * 3000}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 9},
}


class _GzipUpstream(BaseHTTPRequestHandler):
    """Stands in for api.anthropic.com behind a CDN that compresses responses."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        body = gzip.compress(json.dumps(PAYLOAD).encode())
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-encoding", "gzip")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class TestGzippedPassthrough(unittest.TestCase):
    server: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", PORT), _GzipUpstream)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_gzipped_upstream_response_reaches_the_client_as_json(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        original = settings.anthropic_base_url
        settings.anthropic_base_url = f"http://127.0.0.1:{PORT}"
        try:
            with TestClient(server.app) as client:
                response = client.post(
                    "/v1/messages",
                    json={
                        "model": "claude-sonnet-5",
                        "max_tokens": 16,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
        finally:
            settings.anthropic_base_url = original

        self.assertEqual(response.status_code, 200)
        # The failure mode is binary gzip bytes arriving here, so assert on both the
        # magic number and the parse.
        self.assertNotEqual(response.content[:2], b"\x1f\x8b", "body is still gzipped")
        body = json.loads(response.content)
        self.assertEqual(body["id"], "msg_stub")
        self.assertEqual(len(body["content"][0]["text"]), 3000)

    def test_content_encoding_header_is_not_forwarded(self):
        """If the header survived while the body was decoded, clients would double-decode."""
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        original = settings.anthropic_base_url
        settings.anthropic_base_url = f"http://127.0.0.1:{PORT}"
        try:
            with TestClient(server.app) as client:
                response = client.post(
                    "/v1/messages",
                    json={"model": "claude-sonnet-5", "messages": [], "max_tokens": 8},
                )
        finally:
            settings.anthropic_base_url = original

        self.assertNotIn("content-encoding", {k.lower() for k in response.headers})


if __name__ == "__main__":
    unittest.main()
