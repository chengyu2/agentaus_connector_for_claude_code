"""Gemini models through the bridge: routing, translation and thought signatures, against a
stand-in for Google's OpenAI-compatible endpoint shaped like the live one (24 Nov 2026)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import stub_server  # noqa: E402

STATE: dict = {"seen": [], "replies": []}

# As the live API streams two parallel calls: whole, no `index`, signature on the first only,
# and finish_reason "stop" although it called tools.
TOOL_STREAM = [
    {"choices": [{"delta": {"role": "assistant", "tool_calls": [{
        "extra_content": {"google": {"thought_signature": "SIG-A"}},
        "function": {"arguments": "{\"city\":\"Sydney\"}", "name": "get_weather"},
        "id": "call_111", "type": "function"}]}, "index": 0}]},
    {"choices": [{"delta": {"role": "assistant", "tool_calls": [{
        "function": {"arguments": "{\"city\":\"Perth\"}", "name": "get_weather"},
        "id": "call_222", "type": "function"}]}, "index": 0}]},
    {"choices": [{"delta": {"role": "assistant"}, "finish_reason": "stop", "index": 0}],
     "usage": {"completion_tokens": 40, "prompt_tokens": 100}},
]


class _Gemini(BaseHTTPRequestHandler):
    """Stands in for generativelanguage.googleapis.com/v1beta."""

    def do_GET(self) -> None:
        if "nope" in self.path:
            self._send(404, "application/json", b'{"error": {"code": 404}}')
            return
        body = json.dumps({"name": "models/x", "displayName": "Gemini 4 Hydrogen",
                           "outputTokenLimit": 32768}).encode()
        self._send(200, "application/json", body)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        STATE["seen"].append({"path": self.path, "payload": payload,
                              "auth": self.headers.get("authorization")})
        status, reply = STATE["replies"].pop(0)
        if status != 200:
            self._send(status, "application/json", json.dumps(reply).encode())
        elif payload.get("stream"):
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in reply) + "data: [DONE]\n\n"
            self._send(200, "text/event-stream", body.encode())
        else:
            self._send(200, "application/json", json.dumps(reply).encode())

    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _events(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


class Gemini(unittest.TestCase):
    server: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = stub_server.start(_Gemini)

    @classmethod
    def tearDownClass(cls) -> None:
        stub_server.stop(cls.server)

    def setUp(self) -> None:
        from agentaus_bridge import gemini
        from agentaus_bridge.config import settings
        STATE.update({"seen": [], "replies": []})
        gemini._SIGNATURES.clear()
        gemini._OUTPUT_LIMITS.clear()
        self._saved = (settings.gemini_api_key, settings.gemini_base_url, gemini._RETRY_DELAYS_S,
                       gemini._QUOTA_DELAYS_S)
        settings.gemini_api_key = "test-key"
        settings.gemini_base_url = stub_server.url(self.server)
        gemini._RETRY_DELAYS_S = (0.0, 0.0)
        gemini._QUOTA_DELAYS_S = (0.0, 0.0)

    def tearDown(self) -> None:
        from agentaus_bridge import gemini
        from agentaus_bridge.config import settings
        (settings.gemini_api_key, settings.gemini_base_url, gemini._RETRY_DELAYS_S,
         gemini._QUOTA_DELAYS_S) = self._saved

    def post(self, body: dict, path: str = "/v1/messages"):
        from agentaus_bridge import server
        with TestClient(server.app) as client:
            return client.post(path, json=body)

    def test_a_streamed_parallel_call_becomes_two_tool_uses_and_keeps_its_signature(self):
        STATE["replies"] = [(200, TOOL_STREAM)]
        body = {"model": "gemini-4-hydrogen", "stream": True, "max_tokens": 64000,
                "system": [{"type": "text", "text": "Be brief."}],
                "messages": [{"role": "user", "content": "Weather in Sydney and Perth?"}],
                "tools": [{"name": "get_weather", "description": "Weather.",
                           "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]}
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.text)
        events = _events(response.text)
        starts = [e["content_block"] for e in events if e["type"] == "content_block_start"]
        self.assertEqual([(s["type"], s["id"]) for s in starts],
                         [("tool_use", "call_111"), ("tool_use", "call_222")])
        stop = next(e for e in events if e["type"] == "message_delta")
        self.assertEqual(stop["delta"]["stop_reason"], "tool_use")

        sent = STATE["seen"][0]
        self.assertEqual(sent["path"], "/openai/chat/completions")
        self.assertEqual(sent["auth"], "Bearer test-key")
        # Capped by the model's own outputTokenLimit, read from the API.
        self.assertEqual(sent["payload"]["max_tokens"], 32768)
        self.assertEqual(sent["payload"]["messages"][0], {"role": "system", "content": "Be brief."})

        # The next turn sends the signature back on the first call only.
        STATE["replies"] = [(200, {"choices": [{"message": {"content": "Both sunny."},
                                                "finish_reason": "stop"}]})]
        follow = dict(body, stream=False, messages=body["messages"] + [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_111", "name": "get_weather", "input": {"city": "Sydney"}},
                {"type": "tool_use", "id": "call_222", "name": "get_weather", "input": {"city": "Perth"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_111", "content": "22C"},
                {"type": "tool_result", "tool_use_id": "call_222", "content": [{"type": "text", "text": "30C"}]}]}])
        reply = self.post(follow)
        self.assertEqual(reply.json()["content"], [{"type": "text", "text": "Both sunny."}])
        messages = STATE["seen"][1]["payload"]["messages"]
        calls = messages[2]["tool_calls"]
        self.assertEqual(calls[0]["extra_content"], {"google": {"thought_signature": "SIG-A"}})
        self.assertNotIn("extra_content", calls[1])
        self.assertEqual([(m["role"], m["content"]) for m in messages[3:]],
                         [("tool", "22C"), ("tool", "30C")])

    def test_a_call_gemini_never_signed_gets_the_placeholder(self):
        from agentaus_bridge import gemini
        payload = gemini.to_openai({"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "toolu_old", "name": "Read", "input": {"file_path": "a"}},
                {"type": "tool_use", "id": "toolu_old2", "name": "Read", "input": {"file_path": "b"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_old", "content": [
                    {"type": "text", "text": "a screenshot"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}]},
                {"type": "tool_result", "tool_use_id": "toolu_old2", "content": "boom", "is_error": True}]},
        ]}, "gemini-4-hydrogen", stream=False, max_output=1000)
        assistant = payload["messages"][1]
        self.assertEqual(assistant["content"], "Checking.")
        self.assertEqual(assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
                         gemini.DUMMY_SIGNATURE)
        self.assertNotIn("extra_content", assistant["tool_calls"][1])
        self.assertEqual(payload["messages"][2], {"role": "tool", "tool_call_id": "toolu_old",
                                                  "content": "a screenshot"})
        self.assertEqual(payload["messages"][3]["content"], "Error: boom")
        # The tool's image follows the results, as Gemini can see it.
        images = payload["messages"][4]["content"]
        self.assertEqual(images[1], {"type": "image_url",
                                     "image_url": {"url": "data:image/png;base64,QUJD"}})
        self.assertEqual(payload["max_tokens"], 1000)

    def test_googles_list_shaped_error_is_retried_then_passed_on(self):
        overloaded = [{"error": {"code": 503, "message": "This model is currently experiencing high demand.",
                                 "status": "UNAVAILABLE"}}]
        STATE["replies"] = [(503, overloaded)] * 3
        response = self.post({"model": "gemini-3.8-flash", "max_tokens": 100,
                              "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["type"], "overloaded_error")
        self.assertIn("high demand", response.json()["error"]["message"])
        self.assertEqual(len(STATE["seen"]), 3)

    def test_a_retry_that_succeeds_is_invisible(self):
        STATE["replies"] = [(503, [{"error": {"message": "busy"}}]),
                            (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 5, "completion_tokens": 1}})]
        response = self.post({"model": "gemini-3.8-flash", "max_tokens": 100,
                              "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["usage"],
                         {"input_tokens": 5, "cache_read_input_tokens": 0, "output_tokens": 1})

    def test_a_note_that_arrives_mid_conversation_leaves_the_start_of_the_request_alone(self):
        from agentaus_bridge import gemini
        turn = [{"role": "system", "content": "env: cwd is /repo"},
                {"role": "user", "content": "Fix the bug."},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "a"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "x = 1"}]}]
        later = turn + [
            {"role": "assistant", "content": [{"type": "text", "text": "Found it."}]},
            {"role": "system", "content": "A new MCP server connected."},
            {"role": "user", "content": "Go on."}]
        first = gemini.to_openai({"system": "Be brief.", "messages": turn}, "gemini-4-hydrogen",
                                 stream=False, max_output=100)["messages"]
        second = gemini.to_openai({"system": "Be brief.", "messages": later}, "gemini-4-hydrogen",
                                  stream=False, max_output=100)["messages"]
        # Gemini's implicit cache reuses an unchanged start: the earlier request is a prefix.
        self.assertEqual(second[:len(first)], first)
        self.assertEqual(first[0], {"role": "system", "content": "Be brief.\n\nenv: cwd is /repo"})
        self.assertEqual(second[-1]["content"],
                         "<system-reminder>\nA new MCP server connected.\n</system-reminder>\nGo on.")

    def test_cache_hits_are_reported_the_way_claudes_api_reports_them(self):
        from agentaus_bridge import gemini
        usage = {"prompt_tokens": 1000, "completion_tokens": 5,
                 "prompt_tokens_details": {"cached_tokens": 900},
                 "completion_tokens_details": {"reasoning_tokens": 40}}
        STATE["replies"] = [(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                   "usage": usage})]
        response = self.post({"model": "gemini-3.8-flash", "max_tokens": 100,
                              "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.json()["usage"], {"input_tokens": 100, "cache_read_input_tokens": 900,
                                                    "output_tokens": 5})
        self.assertEqual(gemini.usage_line(usage), "in=1000 cached=900 out=5 thinking=40")

    def test_a_quota_429_waits_as_long_as_google_asks_and_passes_a_long_wait_on(self):
        quota = [{"error": {"code": 429, "message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED",
                            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0.01s"}]}}]
        STATE["replies"] = [(429, quota), (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})]
        body = {"model": "gemini-3.8-flash", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(len(STATE["seen"]), 2)

        long_wait = [{"error": {"code": 429, "message": "Quota exceeded. Please retry in 45.2s.",
                                "status": "RESOURCE_EXHAUSTED"}}]
        STATE.update({"seen": [], "replies": [(429, long_wait)]})
        response = self.post(body)
        # Not held for 45 s: handed straight to Claude Code, told how long to wait.
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "45")
        self.assertEqual(response.json()["error"]["type"], "rate_limit_error")
        self.assertEqual(len(STATE["seen"]), 1)

    def test_a_subagent_of_an_agentaus_session_stays_on_agentaus(self):
        from types import SimpleNamespace
        from agentaus_bridge import server, sessions
        sessions.note_main_loop("sess-1", True)
        body = {"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "hi"}]}
        subagent = SimpleNamespace(headers={"x-claude-code-session-id": "sess-1",
                                            "x-claude-code-agent-id": "agent-1"})
        main_loop = SimpleNamespace(headers={"x-claude-code-session-id": "sess-1"})
        self.assertFalse(server._to_gemini(subagent, body, "gemini-4-hydrogen"))
        self.assertTrue(server._to_gemini(main_loop, body, "gemini-4-hydrogen"))

    def test_without_a_key_the_turn_says_how_to_fix_it(self):
        from agentaus_bridge.config import settings
        settings.gemini_api_key = ""
        response = self.post({"model": "gemini-4-hydrogen", "max_tokens": 10,
                              "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 401)
        self.assertIn("GEMINI_API_KEY", response.json()["error"]["message"])
        self.assertEqual(STATE["seen"], [])

    def test_claude_codes_model_lookup_is_answered_and_a_typo_refused(self):
        from agentaus_bridge import server
        with TestClient(server.app) as client:
            found = client.get("/v1/models/gemini-4-hydrogen")
            missing = client.get("/v1/models/gemini-nope")
            agentaus = client.get("/v1/models/agentaus")
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["id"], "gemini-4-hydrogen")
        self.assertEqual(found.json()["display_name"], "Gemini 4 Hydrogen")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(agentaus.json()["id"], "agentaus")

    def test_token_counts_are_estimated_locally(self):
        response = self.post({"model": "gemini-4-hydrogen",
                              "messages": [{"role": "user", "content": "hello there"}]},
                             path="/v1/messages/count_tokens")
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()["input_tokens"], 0)
        self.assertEqual(STATE["seen"], [])

    def test_routing_is_by_prefix_and_force_all_still_wins(self):
        from agentaus_bridge.config import settings
        self.assertTrue(settings.routes_to_gemini("gemini-4-neon"))
        self.assertFalse(settings.routes_to_gemini("claude-opus-5-5"))
        self.assertFalse(settings.routes_to_gemini("agentaus"))
        saved = settings.force_all_to_agentaus
        settings.force_all_to_agentaus = True
        try:
            self.assertFalse(settings.routes_to_gemini("gemini-4-neon"))
        finally:
            settings.force_all_to_agentaus = saved


if __name__ == "__main__":
    unittest.main()
