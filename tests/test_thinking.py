"""Synthesised thinking, tool injection, and keeping bridge tools off the wire.

Agentaus has no native thinking mode, so the bridge asks for the plan as its own turn
and presents it in the block the client already renders for reasoning. Two properties
have to hold: the plan must reach the client as a well-formed thinking block, and it
must not come back to haunt the next turn - Claude Code replays every block it was
sent, and a thinking block Agentaus cannot parse would break the conversation.

The third group here guards the invariant that makes bridge-executed tools work at all:
`agentaus_search` must never appear in anything Claude Code is asked to run, because it
has never heard of it.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import tools  # noqa: E402
from agentaus_bridge.augment import plan_prompt, should_think, with_plan  # noqa: E402
from agentaus_bridge.server import (  # noqa: E402
    _openai_calls,
    _partition_tool_calls,
    _without_bridge_calls,
)
from agentaus_bridge.translate import (  # noqa: E402
    AnthropicStreamBuilder,
    agentaus_response_to_anthropic,
    anthropic_request_to_agentaus,
    inject_bridge_tools,
)


def events(raw: bytes) -> list:
    """Parse an SSE byte string into its decoded data payloads."""
    out = []
    for block in raw.decode().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


class TestWhenThinkingRuns(unittest.TestCase):
    def test_a_bare_prose_turn_is_not_planned(self):
        """A planning round trip on "what does this do" costs latency and buys nothing."""
        self.assertFalse(should_think({"messages": [{"role": "user", "content": "hi"}]}))

    def test_tools_present_means_the_turn_will_act(self):
        self.assertTrue(should_think({"tools": [{"name": "Read"}]}))

    def test_the_clients_own_thinking_toggle_drives_it(self):
        self.assertTrue(should_think({"thinking": {"type": "enabled", "budget_tokens": 4000}}))
        self.assertFalse(should_think({"thinking": {"type": "disabled"}}))

    def test_the_plan_prompt_names_the_tools_actually_available(self):
        prompt = plan_prompt("fix it", {"tools": [{"name": "Read"}, {"name": "Bash"}]})
        self.assertIn('<tool name="Read">', prompt)
        self.assertIn('<tool name="Bash">', prompt)
        self.assertIn("<tools_available>", prompt)
        self.assertIn("fix it", prompt)

    def test_an_empty_plan_leaves_the_system_prompt_alone(self):
        self.assertEqual(with_plan("sys", ""), "sys")
        self.assertEqual(with_plan("sys", "   "), "sys")

    def test_the_plan_is_folded_into_the_system_prompt(self):
        merged = with_plan("sys", "1. read config.py")
        self.assertIn("sys", merged)
        self.assertIn("read config.py", merged)

    def test_a_block_system_prompt_stays_a_list(self):
        merged = with_plan([{"type": "text", "text": "sys"}], "plan")
        self.assertIsInstance(merged, list)
        self.assertEqual(len(merged), 2)


class TestThinkingReachesTheClient(unittest.TestCase):
    def test_the_stream_block_is_well_formed(self):
        builder = AnthropicStreamBuilder("agentaus")
        builder.start()
        parsed = events(builder.thinking("check config.py first"))

        self.assertEqual(parsed[0]["type"], "content_block_start")
        self.assertEqual(parsed[0]["content_block"], {"type": "thinking", "thinking": ""})
        self.assertTrue(any(e.get("delta", {}).get("type") == "thinking_delta" for e in parsed))
        self.assertEqual(parsed[-1]["type"], "content_block_stop")
        self.assertEqual(
            "".join(e["delta"]["thinking"] for e in parsed
                    if e.get("delta", {}).get("type") == "thinking_delta"),
            "check config.py first",
        )

    def test_indices_stay_in_order_when_text_follows(self):
        """Anthropic's contract: block indices match their position in the message."""
        builder = AnthropicStreamBuilder("agentaus")
        builder.start()
        thinking = events(builder.thinking("plan"))
        text = events(builder.text("answer"))
        self.assertEqual(thinking[0]["index"], 0)
        self.assertEqual(text[0]["index"], 1)

    def test_an_empty_plan_emits_no_block(self):
        builder = AnthropicStreamBuilder("agentaus")
        builder.start()
        self.assertEqual(builder.thinking(""), b"")
        self.assertEqual(builder.thinking("   "), b"")

    def test_the_non_stream_response_leads_with_it(self):
        message = agentaus_response_to_anthropic(
            {"choices": [{"message": {"content": "the answer"}, "finish_reason": "stop"}]},
            model="agentaus",
            thinking="the plan",
        )
        self.assertEqual(message["content"][0], {"type": "thinking", "thinking": "the plan"})
        self.assertEqual(message["content"][1]["text"], "the answer")

    def test_a_plan_with_no_answer_still_carries_a_text_block(self):
        """Otherwise the client renders a turn that reasoned and then said nothing."""
        message = agentaus_response_to_anthropic(
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            model="agentaus",
            thinking="the plan",
        )
        self.assertTrue(any(b["type"] == "text" for b in message["content"]))

    def test_a_replayed_thinking_block_is_dropped(self):
        """Claude Code sends every block back next turn. These must not be replayed:
        they are the bridge's own synthesis and Agentaus cannot parse the type."""
        payload = anthropic_request_to_agentaus({
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "my plan"},
                    {"type": "text", "text": "the answer"},
                ]},
            ]
        })
        rendered = json.dumps(payload)
        self.assertNotIn("my plan", rendered)
        self.assertIn("the answer", rendered)


class TestBridgeToolsNeverReachTheClient(unittest.TestCase):
    def test_search_is_injected_and_grep_is_restricted(self):
        body = inject_bridge_tools(
            {"tools": [
                {"name": "Grep", "description": "search with a regex",
                 "input_schema": {"type": "object"}},
                {"name": "Read", "description": "read a file",
                 "input_schema": {"type": "object"}},
            ]},
            [tools.SEARCH_SCHEMA],
        )
        names = [t["name"] for t in body["tools"]]
        self.assertIn(tools.SEARCH_TOOL, names)

        grep = next(t for t in body["tools"] if t["name"] == "Grep")
        self.assertIn("search with a regex", grep["description"], "the original was lost")
        self.assertIn("agentaus_search", grep["description"])
        self.assertEqual(grep["input_schema"], {"type": "object"},
                         "the schema must be untouched - Claude Code still runs Grep")

        read = next(t for t in body["tools"] if t["name"] == "Read")
        self.assertEqual(read["description"], "read a file")

    def test_a_turn_with_no_tools_is_left_alone(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(inject_bridge_tools(body, [tools.SEARCH_SCHEMA]), body)

    def test_injection_does_not_duplicate_on_a_refit(self):
        first = inject_bridge_tools(
            {"tools": [{"name": "Read", "input_schema": {"type": "object"}}]},
            [tools.SEARCH_SCHEMA],
        )
        second = inject_bridge_tools(first, [tools.SEARCH_SCHEMA])
        names = [t["name"] for t in second["tools"]]
        self.assertEqual(names.count(tools.SEARCH_TOOL), 1)

    def test_the_callers_body_is_not_mutated(self):
        body = {"tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
        inject_bridge_tools(body, [tools.SEARCH_SCHEMA])
        self.assertEqual(len(body["tools"]), 1)

    def test_calls_are_split_by_owner(self):
        mine, theirs, invented = _partition_tool_calls([
            {"id": "1", "name": tools.SEARCH_TOOL, "arguments": "{}"},
            {"id": "2", "name": "Read", "arguments": "{}"},
        ], {"Read", tools.SEARCH_TOOL})
        self.assertEqual([c["id"] for c in mine], ["1"])
        self.assertEqual([c["id"] for c in theirs], ["2"])
        self.assertEqual(invented, [])

    def test_a_tool_the_model_made_up_is_caught(self):
        """Observed on the first live Agentaus turn: it answered a search by calling
        `open_file`, which nobody had offered it. Passing that to Claude Code fails a
        tool_use for a tool that does not exist, and the turn dies looking like a
        bridge fault."""
        mine, theirs, invented = _partition_tool_calls([
            {"id": "1", "name": "open_file", "arguments": "{}"},
            {"id": "2", "name": "Read", "arguments": "{}"},
        ], {"Read", tools.SEARCH_TOOL})
        self.assertEqual(mine, [])
        self.assertEqual([c["id"] for c in theirs], ["2"])
        self.assertEqual([c["id"] for c in invented], ["1"])

    def test_nothing_is_called_invented_when_the_offered_set_is_unknown(self):
        """Without the list actually sent upstream, nothing can be judged invented -
        so the check is skipped rather than guessed at."""
        mine, theirs, invented = _partition_tool_calls(
            [{"id": "1", "name": "whatever", "arguments": "{}"}], set())
        self.assertEqual(invented, [])
        self.assertEqual([c["id"] for c in theirs], ["1"])

    def test_the_correction_names_the_real_tools(self):
        from agentaus_bridge.server import _correction_for
        text = _correction_for({"name": "open_file"}, {"Read", "Grep"})
        self.assertIn("open_file", text)
        self.assertIn("Grep, Read", text)
        self.assertIn("invented", text)

    def test_known_names_come_from_the_payload_actually_sent(self):
        from agentaus_bridge.server import _known_tool_names
        self.assertEqual(
            _known_tool_names({"tools": [
                {"type": "function", "function": {"name": "Read"}},
                {"type": "function", "function": {"name": "agentaus_search"}},
            ]}),
            {"Read", "agentaus_search"},
        )

    def test_an_unanswered_bridge_call_is_stripped_from_the_response(self):
        """Out of tool rounds, the pending call must not be surfaced: Claude Code has
        never heard of agentaus_search and would fail the tool_use rather than run it."""
        data = {"choices": [{
            "message": {"content": "", "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": tools.SEARCH_TOOL, "arguments": "{}"}},
            ]},
            "finish_reason": "tool_calls",
        }]}
        cleaned = _without_bridge_calls(data)
        message = cleaned["choices"][0]["message"]
        self.assertIsNone(message["tool_calls"])
        self.assertTrue(message["content"].strip(), "an empty turn tells the user nothing")
        self.assertEqual(cleaned["choices"][0]["finish_reason"], "stop")

    def test_a_clients_own_call_survives_stripping(self):
        data = {"choices": [{
            "message": {"content": "", "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": tools.SEARCH_TOOL, "arguments": "{}"}},
                {"id": "2", "type": "function",
                 "function": {"name": "Read", "arguments": "{}"}},
            ]},
            "finish_reason": "tool_calls",
        }]}
        cleaned = _without_bridge_calls(data)
        kept = cleaned["choices"][0]["message"]["tool_calls"]
        self.assertEqual([c["function"]["name"] for c in kept], ["Read"])
        self.assertEqual(cleaned["choices"][0]["finish_reason"], "tool_calls")

    def test_calls_are_read_out_of_a_non_stream_response(self):
        calls = _openai_calls({"choices": [{"message": {"tool_calls": [
            {"id": "x", "type": "function",
             "function": {"name": "Read", "arguments": '{"p":1}'}},
        ]}}]})
        self.assertEqual(calls, [{"id": "x", "name": "Read", "arguments": '{"p":1}'}])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _ScriptedAgentaus:
    """An upstream that calls agentaus_search once, then answers.

    Only the buffered branch is scripted: `_agentaus_event_stream` takes the same path
    for a non-streaming payload, and the tool round-trip is what is under test here,
    not the SSE parsing (covered in test_translate.py).
    """

    def __init__(self) -> None:
        self.bodies: list = []

    async def post(self, url, *, json=None, headers=None):  # noqa: A002
        self.bodies.append(json)
        roles = [m.get("role") for m in (json or {}).get("messages") or []]
        if "tool" in roles:
            return _FakeResponse({
                "choices": [{"message": {"content": "The cap is asyncio.Semaphore(6) in gate.py."},
                             "finish_reason": "stop"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        return _FakeResponse({
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": tools.SEARCH_TOOL,
                    "arguments": '{"query": "where is the cap", "path": "/tmp"}'}},
            ]}, "finish_reason": "tool_calls"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })


class TestTheInnerToolLoop(unittest.IsolatedAsyncioTestCase):
    async def test_a_search_is_run_and_answered_without_the_client_seeing_it(self):
        from unittest import mock

        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        upstream = _ScriptedAgentaus()

        async def fake_search(name, arguments, call, default_path=None):
            return "gate.py:12  _gate = asyncio.Semaphore(6)"

        previous = settings.agentaus_self_review
        settings.agentaus_self_review = False
        try:
            with mock.patch.object(tools, "execute", fake_search):
                chunks = []
                async for chunk in server._agentaus_event_stream(
                    upstream,
                    {"messages": [{"role": "user", "content": "where is the cap"}],
                     "stream": False},
                    {"messages": [{"role": "user", "content": "where is the cap"}]},
                    "agentaus",
                    0.0,
                    plan="1. search for the cap",
                ):
                    chunks.append(chunk)
        finally:
            settings.agentaus_self_review = previous

        raw = b"".join(chunks).decode()

        # The tool ran, and its result went back upstream as a `tool` message.
        self.assertEqual(len(upstream.bodies), 2, "the search result was never re-asked")
        self.assertIn("Semaphore(6)", json.dumps(upstream.bodies[1]))

        # None of that reached the client: no tool_use block at all.
        self.assertNotIn("agentaus_search", raw)
        self.assertNotIn("tool_use", raw)

        # The answer did, and so did the plan.
        self.assertIn("asyncio.Semaphore(6) in gate.py", raw)
        self.assertIn("thinking_delta", raw)

    async def test_a_client_owned_call_is_emitted_untouched(self):
        from agentaus_bridge import server
        from agentaus_bridge.config import settings

        class _ClientTool:
            bodies: list = []

            async def post(self, url, *, json=None, headers=None):  # noqa: A002
                self.bodies.append(json)
                return _FakeResponse({
                    "choices": [{"message": {"content": "", "tool_calls": [
                        {"id": "t1", "type": "function",
                         "function": {"name": "Read", "arguments": '{"file_path":"a.py"}'}},
                    ]}, "finish_reason": "tool_calls"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                })

        upstream = _ClientTool()
        previous = settings.agentaus_self_review
        settings.agentaus_self_review = False
        try:
            chunks = []
            async for chunk in server._agentaus_event_stream(
                upstream,
                {"messages": [{"role": "user", "content": "read a.py"}], "stream": False},
                {"messages": [{"role": "user", "content": "read a.py"}]},
                "agentaus",
                0.0,
            ):
                chunks.append(chunk)
        finally:
            settings.agentaus_self_review = previous

        raw = b"".join(chunks).decode()
        self.assertEqual(len(upstream.bodies), 1, "a client tool must not be re-asked")
        self.assertIn("tool_use", raw)
        self.assertIn("Read", raw)


class TestNameResolution(unittest.TestCase):
    """A model that spells a tool name carelessly has not invented a tool.

    Observed live: a turn spent one of its three tool rounds being corrected from `read`
    to `Read`. That is the same tool, and burning a round on it left nothing for the
    work the rounds were for.
    """

    def setUp(self):
        from agentaus_bridge.server import _canonical
        self.canonical = _canonical
        self.known = {"Read", "Grep", "agentaus_search"}

    def test_case_is_forgiven(self):
        self.assertEqual(self.canonical("read", self.known), "Read")
        self.assertEqual(self.canonical("GREP", self.known), "Grep")

    def test_separators_are_forgiven(self):
        self.assertEqual(self.canonical("agentaus-search", self.known), "agentaus_search")
        self.assertEqual(self.canonical("agentaussearch", self.known), "agentaus_search")

    def test_an_exact_name_passes_straight_through(self):
        self.assertEqual(self.canonical("Read", self.known), "Read")

    def test_a_genuinely_invented_name_is_not_rescued(self):
        """`open_file` is not a misspelling of anything offered - it was made up."""
        self.assertIsNone(self.canonical("open_file", self.known))

    def test_a_resolved_call_is_routed_by_its_canonical_name(self):
        mine, theirs, invented = _partition_tool_calls(
            [{"id": "1", "name": "AGENTAUS_SEARCH", "arguments": "{}"}],
            {"Read", tools.SEARCH_TOOL},
        )
        self.assertEqual(invented, [])
        self.assertEqual(theirs, [])
        self.assertEqual(mine[0]["name"], tools.SEARCH_TOOL,
                         "a resolved name must be rewritten, or the dispatcher misses it")


class TestNoReplanningMidLoop(unittest.TestCase):
    """A plan is written from the user's message, and the planner never sees tool
    results. Re-deriving one mid-loop tells the model to redo the step it just did.

    Observed live on a real document: the model read a .docx, was handed a fresh plan
    saying "read the .docx", and answered "I am unable to access files on your local
    system" while holding the file's contents.
    """

    def test_a_fresh_tool_turn_still_plans(self):
        self.assertTrue(should_think({"tools": [{"name": "Read"}],
                                      "messages": [{"role": "user", "content": "do it"}]}))

    def test_a_returning_tool_result_does_not(self):
        self.assertFalse(should_think({
            "tools": [{"name": "Read"}],
            "messages": [
                {"role": "user", "content": "do it"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "a", "name": "Read", "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "data"}]},
            ]}))

    def test_a_new_user_message_after_a_tool_plans_again(self):
        """The loop is over once the user speaks again - that is a new turn."""
        self.assertTrue(should_think({
            "tools": [{"name": "Read"}],
            "messages": [
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "data"}]},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "now the next bit"},
            ]}))
