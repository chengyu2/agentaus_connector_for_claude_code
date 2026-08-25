"""Summarising compaction: the head of the conversation is replaced by a summary.

Two properties matter most and are easy to get wrong:

* **Cost.** The bridge is stateless - Claude Code re-sends the whole conversation
  every turn - so a naive implementation re-summarises the entire history on every
  request. The summarised region is a stable prefix, so it must be cached and paid
  for once.

* **Structure.** A `tool_result` answers a `tool_use` in the preceding assistant
  turn. If the kept tail begins at a `tool_result`, the request is malformed and
  Agentaus rejects it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge.compact import (  # noqa: E402
    ConversationCompactor,
    render_for_summary,
    split_head_tail,
)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def convo(turns: int, words: int = 400) -> list:
    msgs = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"turn {i}: " + "detail " * words})
        msgs.append({"role": "assistant", "content": f"reply {i}"})
    msgs.append({"role": "user", "content": "the current question"})
    return msgs


class _Counter:
    """Records how many summarisation calls were made."""

    def __init__(self, reply: str = "SUMMARY: decisions and file paths preserved"):
        self.calls = 0
        self.seen: list[str] = []
        self.reply = reply

    async def __call__(self, text: str) -> str:
        self.calls += 1
        self.seen.append(text)
        return self.reply


class TestSplit(unittest.TestCase):
    def test_newest_message_is_always_kept(self):
        messages = convo(20)
        _, tail = split_head_tail(messages, 500)

        self.assertEqual(tail[-1], messages[-1])

    def test_tail_starts_on_a_clean_turn(self):
        messages = [
            {"role": "user", "content": "old " * 500},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "data"}]},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "next"},
        ]
        _, tail = split_head_tail(messages, 50)

        first = tail[0]
        self.assertEqual(first["role"], "user")
        if isinstance(first.get("content"), list):
            self.assertFalse(any(b.get("type") == "tool_result" for b in first["content"]),
                             "tail begins with an orphaned tool_result")

    def test_head_and_tail_together_are_the_original(self):
        messages = convo(10)
        head, tail = split_head_tail(messages, 400)

        self.assertEqual(head + tail, messages, "compaction must not lose or reorder messages")


class TestRendering(unittest.TestCase):
    def test_tool_traffic_is_included(self):
        """Which tool ran and what it returned is often the substance of the session."""
        text = render_for_summary([
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Edit",
                 "input": {"file_path": "/src/server.py"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "patched"}]},
        ])

        self.assertIn("Edit", text)
        self.assertIn("/src/server.py", text, "file paths must survive into the summary input")
        self.assertIn("patched", text)


class TestCompaction(unittest.TestCase):
    def test_summary_replaces_the_head(self):
        counter = _Counter()
        c = ConversationCompactor(counter)
        body = {"messages": convo(40), "system": "sys"}

        plan = run(c.compact(body, limit=3000, reserve=100))

        self.assertEqual(plan["method"], "summarised")
        self.assertIn("SUMMARY", plan["summary"])
        self.assertGreater(plan["summarised"], 0)
        self.assertLess(len(plan["messages"]), len(body["messages"]))
        self.assertEqual(plan["messages"][-1], body["messages"][-1],
                         "the question being answered must survive verbatim")

    def test_repeat_turn_reuses_the_cached_summary(self):
        """The expensive part must be paid once, not on every turn."""
        counter = _Counter()
        c = ConversationCompactor(counter)
        body = {"messages": convo(40), "system": "sys"}

        run(c.compact(body, limit=3000, reserve=100))
        first = counter.calls
        run(c.compact(body, limit=3000, reserve=100))

        self.assertEqual(counter.calls, first, "the same prefix was summarised twice")
        self.assertEqual(c.hits, 1)

    def test_no_work_when_it_already_fits(self):
        counter = _Counter()
        c = ConversationCompactor(counter)
        body = {"messages": [{"role": "user", "content": "hi"}]}

        plan = run(c.compact(body, limit=131072, reserve=100))

        self.assertEqual(plan["method"], "none")
        self.assertEqual(counter.calls, 0, "a conversation that fits must cost nothing")

    def test_falls_back_to_trimming_when_the_summariser_fails(self):
        """A broken summariser must not take the turn down with it."""
        async def broken(_: str) -> str:
            raise RuntimeError("upstream refused")

        c = ConversationCompactor(broken)
        body = {"messages": convo(40), "system": "sys"}

        plan = run(c.compact(body, limit=3000, reserve=100))

        self.assertEqual(plan["method"], "trimmed")
        self.assertEqual(plan["messages"][-1], body["messages"][-1])

    def test_huge_head_is_summarised_in_pieces(self):
        counter = _Counter(reply="short")
        c = ConversationCompactor(counter)
        body = {"messages": convo(300), "system": "sys"}

        plan = run(c.compact(body, limit=4000, reserve=100))

        self.assertEqual(plan["method"], "summarised")
        self.assertGreater(counter.calls, 1, "an oversized head must be chunked")

    def test_summarisation_input_carries_the_preservation_instruction(self):
        counter = _Counter()
        c = ConversationCompactor(counter)
        run(c.compact({"messages": convo(40), "system": "s"}, limit=3000, reserve=100))

        prompt = counter.seen[0]
        self.assertIn("File paths", prompt)
        self.assertIn("Never invent", prompt, "the summariser must be told not to fabricate")

    def test_cache_is_bounded(self):
        counter = _Counter()
        c = ConversationCompactor(counter, cache_size=2)
        for i in range(5):
            run(c.summarise_head([{"role": "user", "content": f"unique {i}"}], chunk_budget=1000))

        self.assertLessEqual(len(c._cache), 2, "cache must not grow without bound")


if __name__ == "__main__":
    unittest.main()
