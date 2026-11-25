"""What a turn does when the search budget runs out, or the upstream answers with nothing.

Both used to end in a re-ask with every tool removed. That forced a text reply, and a text
reply ends Claude Code's agent turn - so a model that only needed to run the tests or make
the edit stopped mid-task and waited for the user. Now:

- Out of bridge rounds with a search still wanted, only the bridge's tools are withdrawn.
  A call to one of Claude Code's tools goes to the client; only a model that still asks
  for a search is asked again with no tools at all.
- An empty reply is first sent again unchanged, tools and all.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from agentaus_bridge import server  # noqa: E402
from agentaus_bridge import tools as bridge_tools  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402

from test_bridge_robust import Stub, _WithStub, events_of, tool, user  # noqa: E402


class ToolStub(Stub):
    """A Stub whose `behave(payload)` returns a message: {"content": ..., "tool_calls": [...]}."""

    def __init__(self, behave):
        super().__init__(lambda p: (200, 0, ""))
        handler = self.server.RequestHandlerClass
        stub = self

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)))
            stub.calls.append(payload)
            message = behave(payload)
            delta = {"content": message.get("content") or ""}
            if message.get("tool_calls"):
                delta["tool_calls"] = [
                    {"index": i, "id": f"c{len(stub.calls)}-{i}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}
                    for i, (name, args) in enumerate(message["tool_calls"])]
            finish = "tool_calls" if message.get("tool_calls") else "stop"
            self.send_response(200)
            if not payload.get("stream"):
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{
                    "index": 0, "finish_reason": finish,
                    "message": {"role": "assistant", **delta}}]}).encode())
                return
            chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

        handler.do_POST = do_POST


def names(payload: dict) -> set:
    return {((t or {}).get("function") or {}).get("name") for t in payload.get("tools") or []}


def cut_off(payload: dict) -> bool:
    last = (payload.get("messages") or [{}])[-1]
    return last.get("role") == "user" and "search budget" in str(last.get("content"))


async def answered_search(client, payload, calls, default_path=None, seen=None):
    return server._with_tool_results(payload, calls, ["nothing relevant"] * len(calls))


async def unreviewed(client, question, answer):
    return answer


# (client streams, answer buffered for review). A streamed agent turn buffers by default.
MODES = ((True, False), (True, True), (False, False))


class _Loop(_WithStub):
    def setUp(self):
        super().setUp()
        for k in ("agentaus_search", "agentaus_tool_rounds", "agentaus_correction_rounds",
                  "agentaus_skill_library"):
            self.saved[k] = getattr(settings, k)
        settings.agentaus_search = True
        settings.agentaus_tool_rounds = 1
        settings.agentaus_correction_rounds = 3
        for target, value in (("_run_bridge_tools", answered_search),
                              ("_worth_checking", lambda *a, **k: False),
                              ("_self_review", unreviewed)):
            patcher = mock.patch.object(server, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tool_stub(self, behave) -> ToolStub:
        stub = ToolStub(behave)
        self.addCleanup(stub.close)
        settings.agentaus_base_url = stub.url
        return stub

    def turn(self, stream: bool, tools=("Bash", "Read")) -> list:
        """The content blocks of one turn, and its stop reason last."""
        body = {"model": "agentaus", "max_tokens": 200, "stream": stream,
                "tools": [tool(name) for name in tools],
                "messages": [user("why does the cache miss on reload? fix it")]}
        with TestClient(server.app) as client:
            r = client.post("/v1/messages", json=body)
        if not stream:
            message = r.json()
            return message["content"] + [message["stop_reason"]]
        blocks, stop = [], None
        for event in events_of(r.text):
            if event["type"] == "content_block_start":
                blocks.append(dict(event["content_block"]))
            elif event["type"] == "content_block_delta" and event["delta"].get("type") == "text_delta":
                blocks[-1]["text"] = blocks[-1].get("text", "") + event["delta"]["text"]
            elif event["type"] == "message_delta":
                stop = event["delta"]["stop_reason"]
        return [b for b in blocks if b["type"] != "thinking"] + [stop]


class RoundLimit(_Loop):
    def test_the_next_step_reaches_claude_code_when_the_search_budget_is_spent(self):
        def behave(payload):
            if cut_off(payload):
                return {"tool_calls": [("Bash", {"command": "pytest tests/test_cache.py"})]}
            return {"tool_calls": [(bridge_tools.SEARCH_TOOL, {"query": "cache reload", "path": "/tmp"})]}

        for stream, buffered in MODES:
            with self.subTest(stream=stream, buffered=buffered):
                settings.agentaus_self_review = buffered
                stub = self.tool_stub(behave)
                *blocks, stop = self.turn(stream)
                last = stub.calls[-1]
                self.assertTrue(cut_off(last))
                self.assertNotIn(bridge_tools.SEARCH_TOOL, names(last))
                self.assertIn("Bash", names(last))
                self.assertEqual(last["tool_choice"], "auto")
                self.assertEqual([b.get("name") for b in blocks if b["type"] == "tool_use"],
                                 ["Bash"])
                self.assertEqual(stop, "tool_use")

    def test_a_model_that_still_searches_is_asked_with_no_tools(self):
        def behave(payload):
            if not payload.get("tools"):
                return {"content": "The reload path skips the cache key."}
            return {"tool_calls": [(bridge_tools.SEARCH_TOOL, {"query": "cache reload", "path": "/tmp"})]}

        for stream, buffered in MODES:
            with self.subTest(stream=stream, buffered=buffered):
                settings.agentaus_self_review = buffered
                stub = self.tool_stub(behave)
                *blocks, stop = self.turn(stream)
                self.assertTrue(cut_off(stub.calls[-2]))
                self.assertNotIn("tools", stub.calls[-1])
                self.assertEqual([b["type"] for b in blocks], ["text"])
                self.assertIn("skips the cache key", blocks[0]["text"])
                self.assertEqual(stop, "end_turn")

    def test_a_client_call_beside_the_spent_search_is_forwarded_without_a_reask(self):
        def behave(payload):
            calls = [(bridge_tools.SEARCH_TOOL, {"query": "cache reload", "path": "/tmp"})]
            if len(stub.calls) > 1:
                calls.append(("Read", {"file_path": "/tmp/cache.py"}))
            return {"tool_calls": calls}

        for stream, buffered in MODES:
            with self.subTest(stream=stream, buffered=buffered):
                settings.agentaus_self_review = buffered
                stub = self.tool_stub(behave)
                *blocks, stop = self.turn(stream)
                self.assertFalse(any(cut_off(p) for p in stub.calls))
                self.assertEqual([b.get("name") for b in blocks if b["type"] == "tool_use"],
                                 ["Read"])
                self.assertEqual(stop, "tool_use")


    def test_a_spent_library_skill_call_is_not_forwarded(self):
        def behave(payload):
            calls = [(bridge_tools.SEARCH_TOOL, {"query": "cache reload", "path": "/tmp"})]
            if len(stub.calls) > 1:
                calls = [("Skill", {"skill": "cache-notes"}),
                         ("Read", {"file_path": "/tmp/cache.py"})]
            return {"tool_calls": calls}

        settings.agentaus_skill_library = True
        served = mock.patch.object(server, "with_library_skills",
                                   lambda body: (body, frozenset({"cache-notes"})))
        served.start()
        self.addCleanup(served.stop)
        for stream, buffered in MODES:
            with self.subTest(stream=stream, buffered=buffered):
                settings.agentaus_self_review = buffered
                stub = self.tool_stub(behave)
                *blocks, stop = self.turn(stream, tools=("Bash", "Read", "Skill"))
                self.assertEqual([b.get("name") for b in blocks if b["type"] == "tool_use"],
                                 ["Read"])
                self.assertEqual(stop, "tool_use")

    def test_narration_before_a_search_after_the_cut_off_ends_the_turn(self):
        def behave(payload):
            if not payload.get("tools"):
                return {"content": "The reload path skips the cache key."}
            search = [(bridge_tools.SEARCH_TOOL, {"query": "cache reload", "path": "/tmp"})]
            if cut_off(payload):
                return {"content": "Let me look once more.", "tool_calls": search}
            return {"tool_calls": search}

        for stream, buffered in MODES:
            with self.subTest(stream=stream, buffered=buffered):
                settings.agentaus_self_review = buffered
                stub = self.tool_stub(behave)
                *blocks, stop = self.turn(stream)
                self.assertFalse([b for b in blocks if b["type"] == "tool_use"])
                self.assertEqual(stop, "end_turn")


class EmptyReply(_Loop):
    def test_an_empty_reply_is_sent_again_with_its_tools(self):
        def behave(payload):
            if len(stub.calls) == 1:
                return {"content": ""}
            return {"tool_calls": [("Bash", {"command": "pytest"})]}

        for stream, buffered in MODES:
            with self.subTest(stream=stream, buffered=buffered):
                settings.agentaus_self_review = buffered
                stub = self.tool_stub(behave)
                *blocks, stop = self.turn(stream)
                self.assertEqual(len(stub.calls), 2)
                self.assertEqual(stub.calls[1]["tools"], stub.calls[0]["tools"])
                self.assertEqual(stub.calls[1]["messages"], stub.calls[0]["messages"])
                self.assertEqual([b.get("name") for b in blocks if b["type"] == "tool_use"],
                                 ["Bash"])
                self.assertEqual(stop, "tool_use")

    def test_a_second_empty_reply_falls_back_to_no_tools(self):
        """Buffered only: a live stream has no answer to replace (unchanged here)."""
        def behave(payload):
            if not payload.get("tools"):
                return {"content": "Nothing to change."}
            return {"content": ""}

        stub = self.tool_stub(behave)
        *blocks, stop = self.turn(stream=False)
        self.assertEqual(len(stub.calls), 3)
        self.assertNotIn("tools", stub.calls[-1])
        self.assertEqual(blocks[-1].get("text"), "Nothing to change.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
