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
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import stub_server  # noqa: E402

# Mutated per-test to drive the retry-policy stub's behaviour.
STATE = {"calls": 0, "fail_times": 0, "fail_status": 402, "fail_body": ""}

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
        cls.server = stub_server.start(_GzipUpstream)

    @classmethod
    def tearDownClass(cls) -> None:
        stub_server.stop(cls.server)

    def test_gzipped_upstream_response_reaches_the_client_as_json(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        original = settings.anthropic_base_url
        settings.anthropic_base_url = stub_server.url(self.server)
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
        settings.anthropic_base_url = stub_server.url(self.server)
        try:
            with TestClient(server.app) as client:
                response = client.post(
                    "/v1/messages",
                    json={"model": "claude-sonnet-5", "messages": [], "max_tokens": 8},
                )
        finally:
            settings.anthropic_base_url = original

        self.assertNotIn("content-encoding", {k.lower() for k in response.headers})


class _StatusUpstream(BaseHTTPRequestHandler):
    """Stands in for api.anthropic.com, answering with a caller-chosen status.

    The first `fail_times` calls answer `fail_status`; afterwards the normal 200
    payload. Counts every request it sees so tests can assert how many times the
    bridge actually dialled upstream.
    """

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        STATE["calls"] += 1
        if STATE["calls"] <= STATE["fail_times"]:
            body = STATE["fail_body"].encode()
            self.send_response(STATE["fail_status"])
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(PAYLOAD).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


# The 402 body Anthropic sends for a billing error. The passthrough must hand it
# back byte-for-byte so the client sees the real account problem, not a bridge rewrite.
_BILLING_ERROR = json.dumps({
    "type": "error",
    "error": {"type": "billing_error", "message": "Your credit balance is too low."},
})


class TestPassthroughRetryPolicy(unittest.TestCase):
    """The passthrough must not borrow the Agentaus 402-retry policy.

    Agentaus's 402 is a spent quota that is waited out with backoff; Anthropic's 402 is
    a billing error that no retry can fix. Retrying it used to stall the turn several
    backoff cycles before returning the same answer, so the passthrough now returns it
    immediately.
    """

    server: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = stub_server.start(_StatusUpstream)

    @classmethod
    def tearDownClass(cls) -> None:
        stub_server.stop(cls.server)

    def setUp(self) -> None:
        STATE.update({"calls": 0, "fail_times": 0, "fail_status": 402,
                      "fail_body": _BILLING_ERROR})
        from agentaus_bridge.config import settings

        self._saved = (
            settings.anthropic_base_url,
            settings.passthrough_enabled,
            settings.max_retries,
            settings.retry_backoff_seconds,
        )
        settings.anthropic_base_url = stub_server.url(self.server)
        settings.passthrough_enabled = True
        settings.max_retries = 2
        settings.retry_backoff_seconds = 0.01  # keep the suite fast

    def tearDown(self) -> None:
        from agentaus_bridge.config import settings

        (
            settings.anthropic_base_url,
            settings.passthrough_enabled,
            settings.max_retries,
            settings.retry_backoff_seconds,
        ) = self._saved

    def _post(self):
        from agentaus_bridge import server

        with TestClient(server.app) as client:
            return client.post(
                "/v1/messages",
                json={
                    "model": "claude-sonnet-5",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

    def test_a_402_billing_error_is_returned_immediately(self):
        STATE["fail_times"] = 99  # every attempt would answer 402

        response = self._post()

        self.assertEqual(response.status_code, 402)
        self.assertEqual(STATE["calls"], 1,
                         "a 402 billing error must not be retried in passthrough")
        self.assertEqual(response.content, _BILLING_ERROR.encode(),
                         "the upstream 402 body must reach the client unchanged")

    def test_a_transient_502_is_still_retried(self):
        STATE["fail_times"] = 2  # exactly max_retries; the third attempt succeeds
        STATE["fail_status"] = 502

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(STATE["calls"], 3, "a 502 gateway blip is worth the retries")
        self.assertEqual(response.json()["id"], "msg_stub")

    def test_the_402_exclusion_leaves_the_rest_of_the_policy_alone(self):
        from agentaus_bridge.server import _PASSTHROUGH_RETRYABLE_STATUS, _RETRYABLE_STATUS

        self.assertIn(402, _RETRYABLE_STATUS,
                      "the Agentaus policy must keep waiting out its quota 402s")
        self.assertEqual(_PASSTHROUGH_RETRYABLE_STATUS, _RETRYABLE_STATUS - {402})


if __name__ == "__main__":
    unittest.main()
