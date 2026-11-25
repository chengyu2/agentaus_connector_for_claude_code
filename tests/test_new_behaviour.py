"""Tests for new behaviours from the code-review fixes.

Covers:
- POST /quality: form body, JSON body, invalid mode → 400, auth guard
- Variant model: quality applied per-request, model normalised to "agentaus"
- /clear-context: flag consumed exactly once, last user message kept
- skip_summarisation + timeout → trim fallback
- Trim lands within limit (not over)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from agentaus_bridge import server, throttle, graph  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge.compact import ConversationCompactor, estimate_request_tokens  # noqa: E402
from agentaus_bridge.server import (  # noqa: E402
    _QUALITY_PRESETS,
    _MODEL_TO_QUALITY,
    _request_quality,
    _eff,
)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


class _AgentausStub:
    """Minimal Agentaus stand-in. Always returns a short text reply."""

    def __init__(self):
        self.calls: list = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                body = json.loads(
                    self.rfile.read(int(self.headers.get("content-length", 0) or 0))
                )
                stub.calls.append(body)
                resp = json.dumps({
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "ok"}}]
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *a):
                pass

        ThreadingHTTPServer.allow_reuse_address = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# /quality endpoint
# ---------------------------------------------------------------------------

class QualityEndpoint(unittest.TestCase):
    def setUp(self):
        self._saved_mode = server._QUALITY_MODE
        self._saved_thinking = settings.agentaus_thinking
        self._saved_review = settings.agentaus_self_review
        self._saved_token = settings.bridge_token

    def tearDown(self):
        server._QUALITY_MODE = self._saved_mode
        settings.agentaus_thinking = self._saved_thinking
        settings.agentaus_self_review = self._saved_review
        settings.bridge_token = self._saved_token

    def client(self):
        return TestClient(server.app, raise_server_exceptions=False)

    def test_post_form_body_sets_mode(self):
        with self.client() as c:
            r = c.post("/quality", data={"mode": "low"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(settings.agentaus_thinking)
        self.assertFalse(settings.agentaus_self_review)

    def test_post_json_body_sets_mode(self):
        with self.client() as c:
            r = c.post("/quality", json={"mode": "medium"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(settings.agentaus_thinking)
        self.assertTrue(settings.agentaus_self_review)

    def test_post_invalid_mode_returns_400(self):
        with self.client() as c:
            r = c.post("/quality", json={"mode": "ultra"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ultra", r.json().get("error", ""))

    def test_post_requires_auth_when_bridge_token_is_set(self):
        settings.bridge_token = "secret"
        with self.client() as c:
            r = c.post("/quality", json={"mode": "low"})
        self.assertEqual(r.status_code, 401)

    def test_post_accepts_correct_token(self):
        settings.bridge_token = "secret"
        with self.client() as c:
            r = c.post("/quality", json={"mode": "low"},
                       headers={"x-api-key": "secret"})
        self.assertEqual(r.status_code, 200)

    def test_get_reflects_current_mode(self):
        settings.agentaus_thinking = False
        settings.agentaus_self_review = False
        with self.client() as c:
            r = c.get("/quality")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("mode", data)
        self.assertIn("thinking", data)
        self.assertIn("self_review", data)


# ---------------------------------------------------------------------------
# Variant model → per-request quality, model normalised to "agentaus"
# ---------------------------------------------------------------------------

class VariantModelQuality(unittest.TestCase):
    def setUp(self):
        self._stub = _AgentausStub()
        self._saved = {k: getattr(settings, k) for k in (
            "agentaus_base_url", "agentaus_api_key",
            "agentaus_thinking", "agentaus_self_review",
            "agentaus_grounding_check", "agentaus_syntax_check",
            "retry_backoff_seconds", "max_retries",
            "agentaus_upstream_concurrency",
        )}
        settings.agentaus_base_url = self._stub.url
        settings.agentaus_api_key = "test-key"
        settings.agentaus_thinking = False
        settings.agentaus_self_review = False
        settings.agentaus_grounding_check = False
        settings.agentaus_syntax_check = False
        settings.retry_backoff_seconds = 0.01
        settings.max_retries = 1
        settings.agentaus_upstream_concurrency = 10
        throttle.reset()
        graph._FIRST_SEEN.clear()
        self.addCleanup(throttle.reset)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(settings, k, v)
        self._stub.close()

    def _post(self, model: str) -> dict:
        body = {"model": model, "max_tokens": 50,
                "messages": [user("hi")]}
        with TestClient(server.app, raise_server_exceptions=False) as c:
            c.post("/v1/messages", json=body)
        return self._stub.calls[-1] if self._stub.calls else {}

    def test_variant_model_is_normalised_to_agentaus(self):
        """The stub must receive a request (proving the bridge routed to Agentaus)
        and must NOT see the original variant model id in the forwarded body."""
        received = self._post("claude-agentaus-fast")
        self.assertTrue(self._stub.calls,
                        "stub got no calls — variant model was not routed to Agentaus")
        self.assertNotEqual(received.get("model"), "claude-agentaus-fast",
                            "variant model id must not be forwarded verbatim to Agentaus")

    def test_balanced_variant_normalised_to_agentaus(self):
        received = self._post("claude-agentaus-balanced")
        self.assertTrue(self._stub.calls, "stub got no calls for claude-agentaus-balanced")
        self.assertNotEqual(received.get("model"), "claude-agentaus-balanced")

    def test_fast_model_maps_to_low_quality(self):
        self.assertEqual(_MODEL_TO_QUALITY.get("claude-agentaus-fast"), "low")
        self.assertEqual(_QUALITY_PRESETS["low"],
                         {"agentaus_thinking": False, "agentaus_self_review": False})

    def test_balanced_model_maps_to_medium_quality(self):
        self.assertEqual(_MODEL_TO_QUALITY.get("claude-agentaus-balanced"), "medium")
        self.assertEqual(_QUALITY_PRESETS["medium"],
                         {"agentaus_thinking": False, "agentaus_self_review": True})

    def test_eff_uses_per_request_override_over_global(self):
        """_eff() must return the ContextVar override, not the global setting."""
        async def _run():
            settings.agentaus_thinking = True  # global is high
            _request_quality.set(_QUALITY_PRESETS["low"])  # override to low
            return _eff("agentaus_thinking")

        result = run(_run())
        self.assertFalse(result, "_eff() must prefer the per-request override over global settings")
        # Restore
        settings.agentaus_thinking = False

    def test_eff_falls_back_to_global_when_no_override(self):
        async def _run():
            _request_quality.set(None)
            settings.agentaus_thinking = True
            return _eff("agentaus_thinking")

        result = run(_run())
        self.assertTrue(result)
        settings.agentaus_thinking = False


# ---------------------------------------------------------------------------
# /clear-context flag
# ---------------------------------------------------------------------------

class ClearContextFlag(unittest.TestCase):
    def setUp(self):
        self._stub = _AgentausStub()
        self._saved = {k: getattr(settings, k) for k in (
            "agentaus_base_url", "agentaus_api_key",
            "agentaus_thinking", "agentaus_self_review",
            "agentaus_grounding_check", "agentaus_syntax_check",
            "retry_backoff_seconds", "max_retries",
            "agentaus_upstream_concurrency",
        )}
        settings.agentaus_base_url = self._stub.url
        settings.agentaus_api_key = "test-key"
        settings.agentaus_thinking = False
        settings.agentaus_self_review = False
        settings.agentaus_grounding_check = False
        settings.agentaus_syntax_check = False
        settings.retry_backoff_seconds = 0.01
        settings.max_retries = 1
        settings.agentaus_upstream_concurrency = 10
        throttle.reset()
        graph._FIRST_SEEN.clear()
        self.addCleanup(throttle.reset)
        # Reset flag
        server._CLEAR_CONTEXT_NEXT = False

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(settings, k, v)
        server._CLEAR_CONTEXT_NEXT = False
        self._stub.close()

    def test_flag_causes_next_request_to_strip_history(self):
        messages = [
            user("old message 1"),
            assistant("reply 1"),
            user("old message 2"),
            assistant("reply 2"),
            user("current question"),
        ]
        body = {"model": "agentaus", "max_tokens": 50, "messages": messages}

        with TestClient(server.app, raise_server_exceptions=False) as c:
            c.post("/clear-context")
            c.post("/v1/messages", json=body)

        sent = self._stub.calls[-1] if self._stub.calls else {}
        # The bridge prepends a system message in OpenAI format; filter it out.
        conversation = [m for m in sent.get("messages", []) if m.get("role") != "system"]
        self.assertEqual(len(conversation), 1,
                         f"expected 1 conversation message after clear-context, got {len(conversation)}: {conversation}")
        self.assertEqual(conversation[0].get("content"), "current question")

    def test_flag_consumed_exactly_once(self):
        messages = [
            user("old 1"),
            assistant("reply 1"),
            user("current"),
        ]
        body = {"model": "agentaus", "max_tokens": 50, "messages": messages}

        with TestClient(server.app, raise_server_exceptions=False) as c:
            c.post("/clear-context")
            # First request: flag is consumed, history stripped
            c.post("/v1/messages", json=body)
            first_convo = [m for m in
                           (self._stub.calls[-1] if self._stub.calls else {}).get("messages", [])
                           if m.get("role") != "system"]
            # Second request with the same full history: flag is gone, history untouched
            c.post("/v1/messages", json=body)

        second_convo = [m for m in
                        (self._stub.calls[-1] if self._stub.calls else {}).get("messages", [])
                        if m.get("role") != "system"]
        self.assertEqual(len(first_convo), 1, "first call should have stripped to 1 message")
        self.assertEqual(len(second_convo), len(messages),
                         "second call should see the full history (flag already consumed)")

    def test_clear_context_requires_auth_when_token_set(self):
        saved_token = settings.bridge_token
        try:
            settings.bridge_token = "secret"
            with TestClient(server.app, raise_server_exceptions=False) as c:
                r = c.post("/clear-context")
            self.assertEqual(r.status_code, 401)
        finally:
            settings.bridge_token = saved_token

    def test_flag_not_set_before_post(self):
        self.assertFalse(server._CLEAR_CONTEXT_NEXT)


# ---------------------------------------------------------------------------
# skip_summarisation + timeout → trim fallback
# ---------------------------------------------------------------------------

def _large_body(turns: int = 60) -> dict:
    msgs = []
    for i in range(turns):
        msgs.append(user(f"question {i}: " + "detail " * 300))
        msgs.append(assistant(f"answer {i}"))
    msgs.append(user("the current question"))
    return {"messages": msgs, "system": "sys"}


class TrimFallback(unittest.TestCase):
    def test_skip_summarisation_produces_trimmed(self):
        """When skip_summarisation=True the compactor must trim rather than summarise."""
        async def _never_called(_: str) -> str:
            raise AssertionError("summariser should not be called when skip_summarisation=True")

        c = ConversationCompactor(_never_called)
        body = _large_body()

        plan = run(c.compact(body, limit=4000, reserve=100, skip_summarisation=True))

        self.assertEqual(plan["method"], "trimmed",
                         "skip_summarisation=True must always produce method='trimmed'")
        self.assertEqual(plan["messages"][-1], body["messages"][-1],
                         "current question must survive trimming")

    def test_summarisation_timeout_falls_back_to_trim(self):
        """A timeout during summarisation must fall back to trimming, not fail the turn."""
        async def slow(_: str) -> str:
            await asyncio.sleep(10)
            return "SUMMARY"

        c = ConversationCompactor(slow)
        body = _large_body()

        plan = run(c.compact(
            body, limit=4000, reserve=100, summarisation_timeout=0.05
        ))

        self.assertEqual(plan["method"], "trimmed",
                         "a timed-out summariser must fall back to method='trimmed'")
        self.assertEqual(plan["messages"][-1], body["messages"][-1])


# ---------------------------------------------------------------------------
# Trim target stays within limit
# ---------------------------------------------------------------------------

class TrimWithinLimit(unittest.TestCase):
    def test_trim_result_does_not_exceed_limit(self):
        """After trimming, total tokens must not exceed limit."""
        async def broken(_: str) -> str:
            raise RuntimeError("simulated failure")

        c = ConversationCompactor(broken)
        limit = 4000
        reserve = 100
        body = _large_body(turns=80)

        plan = run(c.compact(body, limit=limit, reserve=reserve))

        self.assertEqual(plan["method"], "trimmed")
        total = estimate_request_tokens(
            {"system": body.get("system"), "messages": plan["messages"],
             "tools": body.get("tools")}
        ) + reserve
        self.assertLessEqual(
            total, limit,
            f"trimmed result ({total} tokens) exceeds limit ({limit})"
        )

    def test_trim_target_does_not_exceed_limit_when_threshold_above_1(self):
        """trim_target = min(trigger, limit) must clamp threshold > 1.0 to limit."""
        async def broken(_: str) -> str:
            raise RuntimeError("failure")

        c = ConversationCompactor(broken)
        limit = 3000
        reserve = 100
        body = _large_body(turns=60)

        # threshold > 1.0 means trigger = int(limit * 1.5) = 4500 > limit
        plan = run(c.compact(body, limit=limit, reserve=reserve, threshold=1.5))

        self.assertIn(plan["method"], ("trimmed", "none"))
        if plan["method"] == "trimmed":
            total = estimate_request_tokens(
                {"system": body.get("system"), "messages": plan["messages"],
                 "tools": body.get("tools")}
            ) + reserve
            self.assertLessEqual(total, limit,
                                 "trim_target was not clamped to limit")


if __name__ == "__main__":
    unittest.main()
