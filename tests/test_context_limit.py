"""Agentaus has a 131,072-token context window. Two failure modes are covered here.

1. Over-length requests must be rejected with an actionable message rather than
   being sent upstream.

2. Agentaus reports an over-length *streaming* request as HTTP 200 with the error
   buried in the SSE body:

       HTTP/1.1 200 OK
       data: {"error":{"code":400,"message":"The engine prompt length 224662
              exceeds the max_model_len 131072. Please reduce prompt."}}
       data: [DONE]

   The bridge parser reads chunk["choices"], which an error object does not have,
   so before this fix the error was skipped and the turn ended as an empty but
   apparently successful message - a failure with no explanation anywhere.
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

from agentaus_bridge import server as server_mod  # noqa: E402

PORT = 9941
OVERSIZE_ERROR = {
    "error": {
        "code": 400,
        "message": "The engine prompt length 224662 exceeds the max_model_len 131072. "
                   "Please reduce prompt.",
        "type": "invalid_request_error",
    }
}


class _InBandErrorUpstream(BaseHTTPRequestHandler):
    """Reproduces Agentaus: HTTP 200, error inside the body."""

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        streaming = json.loads(raw or b"{}").get("stream")
        self.send_response(200)
        if streaming:
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(OVERSIZE_ERROR)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps(OVERSIZE_ERROR).encode()
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


_SERVER: HTTPServer | None = None


def setUpModule() -> None:
    global _SERVER
    HTTPServer.allow_reuse_address = True
    _SERVER = HTTPServer(("127.0.0.1", PORT), _InBandErrorUpstream)
    threading.Thread(target=_SERVER.serve_forever, daemon=True).start()


def tearDownModule() -> None:
    if _SERVER is not None:
        _SERVER.shutdown()
        _SERVER.server_close()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        from agentaus_bridge.config import settings

        self._saved = (
            settings.agentaus_base_url,
            settings.agentaus_api_key,
            settings.agentaus_max_input_tokens,
        )
        settings.agentaus_base_url = f"http://127.0.0.1:{PORT}"
        settings.agentaus_api_key = "test-key"
        # The learned window is module state; without clearing it a limit picked up
        # by an earlier test silently overrides the one set here.
        server_mod._reset_learned_limit()
        self._saved_explicit = settings.max_input_tokens_is_explicit
        settings.max_input_tokens_is_explicit = True

    def tearDown(self) -> None:
        from agentaus_bridge.config import settings

        (
            settings.agentaus_base_url,
            settings.agentaus_api_key,
            settings.agentaus_max_input_tokens,
        ) = self._saved
        settings.max_input_tokens_is_explicit = self._saved_explicit
        server_mod._reset_learned_limit()

    def _post(self, text: str, stream: bool, max_tokens: int = 64):
        from agentaus_bridge import server

        with TestClient(server.app) as client:
            return client.post(
                "/v1/messages",
                json={
                    "model": "agentaus",
                    "max_tokens": max_tokens,
                    "stream": stream,
                    "messages": [{"role": "user", "content": text}],
                },
            )


class TestPreflightGuard(_Base):
    def test_oversized_request_is_rejected_before_going_upstream(self):
        from agentaus_bridge.config import settings

        settings.agentaus_max_input_tokens = 1000
        response = self._post("x" * 40_000, stream=False)  # ~10k estimated tokens

        self.assertEqual(response.status_code, 400)
        message = response.json()["error"]["message"]
        self.assertIn("prompt is too long", message)
        self.assertIn("/model opus", message, "must name the recovery that actually works")
        self.assertIn("/compact on Agentaus will fail", message,
                      "must warn that /compact deadlocks when already over the window")

    def test_reply_allowance_counts_toward_the_window(self):
        """Agentaus counts prompt + reply against one window."""
        from agentaus_bridge.config import settings

        settings.agentaus_max_input_tokens = 1000
        # ~500 prompt tokens is under the limit on its own, but not with 800 reserved.
        response = self._post("x" * 2_000, stream=False, max_tokens=800)

        self.assertEqual(response.status_code, 400)

    def test_normal_request_is_not_blocked(self):
        from agentaus_bridge.config import settings

        settings.agentaus_max_input_tokens = 131072
        response = self._post("hello", stream=False)

        # The stub always answers with an over-length error, so "prompt is too long"
        # appears either way. What distinguishes the guard is that it names the env
        # var; the upstream path instead quotes Agentaus verbatim.
        self.assertNotIn("AGENTAUS_MAX_INPUT_TOKENS", response.text,
                         "guard fired on a request well under the limit")
        self.assertIn("Agentaus said:", response.text, "should have reached upstream")

    def test_guard_can_be_disabled(self):
        from agentaus_bridge.config import settings

        settings.agentaus_max_input_tokens = 0
        response = self._post("x" * 40_000, stream=False)

        self.assertNotIn("AGENTAUS_MAX_INPUT_TOKENS", response.text,
                         "guard fired despite being disabled")
        self.assertIn("Agentaus said:", response.text, "should have reached upstream")


class TestInBandErrorSurfacing(_Base):
    """The core bug: HTTP 200 + error in body became a silent empty reply."""

    def setUp(self) -> None:
        super().setUp()
        from agentaus_bridge.config import settings

        settings.agentaus_max_input_tokens = 0  # bypass the guard to reach the stub

    def test_streaming_in_band_error_reaches_the_client(self):
        body = self._post("hi", stream=True).text

        self.assertIn("error", body.lower(), "the error vanished from the stream")
        self.assertIn("max_model_len", body, "the upstream reason must survive")
        self.assertIn("message_stop", body, "stream must still terminate")

    def test_streaming_error_is_not_an_empty_success(self):
        """The exact regression: message_start -> message_stop with no content."""
        body = self._post("hi", stream=True).text

        has_error_event = "event: error" in body or '"type": "error"' in body
        self.assertTrue(has_error_event, f"no error event in stream:\n{body[:400]}")

    def test_buffered_in_band_error_reaches_the_client(self):
        body = self._post("hi", stream=False).text

        self.assertIn("max_model_len", body)

    def test_context_error_includes_guidance(self):
        body = self._post("hi", stream=True).text

        self.assertIn("/model opus", body, "an over-length error must name a working recovery")


class TestCanonicalOverLengthWording(_Base):
    """Claude Code matches on Anthropic's "prompt is too long" to auto-compact and
    retry. If our wording drifts, an over-length turn dies instead of recovering."""

    def test_preflight_error_uses_canonical_wording(self):
        from agentaus_bridge.config import settings

        settings.agentaus_max_input_tokens = 1000
        message = self._post("x" * 40_000, stream=False).json()["error"]["message"]

        self.assertTrue(
            message.startswith("prompt is too long:"),
            f"Claude Code will not recognise this as over-length: {message[:80]!r}",
        )

    def test_upstream_error_uses_canonical_wording(self):
        from agentaus_bridge.config import settings

        settings.agentaus_max_input_tokens = 0  # let the stub's error through
        body = self._post("hi", stream=True).text

        self.assertIn("prompt is too long", body)


if __name__ == "__main__":
    unittest.main()


class TestTrimReachesUpstream(unittest.TestCase):
    """Regression: trimming was applied to `body` *after* the upstream payload had
    already been built from it, so the full untrimmed conversation was still sent.
    Unit-testing the trim function in isolation cannot catch that - only asserting
    on what the upstream actually received can."""

    PORT = 9942

    @classmethod
    def setUpClass(cls) -> None:
        cls.received = []
        received = cls.received

        class _Recorder(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
                received.append(json.loads(raw or b"{}"))
                body = json.dumps({
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a) -> None:
                pass

        HTTPServer.allow_reuse_address = True
        cls.server = HTTPServer(("127.0.0.1", cls.PORT), _Recorder)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_upstream_receives_the_trimmed_conversation(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        self.received.clear()
        saved = (settings.agentaus_base_url, settings.agentaus_api_key,
                 settings.agentaus_max_input_tokens, settings.agentaus_auto_trim)
        settings.agentaus_base_url = f"http://127.0.0.1:{self.PORT}"
        settings.agentaus_api_key = "k"
        settings.agentaus_max_input_tokens = 2000
        settings.agentaus_auto_trim = True
        settings.max_input_tokens_is_explicit = True
        server_mod._reset_learned_limit()
        try:
            messages = []
            for i in range(20):
                messages.append({"role": "user", "content": f"turn {i} " + "filler " * 400})
                messages.append({"role": "assistant", "content": "ok"})
            messages.append({"role": "user", "content": "final question"})
            with TestClient(server.app) as client:
                client.post("/v1/messages", json={
                    "model": "agentaus", "max_tokens": 32,
                    "stream": False, "messages": messages,
                })
        finally:
            (settings.agentaus_base_url, settings.agentaus_api_key,
             settings.agentaus_max_input_tokens, settings.agentaus_auto_trim) = saved

        self.assertGreaterEqual(len(self.received), 1, "upstream was not called")
        # Compaction summarises first, so the actual turn is the final request.
        sent = self.received[-1]["messages"]
        self.assertLess(len(sent), len(messages),
                        "upstream got the full conversation - the trim was discarded")
        blob = json.dumps(sent)
        self.assertIn("final question", blob, "the question being answered was lost")
        self.assertLess(len(blob) // 4, 2000 * 2, "trimmed payload still far over the limit")
