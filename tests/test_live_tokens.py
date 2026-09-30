"""Tokens reach the client as Agentaus produces them.

Measured live: Agentaus streams a reply in ~13-token pieces every ~100ms. The bridge held
all of it back until its checks had run, because an answer already on screen cannot be
revised - so the client's token counter sat still for the whole generation and the answer
then landed at once. The fix streams each piece into a thinking block as it arrives and
still sends the checked answer as the text. These tests pin both halves.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from agentaus_bridge import server  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge.translate import AnthropicStreamBuilder  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def events(raw: bytes) -> list[dict]:
    return [json.loads(line[5:]) for line in raw.decode().splitlines()
            if line.startswith("data:")]


class TheLiveBlock(unittest.TestCase):
    def test_it_opens_once_streams_and_is_signed_when_the_answer_follows(self):
        b = AnthropicStreamBuilder("agentaus", chunk_chars=0)
        out = b.start() + b.live_thinking("Hel") + b.live_thinking("lo") + b.text("Hello")
        out += b.finish("stop", None)
        ev = events(out)
        kinds = [e["type"] for e in ev]
        self.assertEqual(kinds.count("content_block_start"), 2)
        self.assertEqual(ev[1]["content_block"]["type"], "thinking")
        deltas = [e["delta"] for e in ev if e["type"] == "content_block_delta"]
        self.assertEqual([d["thinking"] for d in deltas if d["type"] == "thinking_delta"],
                         ["Hel", "lo"])
        self.assertTrue(any(d["type"] == "signature_delta" for d in deltas),
                        "a thinking block without a signature may not be rendered")
        text_start = next(e for e in ev if e["type"] == "content_block_start"
                          and e["content_block"]["type"] == "text")
        self.assertEqual(text_start["index"], 1, "the answer must follow the draft")
        stops = [e["index"] for e in ev if e["type"] == "content_block_stop"]
        self.assertEqual(stops, [0, 1], "every block is closed, in order")

    def test_a_tool_call_also_closes_the_draft(self):
        b = AnthropicStreamBuilder("agentaus", chunk_chars=0)
        ev = events(b.live_thinking("reading the file") + b.tool_use("t1", "Read", {"file_path": "/x"}))
        self.assertEqual([e["type"] for e in ev].count("content_block_stop"), 2)


# A slow upstream: five pieces, 0.25s apart, like Agentaus' real cadence but slower.
PORT = 9953
PIECES = ["The ", "answer ", "is ", "forty ", "two."]
GAP = 0.25


class _SlowStream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for i, piece in enumerate(PIECES):
            chunk = {"choices": [{"index": 0, "delta": {"content": piece},
                                  "finish_reason": "stop" if i == len(PIECES) - 1 else None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(GAP)
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):
        pass


class TokensArriveWhileAgentausIsStillWriting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HTTPServer.allow_reuse_address = True
        cls.stub = HTTPServer(("127.0.0.1", PORT), _SlowStream)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        self._saved = (settings.agentaus_base_url, settings.agentaus_self_review,
                       settings.agentaus_live_draft, settings.agentaus_grounding_check,
                       settings.agentaus_syntax_check, settings.agentaus_api_key)
        settings.agentaus_base_url = f"http://127.0.0.1:{PORT}"
        settings.agentaus_api_key = "test-key"
        settings.agentaus_self_review = True          # the answer is held back
        settings.agentaus_grounding_check = False
        settings.agentaus_syntax_check = False

    def tearDown(self):
        (settings.agentaus_base_url, settings.agentaus_self_review,
         settings.agentaus_live_draft, settings.agentaus_grounding_check,
         settings.agentaus_syntax_check, settings.agentaus_api_key) = self._saved

    def _stream(self):
        async def go():
            seen = []
            started = time.monotonic()
            async with httpx.AsyncClient() as client:
                # An agent turn - it offers tools - so the answer is held for checks.
                body = {"model": "agentaus", "messages": [{"role": "user", "content": "q"}],
                        "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
                payload = {"messages": [{"role": "user", "content": "q"}], "stream": True}
                async for chunk in server._agentaus_event_stream(
                        client, payload, body, "agentaus", started):
                    for e in events(chunk):
                        seen.append((time.monotonic() - started, e))
            return seen
        return run(go())

    def test_the_first_token_is_shown_before_the_answer_is_finished(self):
        settings.agentaus_live_draft = True
        seen = self._stream()
        drafted = [(t, e) for t, e in seen if e.get("delta", {}).get("type") == "thinking_delta"]
        self.assertTrue(drafted, "nothing streamed live")
        first = drafted[0][0]
        finished = next(t for t, e in seen if e["type"] == "message_stop")
        self.assertLess(first, finished - 3 * GAP,
                        f"first token at {first:.2f}s, answer done at {finished:.2f}s")
        live_text = "".join(e["delta"]["thinking"] for _, e in drafted)
        for piece in PIECES:
            self.assertIn(piece, live_text)
        answer = "".join(e["delta"]["text"] for _, e in seen
                         if e.get("delta", {}).get("type") == "text_delta")
        self.assertEqual(answer, "".join(PIECES), "the checked answer still arrives as text")

    def test_off_means_the_old_behaviour(self):
        settings.agentaus_live_draft = False
        seen = self._stream()
        self.assertFalse([e for _, e in seen if e.get("delta", {}).get("type") == "thinking_delta"])
        answer = "".join(e["delta"]["text"] for _, e in seen
                         if e.get("delta", {}).get("type") == "text_delta")
        self.assertEqual(answer, "".join(PIECES))


class HelperCallsCarryASystemMessage(unittest.TestCase):
    def test_the_overwrite_has_something_to_overwrite_with(self):
        sent = {}

        class _Client:
            async def post(self, url, json=None, headers=None):
                sent.update(json)
                return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]},
                                      request=httpx.Request("POST", url))

        saved = settings.agentaus_stream_helpers
        settings.agentaus_stream_helpers = False
        try:
            self.assertEqual(run(server._agentaus_summarise(_Client(), "summarise this")), "ok")
        finally:
            settings.agentaus_stream_helpers = saved
        roles = [m["role"] for m in sent["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertTrue(sent["system_prompt_overwrite"])


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------------------
# Claude Code's own utility calls are answered, never rewritten
# --------------------------------------------------------------------------------------

UTILITY_PORT = 9954
UTILITY_CALLS: list = []
VERDICT = '{"state": "done", "reason": "' + "the agent delivered the explanation " * 8 + '"}'


class _Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)))
        UTILITY_CALLS.append(body)
        # Any review of the verdict would find "defects", which is the failure observed.
        text = body["messages"][-1]["content"]
        reply = "VERDICT: DEFECTS\n- rewrite it as an essay" if "Review the ANSWER" in text else VERDICT
        # No usage block: a made-up input count would recalibrate the bridge's global
        # token estimate and leak into every later test.
        data = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": reply}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class UtilityCallsAreNotRewritten(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HTTPServer.allow_reuse_address = True
        cls.stub = HTTPServer(("127.0.0.1", UTILITY_PORT), _Upstream)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        UTILITY_CALLS.clear()
        self._saved = (settings.agentaus_base_url, settings.agentaus_api_key,
                       settings.agentaus_self_review, settings.agentaus_thinking,
                       settings.agentaus_grounding_check)
        settings.agentaus_base_url = f"http://127.0.0.1:{UTILITY_PORT}"
        settings.agentaus_api_key = "test-key"
        settings.agentaus_self_review = True
        settings.agentaus_thinking = False
        settings.agentaus_grounding_check = False

    def tearDown(self):
        (settings.agentaus_base_url, settings.agentaus_api_key,
         settings.agentaus_self_review, settings.agentaus_thinking,
         settings.agentaus_grounding_check) = self._saved

    def _post(self, **extra):
        from fastapi.testclient import TestClient
        body = {"model": "agentaus", "max_tokens": 300, "stream": False,
                "system": "Classify the state of the agent. Reply as JSON.",
                "messages": [{"role": "user", "content": "The tail: here is the answer."}]}
        body.update(extra)
        with TestClient(server.app) as client:
            return client.post("/v1/messages", json=body).json()

    def test_the_turn_state_classifier_gets_the_model_answer_verbatim(self):
        reply = self._post()
        self.assertEqual(reply["content"][0]["text"], VERDICT)
        self.assertEqual(len(UTILITY_CALLS), 1, "a check ran on a utility call")

    def test_an_agent_turn_is_still_reviewed(self):
        self._post(tools=[{"name": "Read", "description": "Read a file",
                           "input_schema": {"type": "object", "properties": {}}}])
        self.assertTrue(any("Review the ANSWER" in c["messages"][-1]["content"]
                            for c in UTILITY_CALLS), "agent turns must keep their review")


# --------------------------------------------------------------------------------------
# The plan streams too, and the response starts before planning does
# --------------------------------------------------------------------------------------

PLAN_PORT = 9955
PLAN_PIECES = ["1. Read ", "the file. ", "2. Fix ", "the bug."]


class _PlanThenAnswer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)))
        planning = "Plan the turn below" in json.dumps(body)
        pieces = PLAN_PIECES if planning else ["Fixed ", "it."]
        if not body.get("stream"):
            data = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "".join(pieces)}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for piece in pieces:
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(GAP)
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):
        pass


class ThePlanStreamsLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HTTPServer.allow_reuse_address = True
        cls.stub = HTTPServer(("127.0.0.1", PLAN_PORT), _PlanThenAnswer)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        names = ("agentaus_base_url", "agentaus_api_key", "agentaus_self_review",
                 "agentaus_thinking", "agentaus_thinking_visible", "agentaus_live_draft",
                 "agentaus_grounding_check", "agentaus_syntax_check", "agentaus_search",
                 "agentaus_web_search", "agentaus_inventory", "agentaus_zoom",
                 "agentaus_investigate")
        self._saved = {n: getattr(settings, n) for n in names}
        settings.agentaus_base_url = f"http://127.0.0.1:{PLAN_PORT}"
        settings.agentaus_api_key = "test-key"
        settings.agentaus_self_review = False
        settings.agentaus_thinking = settings.agentaus_thinking_visible = True
        settings.agentaus_live_draft = True
        for n in ("agentaus_grounding_check", "agentaus_syntax_check", "agentaus_search",
                  "agentaus_web_search", "agentaus_inventory", "agentaus_zoom",
                  "agentaus_investigate"):
            setattr(settings, n, False)

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(settings, n, v)

    def test_plan_tokens_arrive_while_planning_is_still_running(self):
        from starlette.requests import Request

        async def go():
            server.app.state.client = httpx.AsyncClient()
            try:
                request = Request({"type": "http", "app": server.app, "headers": []})
                body = {"model": "agentaus", "stream": True, "max_tokens": 100,
                        "messages": [{"role": "user", "content": "fix the bug"}],
                        "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
                started = time.monotonic()
                response = await server._handle_agentaus(request, body, "agentaus", True)
                seen = []
                async for chunk in response.body_iterator:
                    for e in events(chunk if isinstance(chunk, bytes) else chunk.encode()):
                        seen.append((time.monotonic() - started, e))
                return seen
            finally:
                await server.app.state.client.aclose()

        seen = run(go())
        kinds = [e["type"] for _, e in seen]
        self.assertEqual(kinds.count("message_start"), 1)
        self.assertLess(seen[0][0], GAP, "the response must start before planning finishes")
        thinking = [(t, e["delta"]["thinking"]) for t, e in seen
                    if e.get("delta", {}).get("type") == "thinking_delta"]
        plan_times = [t for t, text in thinking if any(p in text for p in PLAN_PIECES)]
        self.assertTrue(plan_times, "the plan was not streamed")
        planning_done = len(PLAN_PIECES) * GAP
        self.assertLess(plan_times[0], planning_done - GAP,
                        "the first plan piece must arrive before the plan is finished")
        answer = "".join(e["delta"]["text"] for _, e in seen
                         if e.get("delta", {}).get("type") == "text_delta")
        self.assertEqual(answer, "Fixed it.")
