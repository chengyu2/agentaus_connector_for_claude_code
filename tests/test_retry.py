"""Tests for transient-failure retries.

What these guard against: a single DNS blip or gateway 502 used to surface in Claude
Code as a hard "API Error 502" that killed the turn, because every upstream call was
one-shot. The observed failure was:

    WARNING agentaus-bridge: agentaus stream failed:
        [Errno 8] nodename nor servname provided, or not known

The second property tested here matters just as much as the retry itself: a retry must
never replay content the client has already seen, so a fault *after* the first text
delta has to fail rather than restart.
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

# Mutated per-test to drive the stub's behaviour.
STATE = {"fail_times": 0, "calls": 0, "mode": "ok"}

COMPLETION = {
    "id": "chatcmpl-stub",
    "object": "chat.completion",
    "model": "agentaus",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "hello from agentaus"},
        }
    ],
    "usage": {"input_tokens": 11, "output_tokens": 4},
}


class _FlakyUpstream(BaseHTTPRequestHandler):
    """Stands in for Agentaus, failing the first `fail_times` calls."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        STATE["calls"] += 1

        if STATE["calls"] <= STATE["fail_times"]:
            self.send_response(502)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"bad gateway")
            return

        if STATE["mode"] == "die_midstream":
            # Emit one text delta, then drop the connection. A retry here would
            # duplicate the text the client has already rendered.
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            chunk = {"choices": [{"index": 0, "delta": {"content": "partial "}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            self.close_connection = True
            raise ConnectionResetError("stub drops the socket mid-stream")

        if STATE["mode"] == "stream":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            chunk = {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "hello from agentaus"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"input_tokens": 11, "output_tokens": 4},
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return

        body = json.dumps(COMPLETION).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


_SERVER: HTTPServer | None = None


def setUpModule() -> None:
    """One stub server for every class here.

    Binding per-class leaves the previous socket in TIME_WAIT and the second class
    fails with EADDRINUSE.
    """
    global _SERVER
    HTTPServer.allow_reuse_address = True
    _SERVER = HTTPServer(("127.0.0.1", PORT), _FlakyUpstream)
    threading.Thread(target=_SERVER.serve_forever, daemon=True).start()


def tearDownModule() -> None:
    if _SERVER is not None:
        _SERVER.shutdown()
        _SERVER.server_close()


class _RetryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        from agentaus_bridge.config import settings

        STATE.update({"fail_times": 0, "calls": 0, "mode": "ok"})
        self._saved = (
            settings.agentaus_base_url,
            settings.agentaus_api_key,
            settings.retry_backoff_seconds,
            settings.max_retries,
        )
        settings.agentaus_base_url = f"http://127.0.0.1:{PORT}"
        settings.agentaus_api_key = "test-key"
        settings.retry_backoff_seconds = 0.01  # keep the suite fast
        from agentaus_bridge import server as _srv
        _srv._reset_learned_limit()
        settings.max_retries = 2

    def tearDown(self) -> None:
        from agentaus_bridge.config import settings

        (
            settings.agentaus_base_url,
            settings.agentaus_api_key,
            settings.retry_backoff_seconds,
            settings.max_retries,
        ) = self._saved

    def _post(self, stream: bool):
        from agentaus_bridge import server

        with TestClient(server.app) as client:
            return client.post(
                "/v1/messages",
                json={
                    "model": "agentaus",
                    "max_tokens": 64,
                    "stream": stream,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )


class TestNonStreamingRetry(_RetryTestBase):
    def test_transient_502_is_retried_and_succeeds(self):
        STATE["fail_times"] = 2  # exactly max_retries

        response = self._post(stream=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(STATE["calls"], 3, "expected two retries after the initial attempt")
        self.assertEqual(response.json()["content"][0]["text"], "hello from agentaus")

    def test_persistent_failure_still_surfaces_an_error(self):
        """Retries must not mask a genuinely broken upstream."""
        STATE["fail_times"] = 99

        response = self._post(stream=False)

        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(STATE["calls"], 3, "should stop after max_retries attempts")

    def test_success_makes_exactly_one_call(self):
        response = self._post(stream=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(STATE["calls"], 1, "a healthy upstream must not be called twice")


class TestStreamingRetry(_RetryTestBase):
    def test_transient_502_is_retried_before_any_content(self):
        STATE.update({"fail_times": 2, "mode": "stream"})

        response = self._post(stream=True)
        body = response.text

        self.assertEqual(response.status_code, 200)
        self.assertEqual(STATE["calls"], 3)
        self.assertIn("hello from agentaus", body)
        # The retry must stay invisible: no error event should reach the client.
        self.assertNotIn('"type": "error"', body)

    def test_failure_after_first_delta_is_not_retried(self):
        """A retry here would re-send text the user has already seen."""
        STATE.update({"fail_times": 0, "mode": "die_midstream"})

        response = self._post(stream=True)
        body = response.text

        self.assertEqual(STATE["calls"], 1, "must not replay a stream that already emitted text")
        self.assertEqual(body.count("partial "), 1, "text was duplicated by a retry")
        # The turn still has to terminate cleanly or Claude Code hangs on the stream.
        self.assertIn("message_stop", body)


if __name__ == "__main__":
    unittest.main()


class TestDnsFailureRetry(_RetryTestBase):
    """The exact fault from the log: getaddrinfo fails, httpx raises ConnectError.

        WARNING agentaus-bridge: agentaus stream failed:
            [Errno 8] nodename nor servname provided, or not known

    The error is injected rather than produced by a real unresolvable hostname: a
    genuine .invalid lookup blocks on the system resolver for ~12s per attempt, which
    made this suite take over a minute.
    """

    GAIERROR = "[Errno 8] nodename nor servname provided, or not known"

    def _patch_dns_failure(self, failures: int):
        """Fail the first `failures` connection attempts with a DNS error."""
        import httpx

        real_post = httpx.AsyncClient.post
        real_stream = httpx.AsyncClient.stream
        calls = {"n": 0}

        async def flaky_post(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise httpx.ConnectError(TestDnsFailureRetry.GAIERROR)
            return await real_post(self, *args, **kwargs)

        def flaky_stream(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise httpx.ConnectError(TestDnsFailureRetry.GAIERROR)
            return real_stream(self, *args, **kwargs)

        httpx.AsyncClient.post = flaky_post
        httpx.AsyncClient.stream = flaky_stream
        self.addCleanup(setattr, httpx.AsyncClient, "post", real_post)
        self.addCleanup(setattr, httpx.AsyncClient, "stream", real_stream)
        return calls

    def test_dns_blip_recovers_without_the_user_seeing_it(self):
        """Two failed lookups then success - the turn should survive."""
        calls = self._patch_dns_failure(failures=2)

        with self.assertLogs("agentaus-bridge", level="WARNING") as logs:
            response = self._post(stream=False)

        self.assertEqual(response.status_code, 200, "a transient DNS blip killed the turn")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len([m for m in logs.output if "retrying in" in m]), 2)
        self.assertIn("nodename nor servname", "\n".join(logs.output))

    def test_persistent_dns_failure_still_reports_an_error(self):
        calls = self._patch_dns_failure(failures=99)

        with self.assertLogs("agentaus-bridge", level="WARNING"):
            response = self._post(stream=False)

        self.assertGreaterEqual(response.status_code, 400, "a dead host must still error")
        self.assertEqual(calls["n"], 3, "should stop after max_retries attempts")

    def test_streaming_dns_blip_recovers(self):
        STATE["mode"] = "stream"
        calls = self._patch_dns_failure(failures=2)

        with self.assertLogs("agentaus-bridge", level="WARNING"):
            body = self._post(stream=True).text

        self.assertEqual(calls["n"], 3)
        self.assertIn("hello from agentaus", body)
        self.assertNotIn("error", body.lower().split("message_stop")[0][:200])

    def test_exhausted_stream_retries_still_close_the_turn(self):
        """Even after giving up, the SSE stream must terminate or Claude Code hangs."""
        self._patch_dns_failure(failures=99)

        with self.assertLogs("agentaus-bridge", level="WARNING"):
            body = self._post(stream=True).text

        self.assertIn("message_stop", body, "stream never terminated; Claude Code would hang")


class TestBackoffCurve(unittest.TestCase):
    """Delays must actually grow, and stay bounded."""

    def setUp(self) -> None:
        from agentaus_bridge.config import settings

        self._saved = (settings.retry_backoff_seconds, settings.retry_max_delay_seconds)
        settings.retry_backoff_seconds = 1.0
        settings.retry_max_delay_seconds = 8.0

    def tearDown(self) -> None:
        from agentaus_bridge.config import settings

        settings.retry_backoff_seconds, settings.retry_max_delay_seconds = self._saved

    def test_delay_increases_with_each_attempt(self):
        from agentaus_bridge.server import _retry_delay

        # Jitter is bounded by retry_backoff_seconds, so compare worst-case to
        # best-case: attempt N's floor must exceed attempt N-1's ceiling.
        for attempt in range(4):
            lo = min(_retry_delay(attempt) for _ in range(200))
            hi = max(_retry_delay(attempt) for _ in range(200))
            self.assertGreaterEqual(lo, 2**attempt, f"attempt {attempt} floor too low")
            self.assertLessEqual(hi, min(2**attempt, 8.0) + 1.0, f"attempt {attempt} ceiling")

    def test_delay_is_capped(self):
        from agentaus_bridge.server import _retry_delay

        # Without a cap, attempt 20 would be 2**20 seconds - a 12-day wait.
        self.assertLessEqual(_retry_delay(20), 8.0 + 1.0)


class TestTimeoutIsTreatedAsTooBig(unittest.TestCase):
    """A Cloudflare 524 says the origin took too long, not that the prompt was wrong.

    For a large conversation that is the same actionable signal as an explicit
    over-length rejection: send less. Nothing in the response says so, though, so a
    plain retry replays the identical payload and times out identically - which is how a
    turn burned every retry it had and failed anyway.
    """

    def test_gateway_timeouts_are_recognised(self):
        from agentaus_bridge.server import _is_too_slow
        for text in ("Agentaus returned HTTP 524: <!DOCTYPE html>",
                     "522 connection timed out", "504 Gateway Timeout",
                     "<title>Gateway time-out</title>"):
            self.assertTrue(_is_too_slow(text), text)

    def test_other_failures_are_not(self):
        from agentaus_bridge.server import _is_too_slow
        for text in ("HTTP 401 unauthorized", "HTTP 400 invalid request",
                     "the engine prompt length 224662 exceeds the max_model_len 131072"):
            self.assertFalse(_is_too_slow(text), text)

    def test_an_over_length_error_is_still_recognised_separately(self):
        from agentaus_bridge.server import _is_over_length, _is_too_slow
        text = "The engine prompt length 224662 exceeds the max_model_len 131072"
        self.assertTrue(_is_over_length(text))
        self.assertFalse(_is_too_slow(text), "the two signals must stay distinguishable")
