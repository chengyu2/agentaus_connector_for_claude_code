"""A large Claude request must not fail just because its upload takes a while.

The bug: the passthrough handed httpx the whole request body as one `bytes`, which
httpcore writes as a single chunk inside one write timeout (BRIDGE_CONNECT_TIMEOUT,
30 s). A Claude Code turn carrying screenshots is routinely 3-20 MB, so on an ordinary
uplink the upload alone exceeded the timeout, failed with WriteTimeout, and was retried
from the first byte - ten times in a row on one real turn. Sending the body in slices
makes the timeout apply per slice: a dead connection still times out, a slow one does
not.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import stub_server  # noqa: E402

REPLY = {
    "id": "msg_stub",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 1},
}

BODY_BYTES = 3 * 1024 * 1024
READ_SLICE = 128 * 1024
READ_PAUSE = 0.06  # ~2 MB/s: the whole body takes ~1.5 s to arrive
WRITE_TIMEOUT = 0.6  # longer than any one slice takes, shorter than the whole body


class _SlowReader(BaseHTTPRequestHandler):
    """Reads the request body at a steady but unhurried pace, like a modest uplink."""

    received: dict = {}

    def setup(self) -> None:
        super().setup()
        # Keep kernel buffers small so the sender really waits on this reader.
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0) or 0)
        type(self).received = {
            "content_length": self.headers.get("content-length"),
            "transfer_encoding": self.headers.get("transfer-encoding"),
        }
        got = 0
        while got < length:
            chunk = self.rfile.read(min(READ_SLICE, length - got))
            if not chunk:
                break
            got += len(chunk)
            time.sleep(READ_PAUSE)
        type(self).received["bytes"] = got
        body = json.dumps(REPLY).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class _StopsReading(BaseHTTPRequestHandler):
    """Accepts the connection, then never reads the body: a wedged upstream."""

    def setup(self) -> None:
        super().setup()
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)

    def do_POST(self) -> None:
        time.sleep(WRITE_TIMEOUT * 4)

    def log_message(self, *args) -> None:
        pass


class TestLargePassthroughUpload(unittest.TestCase):
    server: HTTPServer
    stalled: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = stub_server.start(_SlowReader)
        cls.stalled = stub_server.start(_StopsReading)

    @classmethod
    def tearDownClass(cls) -> None:
        stub_server.stop(cls.server)
        stub_server.stop(cls.stalled)

    def test_upstream_that_stops_reading_still_times_out_with_a_reason(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        saved = (settings.anthropic_base_url, settings.connect_timeout, settings.max_retries)
        settings.anthropic_base_url = stub_server.url(self.stalled)
        settings.connect_timeout = WRITE_TIMEOUT
        settings.max_retries = 0
        raw = json.dumps({
            "model": "claude-sonnet-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "x" * BODY_BYTES}],
        }).encode()
        try:
            with TestClient(server.app) as client:
                started = time.monotonic()
                response = client.post(
                    "/v1/messages", content=raw, headers={"content-type": "application/json"}
                )
                elapsed = time.monotonic() - started
        finally:
            settings.anthropic_base_url, settings.connect_timeout, settings.max_retries = saved

        self.assertEqual(response.status_code, 502)
        message = response.json()["error"]["message"]
        self.assertIn("WriteTimeout", message)
        self.assertLess(elapsed, WRITE_TIMEOUT * 4)

    def test_slow_upload_of_a_large_body_is_not_a_write_timeout(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        saved = (settings.anthropic_base_url, settings.connect_timeout, settings.max_retries)
        settings.anthropic_base_url = stub_server.url(self.server)
        settings.connect_timeout = WRITE_TIMEOUT
        settings.max_retries = 0
        payload = {
            "model": "claude-sonnet-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "x" * BODY_BYTES}],
        }
        raw = json.dumps(payload).encode()
        try:
            with TestClient(server.app) as client:
                response = client.post(
                    "/v1/messages", content=raw, headers={"content-type": "application/json"}
                )
        finally:
            settings.anthropic_base_url, settings.connect_timeout, settings.max_retries = saved

        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertEqual(response.json()["id"], "msg_stub")
        received = _SlowReader.received
        self.assertEqual(received["bytes"], len(raw))
        # Still a plain Content-Length upload, exactly as Claude Code sent it.
        self.assertEqual(received["content_length"], str(len(raw)))
        self.assertIsNone(received["transfer_encoding"])


if __name__ == "__main__":
    unittest.main()
