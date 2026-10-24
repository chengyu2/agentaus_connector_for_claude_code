"""The bridge under a struggling upstream: the fixes from the d10-d14 robustness audit.

Each test is a failure measured live:

- Time-up turns ("report now, no tools") drew 524s 20-50% of the time, were replayed five times
  and copied ten more by Claude Code's buffered fallback, and every 524 dropped the throttle for
  every agent. A gateway failure on one is now answered by the bridge from what the agent has.
- Agents ran 68-81 min past their limit; one request was retried for 2,627 s.
- Claude Code cut streams carrying only pings at 600 s and re-sent them buffered, ~10 times,
  and the bridge never noticed a buffered client had gone.
- After the graph, a main-loop Edit used 8 spaces where the file had 4 (d12, 0.5 not 1.0), and
  another put `parse_args()` at module level (d13, 0.167 -> 0).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agentaus_bridge import (augment, flight, graph, import_guard, ledger, repair,  # noqa: E402
                             server, throttle)
from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge.translate import AnthropicStreamBuilder  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def tool(name):
    return {"name": name, "description": "", "input_schema": {"type": "object", "properties": {}}}


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


SUBAGENT_SYSTEM = [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.300; "
                    "cc_is_subagent=true;\nYou are a subagent spawned by a workflow orchestration "
                    "script."}]


def events_of(raw: str) -> list:
    out = []
    for frame in raw.split("\n\n"):
        data = next((l[5:].strip() for l in frame.split("\n") if l.startswith("data:")), None)
        if data:
            out.append(json.loads(data))
    return out


# --- A stub Agentaus -----------------------------------------------------------------------

class Stub:
    """An Agentaus stand-in on a free port. `behave(payload) -> (status, delay_s, text)`."""

    def __init__(self, behave):
        self.calls: list = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)))
                stub.calls.append(payload)
                status, delay, text = behave(payload)
                time.sleep(delay)
                if status != 200:
                    self.send_response(status)
                    self.send_header("content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"error code: %d" % status)
                    return
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    chunk = {"choices": [{"index": 0, "delta": {"content": text},
                                          "finish_reason": "stop"}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
                else:
                    body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {
                        "role": "assistant", "content": text}}]}).encode()
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *a):
                pass

        ThreadingHTTPServer.allow_reuse_address = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _WithStub(unittest.TestCase):
    """Settings that keep a turn to one plain upstream call, and a fresh throttle."""

    def setUp(self):
        self.saved = {k: getattr(settings, k) for k in (
            "agentaus_base_url", "agentaus_api_key", "retry_backoff_seconds", "max_retries",
            "agentaus_thinking", "agentaus_self_review", "agentaus_grounding_check",
            "heartbeat_seconds", "nonstream_deadline_seconds", "flight_grace_seconds",
            "agentaus_upstream_concurrency", "agentaus_syntax_check")}
        settings.agentaus_api_key = "test-key"
        settings.retry_backoff_seconds = 0.01
        settings.max_retries = 4
        settings.agentaus_thinking = False
        settings.agentaus_self_review = False
        settings.agentaus_grounding_check = False
        settings.agentaus_syntax_check = False
        settings.agentaus_upstream_concurrency = 10
        throttle.reset()
        graph._FIRST_SEEN.clear()
        self.addCleanup(throttle.reset)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(settings, k, v)

    def stub(self, behave) -> Stub:
        stub = Stub(behave)
        self.addCleanup(stub.close)
        settings.agentaus_base_url = stub.url
        return stub


# --- 1. Time-up turns ----------------------------------------------------------------------

class TimeUpTurns(_WithStub):
    PROMPT = f"{graph.MARK}\n  # Solve subtask 3\n  ## Your skill: solve\n  Fix count_top()."

    def agent(self, tag: str):
        prompt = self.PROMPT + f" ({tag})"
        first = {"model": "agentaus", "max_tokens": 200, "system": SUBAGENT_SYSTEM,
                 "tools": [tool("Bash"), tool("Read")], "messages": [user(prompt)]}
        later = {**first, "messages": [
            user(prompt),
            {"role": "assistant", "content": [
                {"type": "text", "text": "count_top was off by one: the slice dropped the last row."},
                {"type": "tool_use", "id": "b1", "name": "Bash",
                 "input": {"command": "python3 -c 'import solutions; print(solutions.count_top())'"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b1", "content": "7"}]}]}
        return first, later

    def backdate(self, body, seconds_ago: float):
        graph.overdue(body)                       # starts the clock, as the first request does
        key = graph.hashlib.sha256(graph._agent_key(body)[0].encode()).hexdigest()
        graph._FIRST_SEEN[key][0] = time.monotonic() - seconds_ago

    def test_a_524_on_a_time_up_turn_is_answered_from_what_the_agent_has(self):
        # Agentaus 524s the no-tools report; everything else succeeds.
        stub = self.stub(lambda p: (524, 0, "") if not p.get("tools") else (200, 0, "ok"))
        for stream in (True, False):
            stub.calls.clear()
            throttle.reset()
            first, later = self.agent(f"stream={stream}")
            self.backdate(first, settings.agentaus_graph_agent_max_s + 30)
            with TestClient(server.app) as client:
                r = client.post("/v1/messages", json={**later, "stream": stream})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(len(stub.calls), 1, "the doomed time-up prompt was replayed")
            self.assertNotIn("tools", stub.calls[0])
            text = ("".join(e["delta"].get("text", "") for e in events_of(r.text)
                            if e["type"] == "content_block_delta") if stream
                    else r.json()["content"][0]["text"])
            self.assertIn(graph.TIME_UP_PARTIAL, text)
            self.assertIn("## Answer", text)
            self.assertIn("the slice dropped the last row", text)
            self.assertIn("7", text)
            gate = throttle.limit()
            self.assertEqual(gate.limit, gate.ceiling, "a time-up 524 narrowed the throttle")
            self.assertEqual(throttle.health().snapshot(gate.limit, gate.ceiling)["fail_10m"], 0)

    def test_an_agent_far_past_its_limit_gets_out_of_time_without_an_upstream_call(self):
        stub = self.stub(lambda p: (200, 0, "should not be asked"))
        first, later = self.agent("give up")
        self.backdate(first, settings.agentaus_graph_agent_max_s + settings.agentaus_graph_give_up_s + 1)
        with TestClient(server.app) as client:
            r = client.post("/v1/messages", json={**later, "stream": False})
        self.assertEqual(stub.calls, [])
        self.assertEqual(r.json()["content"], [{"type": "text", "text": "(out of time)"}])
        self.assertEqual(r.json()["stop_reason"], "end_turn")
        self.assertLess(len(graph.OUT_OF_TIME), 20, "the graph reads a reply this short as none")

    def test_retries_stop_once_the_agent_runs_past_its_limit(self):
        # One request was retried for 2,627 s (d13). An ordinary turn that crosses its limit
        # while being retried is answered by the bridge at its next gateway failure.
        stub = self.stub(lambda p: (524, 0.15, ""))
        first, later = self.agent("crosses")
        self.backdate(first, settings.agentaus_graph_agent_max_s - 0.1)
        with TestClient(server.app) as client:
            r = client.post("/v1/messages", json={**later, "stream": True})
        self.assertEqual(len(stub.calls), 1)
        self.assertIn("tools", stub.calls[0], "it was an ordinary turn when it was sent")
        self.assertIn(graph.TIME_UP_PARTIAL, r.text)

    def test_the_fallback_keeps_the_roles_heading_and_clips(self):
        _, later = self.agent("text")
        later["messages"][2]["content"][0]["content"] = "x" * 9000
        text = graph.time_up_fallback(later)
        self.assertTrue(text.startswith(graph.TIME_UP_PARTIAL))
        self.assertIn("`Bash: python3 -c", text)
        self.assertLess(len(text), 3000)
        check = {**later, "messages": [user(f"{graph.MARK}\n  ## Your skill: check\n  Check s1.")]}
        self.assertIn("## Evidence", graph.time_up_fallback(check))

    def test_agent_time_leaves_out_time_the_upstream_was_down(self):
        first, later = self.agent("outage")
        with mock.patch.object(throttle, "down_total", return_value=100.0):
            self.backdate(first, settings.agentaus_graph_agent_max_s + 50)
        with mock.patch.object(throttle, "down_total", return_value=100.0):
            self.assertGreater(graph.seconds_over(later), 40)
        # 300 s of outage since the agent started: it is not past its limit after all.
        with mock.patch.object(throttle, "down_total", return_value=400.0):
            self.assertEqual(graph.seconds_over(later), 0.0)
            self.assertEqual(graph.overdue(later)[0], 0.0)


# --- 2. Heartbeat --------------------------------------------------------------------------

class Heartbeat(_WithStub):
    def test_a_silent_wait_sends_thinking_deltas_in_index_order(self):
        builder = AnthropicStreamBuilder("agentaus")

        async def go():
            out = [builder.start()]
            box: dict = {}
            async for beat in server._waiting(asyncio.sleep(0.35, result="answer"), builder,
                                              box, every=0.1):
                out.append(beat)
            out.append(builder.text(box["value"]))
            out.append(builder.finish("stop", None))
            return b"".join(out).decode()

        events = events_of(run(go()))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "message_start")
        beats = [e for e in events if e["type"] == "content_block_delta"
                 and e["delta"]["type"] == "thinking_delta"]
        self.assertGreaterEqual(len(beats), 3)
        self.assertTrue(all(e["index"] == 0 for e in beats))
        starts = [(e["index"], e["content_block"]["type"]) for e in events
                  if e["type"] == "content_block_start"]
        self.assertEqual(starts, [(0, "thinking"), (1, "text")])
        stop0 = kinds.index("content_block_stop")
        self.assertLess(stop0, kinds.index("content_block_start", 2))
        self.assertEqual(kinds[-1], "message_stop")

    def test_no_heartbeat_inside_a_text_block_the_client_is_reading(self):
        builder = AnthropicStreamBuilder("agentaus")
        builder.start()
        builder.text("half an answer")
        self.assertEqual(server._heartbeat(builder, 60), b"")

    def test_a_turn_waiting_on_the_upstream_streams_heartbeats(self):
        self.stub(lambda p: (200, 0.45, "hello after a long wait"))
        settings.heartbeat_seconds = 0.1
        with TestClient(server.app) as client:
            r = client.post("/v1/messages", json={"model": "agentaus", "max_tokens": 50,
                                                  "stream": True, "messages": [user("hi")]})
        events = events_of(r.text)
        thinking = [e for e in events if e["type"] == "content_block_delta"
                    and e["delta"]["type"] == "thinking_delta"]
        self.assertGreaterEqual(len(thinking), 2, r.text[:800])
        self.assertIn("Still waiting for Agentaus", thinking[0]["delta"]["thinking"])
        text = "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta")
        self.assertEqual(text, "hello after a long wait")


# --- 3. Buffered clients and flights -------------------------------------------------------

class Flights(_WithStub):
    BODY = {"model": "agentaus", "max_tokens": 50, "messages": [user("what is 6 x 7?")]}

    def asgi(self):
        """An httpx client on the app, without lifespan: the app's own clients set up here."""
        server.app.state.client = httpx.AsyncClient(
            timeout=30, transport=throttle.LimitedTransport(httpx.AsyncHTTPTransport()))
        server.app.state.anthropic_client = server.app.state.client
        flight.reset()
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                 base_url="http://bridge", timeout=30)

    def test_identical_buffered_requests_share_one_upstream_turn(self):
        stub = self.stub(lambda p: (200, 0.4, "42"))

        async def go():
            async with self.asgi() as c:
                a, b = await asyncio.gather(
                    c.post("/v1/messages", json={**self.BODY, "stream": False}),
                    c.post("/v1/messages", json={**self.BODY, "stream": False}))
                await server.app.state.client.aclose()
                return a, b
        a, b = run(go())
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertEqual(a.json()["content"][0]["text"], "42")
        self.assertEqual(len(stub.calls), 1)

    def test_a_buffered_fallback_joins_the_stream_it_replaces(self):
        stub = self.stub(lambda p: (200, 0.5, "42"))

        async def go():
            async with self.asgi() as c:
                streamed = asyncio.ensure_future(
                    c.post("/v1/messages", json={**self.BODY, "stream": True}))
                await asyncio.sleep(0.15)
                buffered = await c.post("/v1/messages", json={**self.BODY, "stream": False})
                streamed = await streamed
                await server.app.state.client.aclose()
                return streamed, buffered
        streamed, buffered = run(go())
        self.assertEqual(len(stub.calls), 1, "the fallback started a second upstream turn")
        self.assertEqual(buffered.status_code, 200, buffered.text)
        message = buffered.json()
        self.assertEqual(message["content"], [{"type": "text", "text": "42"}],
                         "thinking blocks are display, not part of a buffered answer")
        self.assertIn("42", streamed.text)

    def test_a_buffered_client_is_told_to_retry_at_the_deadline_and_the_retry_joins(self):
        # The turn takes 0.4 s: the first client gives up at 0.25 s, the retry joins and gets it.
        stub = self.stub(lambda p: (200, 0.4, "42"))
        settings.nonstream_deadline_seconds = 0.25

        async def go():
            async with self.asgi() as c:
                first = await c.post("/v1/messages", json={**self.BODY, "stream": False})
                second = await c.post("/v1/messages", json={**self.BODY, "stream": False})
                await server.app.state.client.aclose()
                return first, second
        first, second = run(go())
        self.assertEqual(first.status_code, 504)
        self.assertIn("still running", first.json()["error"]["message"])
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(stub.calls), 1)

    def test_a_turn_with_no_client_is_cancelled_after_the_grace_with_no_new_request(self):
        settings.flight_grace_seconds = 0.2
        ran = {"cancelled": False}

        async def forever():
            try:
                yield b"event: message_start\ndata: {}\n\n"
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                ran["cancelled"] = True
                raise

        async def go():
            flight.reset()
            f = flight.start_stream("k", forever())
            reader = asyncio.ensure_future(flight.subscribe(f).__anext__())
            await asyncio.sleep(0.05)
            reader.cancel()                    # the client hangs up
            await asyncio.sleep(0.05)
            self.assertIs(flight.find("k"), f, "kept for a retry during the grace")
            await asyncio.sleep(0.4)
            return f
        f = run(go())
        self.assertTrue(ran["cancelled"])
        self.assertTrue(f.done)
        self.assertIsNone(flight.find("k"))

    def test_a_buffered_client_that_disconnects_stops_waiting(self):
        settings.flight_grace_seconds = 0.2

        class Gone:
            headers = {}

            async def is_disconnected(self):
                return True

        async def slow():
            await asyncio.sleep(3600)

        async def go():
            flight.reset()
            started = time.monotonic()
            r = await server._answer_buffered(Gone(), "k2", slow, "agentaus")
            waited = time.monotonic() - started
            f = flight._flights.get("k2")
            await asyncio.sleep(0.4)
            return r, waited, f
        r, waited, f = run(go())
        self.assertEqual(r.status_code, 504)
        self.assertLess(waited, 3)
        self.assertTrue(f.done, "the abandoned turn was not cancelled")

    def test_the_flight_key_ignores_stream_and_max_tokens_but_not_the_agent(self):
        body = {"model": "agentaus", "max_tokens": 50, "stream": True, "messages": [user("x")]}
        same = {**body, "stream": False, "max_tokens": 32000}
        self.assertEqual(flight.key_of(body, {}), flight.key_of(same, {}))
        self.assertNotEqual(flight.key_of(body, {"x-claude-code-agent-id": "a1"}),
                            flight.key_of(body, {"x-claude-code-agent-id": "a2"}))

    def test_a_finished_turn_every_client_saw_is_not_served_again(self):
        stub = self.stub(lambda p: (200, 0, "42"))
        with TestClient(server.app) as client:
            client.post("/v1/messages", json={**self.BODY, "stream": True})
            client.post("/v1/messages", json={**self.BODY, "stream": True})
        self.assertEqual(len(stub.calls), 2)


# --- 4. Always streamed upstream -----------------------------------------------------------

class AlwaysStreamed(_WithStub):
    def test_a_buffered_client_turn_is_streamed_upstream_and_assembled(self):
        stub = self.stub(lambda p: (200, 0, "hello"))
        with TestClient(server.app) as client:
            r = client.post("/v1/messages", json={"model": "agentaus", "max_tokens": 50,
                                                  "stream": False, "messages": [user("hi")]})
        self.assertEqual(r.json()["content"][0]["text"], "hello")
        self.assertTrue(stub.calls[0]["stream"])

    def test_streamed_tool_calls_are_assembled_into_the_buffered_shape(self):
        sse = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "Read", "arguments": '{"file_'}}]}}]})
            + "\n\ndata: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'path": "/a"}'}}]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 3}}) + "\n\ndata: [DONE]\n\n")

        async def go():
            transport = httpx.MockTransport(lambda r: httpx.Response(
                200, text=sse, headers={"content-type": "text/event-stream"}))
            async with httpx.AsyncClient(transport=transport) as c:
                return await server._post_with_retry(c, "http://x", json_body={}, headers={},
                                                      stream_upstream=True)
        data = run(go()).json()
        message = data["choices"][0]["message"]
        self.assertEqual(message["tool_calls"][0]["function"],
                         {"name": "Read", "arguments": '{"file_path": "/a"}'})
        self.assertEqual(data["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(data["usage"], {"prompt_tokens": 3})

    def test_helpers_stream_by_default(self):
        self.assertTrue(settings.agentaus_stream_helpers)
        self.assertGreater(settings.agentaus_stream_idle_seconds, 0)


# --- 5. Health -----------------------------------------------------------------------------

class HealthEndpoint(_WithStub):
    def test_healthz_upstream_has_the_contract_fields(self):
        with TestClient(server.app) as client:
            data = client.get("/healthz/upstream").json()
        for field, kind in (("ok_10m", int), ("fail_10m", int), ("n524_10m", int),
                            ("ok_rate_10m", float), ("p50_s_10m", float), ("limit", int),
                            ("ceiling", int), ("down", bool), ("down_total_s", float),
                            ("source_hash", str)):
            self.assertIsInstance(data[field], kind, field)
        self.assertIn("down_since", data)
        self.assertEqual(len(data["source_hash"]), 16)

    def test_queue_time_is_not_called_an_unresponsive_upstream(self):
        settings_saved = settings.stall_warning_seconds
        settings.stall_warning_seconds = 0.05
        self.addCleanup(setattr, settings, "stall_warning_seconds", settings_saved)
        gate = throttle.AdaptiveLimit(1, 0)

        async def go():
            await gate.acquire()               # the only slot is taken
            transport = throttle.LimitedTransport(
                httpx.MockTransport(lambda r: httpx.Response(200, json={})), adaptive=lambda: gate)
            async with httpx.AsyncClient(transport=transport) as c:
                call = asyncio.ensure_future(server._post_with_retry(
                    c, "http://x", json_body={}, headers={}))
                await asyncio.sleep(0.2)
                await gate.release(True)
                await call

        with self.assertLogs("agentaus-bridge", level="WARNING") as logs:
            run(go())
        text = "\n".join(logs.output)
        self.assertIn("waiting for an upstream slot", text)
        self.assertNotIn("upstream has not responded", text)


# --- 6. The graph's clock ------------------------------------------------------------------

class GraphArgs(unittest.TestCase):
    def test_the_graph_gets_a_hard_end_less_the_sessions_time_so_far(self):
        call = {"name": "Workflow", "arguments": json.dumps({"script": "x"})}
        with mock.patch.object(graph, "prepare", return_value={}), \
                mock.patch.object(settings, "agentaus_graph_kind", "dynamic"):
            fresh = json.loads(graph.expand(call, "do it", "/tmp/x")["arguments"])["args"]
            later = json.loads(graph.expand(call, "do it", "/tmp/x", time.time() - 600)["arguments"])["args"]
        self.assertEqual(fresh["total_ms"], settings.agentaus_graph_total_s * 1000)
        self.assertAlmostEqual(later["total_ms"], (settings.agentaus_graph_total_s - 600) * 1000, delta=2000)
        self.assertEqual(fresh["writer_ms"], (settings.agentaus_graph_writer_max_s + 120) * 1000)
        self.assertEqual(settings.agentaus_graph_total_s, 5400)


# --- 7. Edits after the graph --------------------------------------------------------------

FILE = ("def parse(profile):\n"
        "    order = {}\n"
        "    # Require at least a top\u2011five report.\n"
        "    if depth < 6:\n"
        "        return None\n"
        "    return order\n")


class EditRepair(unittest.TestCase):
    def test_eight_spaces_where_the_file_has_four(self):
        # d12 c1913h: graded 1.0 with this one line fixed, 0.5 as shipped.
        fixed = repair.repair_edit(FILE, "        if depth < 6:", "        if depth < 5:")
        self.assertEqual(fixed[:2], ("    if depth < 6:", "    if depth < 5:"))

    def test_a_block_keeps_its_relative_indentation(self):
        fixed = repair.repair_edit(FILE, "if depth < 6:\n    return None",
                                   "if depth < 5:\n    return []")
        self.assertEqual(fixed[:2], ("    if depth < 6:\n        return None",
                                     "    if depth < 5:\n        return []"))

    def test_a_typed_hyphen_and_a_pasted_read_prefix(self):
        fixed = repair.repair_edit(FILE, "    # Require at least a top-five report.",
                                   "    # Require a top-five report.")
        self.assertEqual(fixed[0], "    # Require at least a top\u2011five report.")
        fixed = repair.repair_edit(FILE, "     4\t    if depth < 6:", "     4\t    if depth < 5:")
        self.assertEqual(fixed[:2], ("    if depth < 6:", "    if depth < 5:"))

    def test_no_repair_when_it_is_not_one_place_or_mixes_tabs(self):
        twice = "a:\n    x = 1\nb:\n    x = 1\n"
        self.assertIsNone(repair.repair_edit(twice, "  x = 1", "  x = 2"))
        tabs = "def g(x):\n\tif x < 6:\n\t\treturn None\n"
        self.assertIsNone(repair.repair_edit(tabs, "    if x < 6:", "    if x < 5:"))
        self.assertIsNone(repair.repair_edit(FILE, "    if depth < 6:", "    if depth < 5:"),
                          "an exact match needs no repair")

    def test_tabs_typed_where_the_file_has_four_spaces(self):
        # d15 he10: the post-graph Edit of order_by_points came back with tab indentation.
        src = "def f(xs):\n    # pair them\n    out = sorted(xs)\n    return out\n"
        fixed = repair.repair_edit(src, "\t# pair them\n\tout = sorted(xs)", "\tout = sorted(xs, key=abs)")
        self.assertEqual(fixed[:2], ("    # pair them\n    out = sorted(xs)", "    out = sorted(xs, key=abs)"))

    def test_a_word_slipped_in_a_retyped_block_is_taken_from_the_file(self):
        # d15 he10: 8 Edits retyped "based on their index" as "based on the index"; all refused.
        src = ('def order(nums):\n    """\n    Sort by digit sum.\n    Ties: order them based on their index in original list.\n'
               '    """\n    return sorted(nums, key=abs)\n')
        old = ('def order(nums):\n    """\n    Sort by digit sum.\n    Ties: order them based on the index in original list.\n'
               '    """\n    return sorted(nums, key=abs)')
        new = old.replace("key=abs", "key=digits")
        fixed = repair.repair_edit(src, old, new)
        self.assertEqual(src.count(fixed[0]), 1)
        self.assertIn("based on their index", fixed[1], "the docstring keeps the file's words")
        self.assertIn("key=digits", fixed[1])

    def test_a_near_match_in_two_places_or_too_different_is_not_used(self):
        two = "def a():\n    x = 1\n    y = 2\n    z = 3\n\ndef b():\n    x = 1\n    y = 2\n    z = 3\n"
        self.assertIsNone(repair.repair_edit(two, "    x = 1\n    y = 9\n    z = 3", "    x = 1"))
        src = "def a():\n    x = 1\n    y = 2\n    z = 3\n"
        self.assertIsNone(repair.repair_edit(src, "    x = 1\n    w = q + r\n    z = 3", "    x = 1"))
        self.assertIsNone(repair.repair_edit(src, "    x = 1\n    y = 9", "    x = 1"), "under three lines")

    def test_already_applied_only_for_a_long_new_string(self):
        short = "def a():\n    return None\ndef b(x):\n    if x:\n        return 1\n"
        self.assertIsNone(repair.repair_edit(short, "    if x:\n        return 2", "    return None"))
        done = FILE.replace("depth < 6", "depth < 5")
        applied = repair.repair_edit(done, "    if depth < 6:\n        return None",
                                     "    if depth < 5:\n        return None")
        self.assertEqual(applied[:2], (repair.APPLIED, 4))

    def test_through_validation_sequential_edits_and_replace_all(self):
        with tempfile.TemporaryDirectory() as cwd:
            path = os.path.join(cwd, "bank.py")
            with open(path, "w") as fh:
                fh.write(FILE)
            schema = {"type": "object", "properties": {k: {"type": "string"} for k in
                                                         ("file_path", "old_string", "new_string")}}
            first = {"id": "1", "name": "Edit", "arguments": json.dumps({
                "file_path": path, "old_string": "        if depth < 6:", "new_string": "        if depth < 5:"})}
            # The second relies on the first: on disk it is "< 6", after the first "< 5".
            second = {"id": "2", "name": "Edit", "arguments": json.dumps({
                "file_path": path, "old_string": "        if depth < 5:\n            return None",
                "new_string": "        if depth < 5:\n            return []"})}
            every = {"id": "3", "name": "Edit", "arguments": json.dumps({
                "file_path": path, "old_string": "  order", "new_string": "  ordering",
                "replace_all": True})}
            broken = server._validate_tool_calls([first, second, every], {"Edit": schema}, cwd)
            self.assertEqual(broken, [])
            self.assertEqual(json.loads(first["arguments"])["old_string"], "    if depth < 6:")
            self.assertEqual(json.loads(second["arguments"])["new_string"],
                             "    if depth < 5:\n        return []")
            self.assertEqual(json.loads(every["arguments"])["old_string"], "  order", "replace_all is left alone")
            again = {"id": "4", "name": "Edit", "arguments": json.dumps({
                "file_path": path, "old_string": "    if depth < 7:\n        return None",
                "new_string": "    if depth < 6:\n        return None"})}
            [(call, message)] = server._validate_tool_calls([again], {"Edit": schema}, cwd)
            self.assertIn("already in", message)

    def test_a_refused_edit_gets_the_closest_lines_with_their_indentation(self):
        with tempfile.TemporaryDirectory() as cwd:
            path = os.path.join(cwd, "bank.py")
            with open(path, "w") as fh:
                fh.write(FILE)
            body = {"messages": [
                user("fix it"),
                {"role": "assistant", "content": [{"type": "tool_use", "id": "e1", "name": "Edit", "input": {
                    "file_path": path, "old_string": "\tif depth <= 6:", "new_string": "\tif depth < 5:"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "e1", "is_error": True,
                                              "content": "<tool_use_error>String to replace not found in file.\n"
                                                         "String: \tif depth <= 6:</tool_use_error>"}]}]}
            hinted, n = repair.edit_hints(body)
        self.assertEqual(n, 1)
        note = hinted["messages"][-1]["content"][0]["content"][-1]["text"]
        self.assertIn("4:     if depth < 6:    (4 spaces)", note)


ARGPARSE_MODULE = '''import argparse

BANK = {"a": 1}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    print(BANK if args.json else len(BANK))


if __name__ == "__main__":
    main()
'''

# What the d13 c1913h main loop left: argument parsing at module level.
BROKEN_MODULE = '''import argparse

BANK = {"a": 1}

parser = argparse.ArgumentParser()
parser.add_argument("--json", action="store_true")
args = parser.parse_args()


def main():
    print(BANK if args.json else len(BANK))


if __name__ == "__main__":
    main()
'''


class ImportGuard(unittest.TestCase):
    def setUp(self):
        if not shutil.which("python3"):
            self.skipTest("python3 is not installed")
        import_guard.forget()
        self.addCleanup(import_guard.forget)
        self.cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cwd, True)
        os.makedirs(os.path.join(self.cwd, "tools"))
        open(os.path.join(self.cwd, "tools", "__init__.py"), "w").close()
        self.path = os.path.join(self.cwd, "tools", "bank.py")

    def edit_round(self, before: str, after: str) -> dict:
        with open(self.path, "w") as fh:
            fh.write(before)
        call = {"id": "e1", "name": "Write", "arguments": json.dumps({"file_path": self.path, "content": after})}
        self.assertEqual(import_guard.remember([call], self.cwd), 1)
        with open(self.path, "w") as fh:           # Claude Code applies the call
            fh.write(after)
        body = {"messages": [user("ultracode: add --json"),
                             {"role": "assistant", "content": [{"type": "tool_use", "id": "e1", "name": "Write",
                                                                "input": {"file_path": self.path}}]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "e1",
                                                           "content": "File written"}]}]}
        checked, _ = import_guard.check(body)
        return checked

    def test_module_level_parse_args_after_the_graph_is_reported(self):
        checked = self.edit_round(ARGPARSE_MODULE, BROKEN_MODULE)
        result = checked["messages"][-1]["content"][0]["content"]
        self.assertIsInstance(result, list)
        self.assertIn("imported cleanly before this edit and now fails", result[-1]["text"])
        self.assertIn("if __name__ == '__main__':", result[-1]["text"])

    def test_a_module_that_never_imported_is_not_reported(self):
        never = "import not_a_real_module_xyz\n" + ARGPARSE_MODULE
        checked = self.edit_round(never, never + "\nX = 1\n")
        self.assertEqual(checked["messages"][-1]["content"][0]["content"], "File written")

    def test_a_good_edit_is_not_reported(self):
        checked = self.edit_round(ARGPARSE_MODULE, ARGPARSE_MODULE.replace('{"a": 1}', '{"a": 2}'))
        self.assertEqual(checked["messages"][-1]["content"][0]["content"], "File written")

    def test_a_relative_import_inside_a_package_is_imported_as_a_package(self):
        with open(os.path.join(self.cwd, "tools", "common.py"), "w") as fh:
            fh.write("X = 1\n")
        good = "from .common import X\n" + ARGPARSE_MODULE
        checked = self.edit_round(good, good.replace("from .common import X\n", "from .common import X\nimport sys\n"))
        self.assertEqual(checked["messages"][-1]["content"][0]["content"], "File written")
        checked = self.edit_round(good, good.replace("X = 1", "X = 1") + "\nraise SystemExit(2)\n")
        self.assertIn("now fails", checked["messages"][-1]["content"][0]["content"][-1]["text"])


# --- 8. The session record and the main loop's instructions --------------------------------

class SessionRecord(unittest.TestCase):
    def test_the_graph_session_survives_a_bridge_restart(self):
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd, True)
        scratch = graph.scratch_of(cwd)
        os.makedirs(scratch, exist_ok=True)
        self.addCleanup(shutil.rmtree, scratch, True)
        self.addCleanup(graph.forget_runs)
        body = {"metadata": {"user_id": "u_session_9"}, "system": f"Primary working directory: {cwd}",
                "messages": [user("ultracode: fix it")]}
        graph.remember_run(body, "ultracode: fix it", "ultracode: fix it")
        launched = {**body, "messages": body["messages"] + [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "wf", "name": "Workflow", "input": {
                "script": graph.DYNAMIC_SCRIPT, "args": {"task": "ultracode: fix it"}}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "wf",
                                          "content": "Workflow launched in background. Task ID: w9"}]},
            user("<task-notification><task-id>w9</task-id><status>completed</status></task-notification>")]}
        self.assertEqual(graph.run_state(graph.note_run(launched)), "reported")
        self.assertTrue(os.path.isfile(os.path.join(scratch, graph.SESSION_FILE)))
        graph._RUNS.clear()                       # the bridge restarts
        compacted = {**body, "messages": [user("Read solutions.py and fix line 69")]}
        self.assertEqual(graph.run_state(graph.note_run(compacted)), "reported")

    def test_the_post_graph_steps_name_the_commands_and_not_open_issues(self):
        self.assertNotIn("open_issues", augment.GRAPH_GUIDANCE)
        self.assertIn("run them again after your last edit", " ".join(augment.GRAPH_GUIDANCE.split()))

    def test_the_evidence_shows_what_an_edit_wrote(self):
        messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "e1", "name": "Edit", "input": {
            "file_path": "/p/bank.py", "old_string": "x", "new_string": "if depth < 5:\n" + "y" * 3000}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "e1", "content": "updated"}]}]
        [(name, digest, outcome, output)] = ledger.collect_with_output(messages)
        self.assertIn("if depth < 5:", output)
        self.assertIn("result: updated", output)
        self.assertLess(len(output), 1600)


if __name__ == "__main__":
    unittest.main()


class RetryForever(unittest.TestCase):
    def test_retries_are_unbounded_by_default_and_back_off_to_a_cap(self):
        from agentaus_bridge.config import Settings
        from agentaus_bridge.server import _retry_delay
        s = Settings()
        self.assertGreater(s.max_retries, 1000)
        self.assertGreaterEqual(s.retry_max_delay_seconds, 300)
        from agentaus_bridge.config import settings
        saved = (settings.retry_backoff_seconds, settings.retry_max_delay_seconds)
        try:
            settings.retry_backoff_seconds, settings.retry_max_delay_seconds = 1.0, 300.0
            self.assertLess(_retry_delay(0), 3)
            self.assertGreaterEqual(_retry_delay(50), 300)
            self.assertLessEqual(_retry_delay(50), 301)
        finally:
            settings.retry_backoff_seconds, settings.retry_max_delay_seconds = saved

    def test_time_spent_retrying_is_not_served_time(self):
        import asyncio
        from agentaus_bridge import flight

        async def go():
            f = flight.Flight("k", "stream")
            flight.CURRENT.set(f)
            f.started -= 1000
            flight.note_wait(900)
            return f.served()
        self.assertLess(run_coro(go()) if False else asyncio.run(go()), 150)
