"""Helper passes that no longer sit in front of the user's answer.

The plan is written beside a turn's first call, not before it (AGENTAUS_PLAN_ASYNC).

The first call must not wait for the planner; the plan joins the turn's later requests once
it is written; and a new user message starts a new plan. Also: the syntax gate on tool-loop
answers, and the one-pass grounding reply. See plans.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import stub_server  # noqa: E402
from agentaus_bridge import finish, plans, server, syntax  # noqa: E402
from agentaus_bridge.augment import grounding_revision, with_plan  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402

PLAN_SECONDS = 0.8
CHECK_SECONDS = 0.6
CALLS: list[dict] = []
MAIN_REPLY = ["Done."]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)))
        raw = json.dumps(body)
        planning = "Plan the turn below" in raw
        kind = ("plan" if planning else "classify" if "Its turn ended with this reply" in raw
                else "ground" if "could not possibly know" in raw else "main")
        system = " ".join(m.get("content") or "" for m in body.get("messages") or []
                          if m.get("role") == "system" and isinstance(m.get("content"), str))
        CALLS.append({"at": time.monotonic(), "planning": planning, "system": system,
                      "kind": kind})
        if planning:
            time.sleep(PLAN_SECONDS)
        if kind in ("classify", "ground"):
            time.sleep(CHECK_SECONDS)
        text = {"plan": "1. Read the file. 2. PLAN-MARKER fix it.", "classify": "ANSWER",
                "ground": "GROUNDED"}.get(kind, MAIN_REPLY[0])
        ending = "length" if "TRUNCATE-ME" in raw else "stop"
        if not body.get("stream"):
            data = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": text}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": ending}]}
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):
        pass


TOOLS = [{"name": "Read", "input_schema": {"type": "object", "properties": {}}}]


def first(text="fix the bug in app.py"):
    return {"model": "agentaus", "stream": True, "max_tokens": 100, "tools": TOOLS,
            "messages": [{"role": "user", "content": text}]}


def after_tool(text="fix the bug in app.py"):
    body = first(text)
    body["messages"] += [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                           "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "def f(): pass"}]},
    ]
    return body


class TheFirstCallDoesNotWaitForThePlan(unittest.TestCase):
    NAMES = ("agentaus_base_url", "agentaus_api_key", "agentaus_self_review",
             "agentaus_thinking", "agentaus_thinking_visible", "agentaus_live_draft",
             "agentaus_grounding_check", "agentaus_syntax_check", "agentaus_search",
             "agentaus_web_search", "agentaus_inventory", "agentaus_zoom",
             "agentaus_investigate", "agentaus_plan_async", "agentaus_correction_rounds",
             "agentaus_overlap_checks", "agentaus_grounding_one_pass",
             "agentaus_plan_wait_seconds", "agentaus_finish_gate")

    @classmethod
    def setUpClass(cls):
        cls.stub = stub_server.start(_Upstream, ThreadingHTTPServer)

    @classmethod
    def tearDownClass(cls):
        stub_server.stop(cls.stub)

    def setUp(self):
        self._saved = {n: getattr(settings, n) for n in self.NAMES}
        settings.agentaus_base_url = stub_server.url(self.stub)
        settings.agentaus_api_key = "test-key"
        settings.agentaus_thinking = True
        settings.agentaus_plan_async = True
        for n in ("agentaus_self_review", "agentaus_grounding_check", "agentaus_syntax_check",
                  "agentaus_search", "agentaus_web_search", "agentaus_inventory",
                  "agentaus_zoom", "agentaus_investigate"):
            setattr(settings, n, False)
        settings.agentaus_correction_rounds = 0
        CALLS.clear()
        plans.clear()
        MAIN_REPLY[0] = "Done."

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(settings, n, v)
        plans.clear()

    def _turns(self, bodies, pause=0.0, session=b"session-1"):
        from starlette.requests import Request

        async def go():
            server.app.state.client = httpx.AsyncClient()
            try:
                for i, body in enumerate(bodies):
                    if i and pause:
                        await asyncio.sleep(pause)
                    request = Request({"type": "http", "app": server.app, "headers": [
                        (b"x-claude-code-session-id", session)] if session else []})
                    response = await server._handle_agentaus(request, body, "agentaus", True)
                    async for _ in response.body_iterator:
                        pass
            finally:
                for entry in list(plans._turns.values()):
                    await asyncio.wait([entry.task], timeout=3)
                await server.app.state.client.aclose()

        run(go())

    def test_the_first_call_goes_out_before_the_plan_is_written(self):
        started = time.monotonic()
        self._turns([first()])
        mains = [c for c in CALLS if not c["planning"]]
        self.assertTrue(any(c["planning"] for c in CALLS), "the turn was not planned")
        self.assertEqual(len(mains), 1)
        self.assertLess(mains[0]["at"] - started, PLAN_SECONDS / 2,
                        "the first call waited for the planner")
        self.assertNotIn("PLAN-MARKER", mains[0]["system"])

    def test_the_plan_joins_the_next_request_of_the_turn(self):
        self._turns([first(), after_tool()], pause=PLAN_SECONDS + 0.4)
        mains = [c for c in CALLS if not c["planning"]]
        self.assertEqual(len(mains), 2)
        self.assertIn("PLAN-MARKER", mains[1]["system"])
        self.assertIn("written when it started", mains[1]["system"])
        self.assertEqual(sum(c["planning"] for c in CALLS), 1, "planned the same turn twice")

    def test_the_second_request_waits_for_a_plan_still_being_written(self):
        settings.agentaus_plan_wait_seconds = 5
        self._turns([first(), after_tool(), after_tool()])
        mains = [c for c in CALLS if not c["planning"]]
        self.assertNotIn("PLAN-MARKER", mains[0]["system"])
        self.assertIn("PLAN-MARKER", mains[1]["system"], "the second call did not wait")

    def test_no_wait_when_off(self):
        settings.agentaus_plan_wait_seconds = 0
        self._turns([first(), after_tool()])
        mains = [c for c in CALLS if not c["planning"]]
        self.assertNotIn("PLAN-MARKER", mains[1]["system"])

    def test_a_new_message_is_a_new_turn(self):
        self._turns([first(), after_tool("something else entirely")],
                    pause=PLAN_SECONDS + 0.4)
        mains = [c for c in CALLS if not c["planning"]]
        self.assertNotIn("PLAN-MARKER", mains[1]["system"],
                         "another turn's plan was carried over")

    def test_blocking_mode_still_plans_first(self):
        settings.agentaus_plan_async = False
        self._turns([first()])
        self.assertTrue(CALLS[0]["planning"])
        self.assertIn("PLAN-MARKER", CALLS[1]["system"])

    def test_without_a_session_it_plans_first(self):
        # Two sessionless clients opening with the same message must not share a plan.
        self._turns([first()], session=None)
        self.assertTrue(CALLS[0]["planning"])
        self.assertIn("PLAN-MARKER", CALLS[1]["system"])
        self.assertEqual(plans._turns, {})

    def _priorities(self):
        seen = []
        real = server.hold

        def spy(label="helper call", priority="normal"):
            seen.append((label, priority))
            return real(label, priority)

        return seen, mock.patch.object(server, "hold", spy)

    def test_only_a_blocking_plan_is_urgent(self):
        seen, spy = self._priorities()
        with spy:
            self._turns([first()])
        self.assertIn(("planning", "normal"), seen)
        self.assertNotIn(("planning", "urgent"), seen)
        settings.agentaus_plan_async = False
        plans.clear()
        seen, spy = self._priorities()
        with spy:
            self._turns([first()])
        self.assertIn(("planning", "urgent"), seen)


class TheGroundingCheckStartsBesideTheStallCheck(TheFirstCallDoesNotWaitForThePlan):
    def setUp(self):
        super().setUp()
        settings.agentaus_thinking = False
        settings.agentaus_grounding_check = True
        settings.agentaus_grounding_one_pass = True
        settings.agentaus_correction_rounds = 2
        MAIN_REPLY[0] = ("The bug was in f: it returned None. " * 15).strip()

    def _kinds(self):
        return {c["kind"]: c["at"] for c in CALLS if c["kind"] in ("classify", "ground")}

    def test_overlapped(self):
        settings.agentaus_overlap_checks = True
        self._turns([after_tool()])
        at = self._kinds()
        self.assertEqual(set(at), {"classify", "ground"})
        self.assertLess(abs(at["ground"] - at["classify"]), CHECK_SECONDS / 2,
                        "the grounding check waited for the stall check")

    def test_in_order_when_off(self):
        settings.agentaus_overlap_checks = False
        self._turns([after_tool()])
        at = self._kinds()
        self.assertGreaterEqual(at["ground"] - at["classify"], CHECK_SECONDS * 0.9)

    def test_a_finish_gate_reask_cancels_the_overlapped_check(self):
        settings.agentaus_overlap_checks = True
        settings.agentaus_finish_gate = True
        cancelled = []

        async def probe(answer):
            try:
                await asyncio.sleep(CHECK_SECONDS * 4)
            except asyncio.CancelledError:
                cancelled.append(time.monotonic())
                raise
            return answer

        gate = mock.Mock(side_effect=[("unverified", "Run it.", False), ("", "", False)])
        with mock.patch.object(server, "_answer_check", lambda c, o, a, r: probe(a)), \
                mock.patch.object(finish, "finish_gate", gate):
            self._turns([after_tool()])
        mains = [c["at"] for c in CALLS if c["kind"] == "main"]
        self.assertEqual(len(mains), 2, "the finish gate did not re-ask")
        self.assertTrue(cancelled, "the first answer's check was never cancelled")
        self.assertLess(cancelled[0], mains[1],
                        "the first answer's check kept running into the re-ask")

    # The planner tests do not apply here.
    test_the_first_call_goes_out_before_the_plan_is_written = None
    test_the_plan_joins_the_next_request_of_the_turn = None
    test_a_new_message_is_a_new_turn = None
    test_the_second_request_waits_for_a_plan_still_being_written = None
    test_no_wait_when_off = None
    test_blocking_mode_still_plans_first = None
    test_without_a_session_it_plans_first = None
    test_only_a_blocking_plan_is_urgent = None


class TheTurnKey(unittest.TestCase):
    def test_a_tool_round_trip_keeps_it_and_a_new_message_changes_it(self):
        a = plans.turn_key("s", ["fix it"])
        self.assertEqual(a, plans.turn_key("s", ["fix it"]))
        self.assertNotEqual(a, plans.turn_key("s", ["fix it", "now the tests"]))
        self.assertNotEqual(a, plans.turn_key("other", ["fix it"]))
        self.assertIsNone(plans.turn_key("s", []))

    def test_no_session_no_key(self):
        self.assertIsNone(plans.turn_key(None, ["fix it"]))
        self.assertIsNone(plans.turn_key("", ["fix it"]))


class ANewTurnCancelsTheLastOnesPlan(unittest.TestCase):
    def tearDown(self):
        plans.clear()

    def test_same_session_only(self):
        async def go():
            async def slow():
                await asyncio.sleep(30)
                return "plan"

            plans.start("a1", slow, session="a")
            plans.start("b1", slow, session="b")
            old, other = plans._turns["a1"].task, plans._turns["b1"].task
            plans.start("a2", slow, session="a")
            await asyncio.sleep(0)
            self.assertTrue(old.cancelled())
            self.assertNotIn("a1", plans._turns)
            self.assertFalse(other.done())
            self.assertTrue(plans.pending("a2"))
            plans.clear()

        run(go())


class AClosedStreamCancelsItsCheck(unittest.TestCase):
    def test_client_disconnect(self):
        async def go():
            started = []

            async def inner(*args, checks=None, **kwargs):
                task = asyncio.ensure_future(asyncio.sleep(30))
                checks.add(task)
                started.append(task)
                yield b"one"
                yield b"two"

            with mock.patch.object(server, "_unguarded_event_stream", inner):
                stream = server._agentaus_event_stream()
                self.assertEqual(await stream.__anext__(), b"one")
                await stream.aclose()
            await asyncio.sleep(0)
            self.assertTrue(started[0].cancelled())

        run(go())

    def test_mid_turn_wording(self):
        self.assertIn("written when it started", with_plan("sys", "1. x", mid_turn=True))
        self.assertIn("which you wrote just now", with_plan("sys", "1. x"))


class TheSyntaxGateOnToolLoopAnswers(unittest.TestCase):
    def test_command_output_is_not_python(self):
        for text in ("Done.\n```\n15 passed in 0.02s\n```",
                     "```\nINV-0003,\"Acme, Inc.\",2024-10-16,2882.00,paid\n```"):
            self.assertEqual(syntax.first_error(text, strict=True), "", text)
            self.assertTrue(syntax.first_error(text), "non-strict behaviour must not change")

    def test_python_is_still_checked(self):
        self.assertTrue(syntax.first_error("```\ndef f(:\n    pass\n```", strict=True))
        self.assertTrue(syntax.first_error("```python\nx = (\n```", strict=True))

    def test_a_failure_report_quoting_a_test_is_not_python(self):
        text = ("Still failing:\n```\n____ test_x ____\n\n    def test_x():\n"
                ">       assert f(2) == 4\nE       assert 5 == 4\n```")
        self.assertEqual(syntax.first_error(text, strict=True), "")


class AGroundingRevisionThatLosesTheAnswer(unittest.TestCase):
    ANSWER = "The bug was in f: it returned None, and `g` passed that on. " * 10
    BODY = after_tool()

    def setUp(self):
        self._saved = (settings.agentaus_grounding_one_pass, settings.agentaus_base_url,
                       settings.agentaus_api_key, settings.agentaus_stream_helpers)

    def tearDown(self):
        (settings.agentaus_grounding_one_pass, settings.agentaus_base_url,
         settings.agentaus_api_key, settings.agentaus_stream_helpers) = self._saved

    def _check(self, *replies, one_pass=True):
        settings.agentaus_grounding_one_pass = one_pass
        queue = list(replies)

        async def summarise(client, text, **kwargs):
            return queue.pop(0)

        with mock.patch.object(server, "_agentaus_summarise", summarise):
            return run(server._check_grounding(None, self.BODY, self.ANSWER))

    def _cut(self, text):
        reply = server._HelperText(text)
        reply.finish_reason = "length"
        return reply

    def test_kept_when_cut_off_or_drastically_shorter(self):
        most = self.ANSWER[: int(len(self.ANSWER) * 0.8)]
        self.assertEqual(self._check(self._cut("REVISED\n" + most)), self.ANSWER)
        self.assertEqual(self._check("REVISED\nRemoved the claim about g."), self.ANSWER)
        self.assertEqual(self._check("REVISED\n" + most), most.strip())

    def test_the_two_call_path_too(self):
        most = self.ANSWER[: int(len(self.ANSWER) * 0.8)]
        gaps = "GAPS\n- g was never read"
        self.assertEqual(self._check(gaps, "Removed g.", one_pass=False), self.ANSWER)
        self.assertEqual(self._check(gaps, self._cut(most), one_pass=False), self.ANSWER)
        self.assertEqual(self._check(gaps, most, one_pass=False), most.strip())

    def test_the_streamed_helper_reports_how_it_ended(self):
        stub = stub_server.start(_Upstream, ThreadingHTTPServer)
        settings.agentaus_base_url = stub_server.url(stub)
        settings.agentaus_api_key = "test-key"
        settings.agentaus_stream_helpers = True

        async def go():
            async with httpx.AsyncClient() as client:
                return (await server._agentaus_summarise(client, "TRUNCATE-ME"),
                        await server._agentaus_summarise(client, "fine"))

        try:
            cut, whole = run(go())
        finally:
            stub_server.stop(stub)
        self.assertEqual(cut.finish_reason, "length")
        self.assertEqual(whole.finish_reason, "stop")


class TheOnePassGroundingReply(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(grounding_revision("GROUNDED"), "")
        self.assertEqual(grounding_revision("REVISED\nThe fixed answer."), "The fixed answer.")
        self.assertEqual(grounding_revision("**REVISED**\nX"), "X")
        self.assertEqual(grounding_revision("Here is the answer: REVISED"), "")
        self.assertEqual(grounding_revision("REVISED\n"), "")


if __name__ == "__main__":
    unittest.main()
