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

    def test_tail_is_clean_when_the_conversation_ends_mid_tool_loop(self):
        """The boundary must go backwards when there is no clean turn ahead of it.

        Compaction usually fires on a user's message, so the newest message is a clean
        turn and the tail can start there. Fire it on a tool result instead - the model
        called a tool, the result came back large enough to tip the window - and every
        message from the boundary on is part of an exchange that has not closed. Walking
        forward settles on the `tool_result`, whose `tool_use` is now in the summarised
        head, and Agentaus answers 400 with no explanation.
        """
        messages = [{"role": "user", "content": "old " * 500},
                    {"role": "assistant", "content": "older reply"}]
        messages += [
            {"role": "user", "content": "read those files"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 40000}]},
        ]

        head, tail = split_head_tail(messages, 50)

        self.assertTrue(head, "nothing was summarised")
        first = tail[0]
        self.assertEqual(first["role"], "user")
        if isinstance(first.get("content"), list):
            self.assertFalse(
                any(b.get("type") == "tool_result" for b in first["content"]),
                "tail begins with a tool_result whose tool_use went into the head",
            )
        # The tool_use answering the kept tool_result has to travel with it.
        self.assertIn("t1", json.dumps(tail), "tool_use was separated from its result")

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


class TestQualityDesign(unittest.TestCase):
    """Behaviours chosen for fidelity and latency rather than economy."""

    def test_chunks_are_summarised_concurrently(self):
        """Sequential calls on a long history are minutes of visible latency."""
        import asyncio as aio

        in_flight = {"now": 0, "peak": 0}

        async def slow(_: str) -> str:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            await aio.sleep(0.02)
            in_flight["now"] -= 1
            return "s"

        c = ConversationCompactor(slow, verify=False)
        run(c.compact({"messages": convo(300), "system": "s"}, limit=4000, reserve=100))

        self.assertGreater(in_flight["peak"], 1, "summarisation ran one chunk at a time")

    def test_concurrency_is_bounded(self):
        """Unbounded fan-out on a very long history would hammer the API."""
        import asyncio as aio

        in_flight = {"now": 0, "peak": 0}

        async def slow(_: str) -> str:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            await aio.sleep(0.01)
            in_flight["now"] -= 1
            return "s"

        c = ConversationCompactor(slow, max_concurrency=3, verify=False)
        run(c.compact({"messages": convo(400), "system": "s"}, limit=4000, reserve=100))

        self.assertLessEqual(in_flight["peak"], 3)

    def test_verification_pass_recovers_missed_detail(self):
        """The gap pass exists to catch what a single summarising pass drops."""
        seen_prompts = []

        async def summariser(text: str) -> str:
            seen_prompts.append(text)
            if text.startswith("Below is a SUMMARY"):
                return "- the port is 9473"      # the gap pass finds a missed fact
            if text.startswith("The following are summaries"):
                # Merge must carry its input through, or it would mask what the
                # earlier passes produced and this test would prove nothing.
                return text.split("\n\n", 1)[-1]
            return "generic summary with no specifics"

        c = ConversationCompactor(summariser, verify=True)
        plan = run(c.compact({"messages": convo(40), "system": "s"}, limit=3000, reserve=100))

        self.assertIn("9473", plan["summary"], "recovered detail was not folded back in")
        self.assertTrue(any(p.startswith("Below is a SUMMARY") for p in seen_prompts),
                        "no verification pass was made")

    def test_verification_ignores_an_explicit_nothing_missing(self):
        async def summariser(text: str) -> str:
            if text.startswith("Below is a SUMMARY"):
                return "NONE"
            if text.startswith("The following are summaries"):
                return text.split("\n\n", 1)[-1]
            return "real summary"

        c = ConversationCompactor(summariser, verify=True)
        plan = run(c.compact({"messages": convo(40), "system": "s"}, limit=3000, reserve=100))

        self.assertNotIn("NONE", plan["summary"], "the sentinel leaked into the summary")

    def test_compaction_happens_before_the_window_is_full(self):
        """At 100% occupancy a single large tool result tips the next turn over."""
        from agentaus_bridge.translate import estimate_request_tokens

        counter = _Counter()
        c = ConversationCompactor(counter, verify=False)
        body = {"messages": convo(40), "system": "s"}
        size = estimate_request_tokens(body)

        # A window the conversation fits inside with room to spare: at threshold 1.0
        # nothing should happen, but at 0.5 the trigger is below the current size and
        # the keep-budget is smaller than the conversation, so it must compact.
        limit = int(size * 1.6)
        under = run(c.compact(body, limit=limit, reserve=100, threshold=1.0))
        early = run(c.compact(body, limit=limit, reserve=100, threshold=0.5))

        self.assertEqual(under["method"], "none", "fits comfortably, should not compact")
        self.assertEqual(early["method"], "summarised", "threshold did not trigger early")

    def test_chunks_overlap_so_boundaries_are_not_lost(self):
        """A decision explained across a chunk boundary must reach one summariser whole."""
        from agentaus_bridge.compact import _chunk

        text = "\n".join(f"line {i}" for i in range(400))
        pieces = _chunk(text, 200, overlap_ratio=0.25)

        self.assertGreater(len(pieces), 1)
        tail_of_first = pieces[0].splitlines()[-5:]
        self.assertTrue(any(line in pieces[1] for line in tail_of_first),
                        "consecutive chunks share no context")

    def test_partial_summaries_are_merged_by_the_model(self):
        """Concatenating overlapping chunk summaries repeats itself."""
        prompts = []

        async def summariser(text: str) -> str:
            prompts.append(text)
            return "chunk summary"

        c = ConversationCompactor(summariser, verify=False)
        run(c.compact({"messages": convo(300), "system": "s"}, limit=4000, reserve=100))

        self.assertTrue(any("Merge them into a single coherent record" in p for p in prompts),
                        "partial summaries were concatenated rather than merged")


class TestChunkSizing(unittest.TestCase):
    """Chunks must fit the summariser's own context window.

    Regression: `render_for_summary` emits one line per message and a message can be
    tens of thousands of characters, so overlapping by a *line count* carried far more
    than intended. Chunks grew past the model's entire window, every summarisation call
    came back HTTP 400, and compaction silently degraded to dropping the history - the
    exact detail loss the summarising was added to prevent.
    """

    def test_no_chunk_exceeds_the_budget(self):
        from agentaus_bridge.compact import _chunk, render_for_summary

        msgs = [{"role": "user", "content": f"item {i}: " + "padding detail. " * 3000}
                for i in range(30)]
        budget = 32_000
        pieces = _chunk(render_for_summary(msgs), budget)

        oversized = [len(p) for p in pieces if len(p) > budget * 4 * 1.1]
        self.assertEqual(oversized, [], f"chunks exceed the budget: {oversized}")

    def test_chunk_count_does_not_explode(self):
        """The broken overlap also produced far more chunks than the text warranted."""
        from agentaus_bridge.compact import _chunk, render_for_summary

        msgs = [{"role": "user", "content": f"item {i}: " + "padding detail. " * 3000}
                for i in range(30)]
        text = render_for_summary(msgs)
        budget = 32_000
        pieces = _chunk(text, budget)

        # Generous ceiling: overlap means some redundancy, but not multiples.
        self.assertLess(len(pieces), (len(text) / (budget * 4)) * 2 + 2,
                        f"{len(pieces)} chunks for {len(text)} chars")

    def test_a_single_huge_message_is_split(self):
        from agentaus_bridge.compact import _chunk

        pieces = _chunk("x" * 500_000, 10_000)

        self.assertGreater(len(pieces), 1, "an oversized single line was emitted whole")
        self.assertTrue(all(len(p) <= 10_000 * 4 * 1.1 for p in pieces))

    def test_chunks_still_overlap(self):
        from agentaus_bridge.compact import _chunk

        text = "\n".join(f"line {i} " + "z" * 200 for i in range(400))
        pieces = _chunk(text, 2000, overlap_ratio=0.2)

        self.assertGreater(len(pieces), 1)
        tail = pieces[0].splitlines()[-3:]
        self.assertTrue(any(t in pieces[1] for t in tail), "overlap was lost in the fix")


class TestIncrementalCompaction(unittest.TestCase):
    """Cost across a *session*, not a single request.

    The regression this guards: the compaction boundary advanced by roughly one turn
    every turn, so the summary was keyed on something that never repeated and the cache
    never hit. Every turn re-summarised the entire history - measured at 173 seconds
    per turn on a real session, which is what made a plain "hello" look like a hang.
    """

    @staticmethod
    def session(turns: int) -> list:
        msgs = []
        for i in range(turns):
            msgs.append({"role": "user", "content": f"turn {i}: " + "detail " * 400})
            msgs.append({"role": "assistant", "content": f"reply {i}"})
        msgs.append({"role": "user", "content": "the current question"})
        return msgs

    def test_a_session_costs_far_less_than_re_summarising_every_turn(self):
        """What matters is total work across a session, not any single request.

        Two mechanisms contribute and both are wins: a quantised boundary makes some
        turns free outright, and prefix reuse makes the rest cost only the new
        messages. A representative run is [21, 0, 0, 0, 3, 2, 2, 2, 2, 2] - against
        21 per turn with neither, which is the behaviour that took 173 seconds a turn.
        """
        counter = _Counter()
        c = ConversationCompactor(counter, verify=False, block=20)

        per_turn = []
        for turns in range(40, 50):
            before = counter.calls
            run(c.compact({"messages": self.session(turns), "system": "s"},
                          limit=8_000, reserve=100))
            per_turn.append(counter.calls - before)

        cold = per_turn[0]
        naive_total = cold * len(per_turn)
        actual_total = sum(per_turn)

        self.assertGreater(cold, 0, "the first turn should do real work")
        self.assertLess(actual_total, naive_total / 3,
                        f"session cost {actual_total} vs {naive_total} naive: {per_turn}")
        later = per_turn[1:]
        self.assertTrue(all(n < cold for n in later),
                        f"a later turn cost as much as a cold summary: {per_turn}")

    def test_boundary_is_stable_across_turns(self):
        """An unquantised boundary moves every turn and defeats the cache."""
        from agentaus_bridge.compact import split_head_tail

        heads = set()
        for turns in range(40, 46):
            head, _ = split_head_tail(self.session(turns), 5000, block=20)
            heads.add(len(head))

        self.assertLess(len(heads), 6, f"boundary moved every turn: {sorted(heads)}")

    def test_extending_a_prefix_only_summarises_the_new_messages(self):
        counter = _Counter()
        c = ConversationCompactor(counter, verify=False, block=1)

        run(c.compact({"messages": self.session(40), "system": "s"},
                      limit=8_000, reserve=100))
        cold = counter.calls
        counter.calls = 0
        # A much longer session: without prefix reuse this costs as much again.
        run(c.compact({"messages": self.session(44), "system": "s"},
                      limit=8_000, reserve=100))

        self.assertLess(counter.calls, cold,
                        "extending the head cost as much as summarising it cold")

    def test_reused_summary_still_covers_the_whole_head(self):
        """Incremental reuse must not lose the earlier part of the conversation."""
        merged_inputs = []

        async def summariser(text: str) -> str:
            if text.startswith("The following are summaries"):
                merged_inputs.append(text)
                return "MERGED: " + text[-200:]
            return "PART"

        c = ConversationCompactor(summariser, verify=False, block=1)
        run(c.compact({"messages": self.session(40), "system": "s"},
                      limit=8_000, reserve=100))
        plan = run(c.compact({"messages": self.session(44), "system": "s"},
                             limit=8_000, reserve=100))

        self.assertTrue(merged_inputs, "the prior summary was never merged with the new part")
        self.assertIn("MERGED", plan["summary"])

    def test_a_changed_prefix_is_not_falsely_reused(self):
        """Reuse is keyed on content: an edited history must re-summarise."""
        counter = _Counter()
        c = ConversationCompactor(counter, verify=False, block=1)

        base = self.session(40)
        run(c.compact({"messages": base, "system": "s"}, limit=8_000, reserve=100))

        altered = [dict(m) for m in base]
        altered[0] = {"role": "user", "content": "COMPLETELY DIFFERENT " + "x " * 400}
        counter.calls = 0
        run(c.compact({"messages": altered, "system": "s"}, limit=8_000, reserve=100))

        self.assertGreater(counter.calls, 0, "a changed prefix was wrongly reused")
