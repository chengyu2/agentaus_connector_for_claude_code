"""Automatic recovery when a conversation outgrows Agentaus' 131k window.

The bridge cannot drive Claude Code's UI - selecting a model and running /compact
are client-side - so the only recovery it can perform is to make the oversized
request itself succeed, by dropping the oldest messages.

The correctness risk is structural: Anthropic messages pair a `tool_use` block in an
assistant turn with a `tool_result` block in the following user turn. Cutting between
them leaves an orphaned result that Agentaus rejects, so trimming must always land on
a clean user turn.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge.translate import trim_messages_to_fit  # noqa: E402


def _big(n: int) -> str:
    return "word " * n


class TestTrimming(unittest.TestCase):
    def test_nothing_dropped_when_it_already_fits(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        kept, dropped = trim_messages_to_fit(body, 131072)

        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 1)

    def test_oldest_messages_are_dropped_until_it_fits(self):
        body = {"messages": [
            {"role": "user", "content": _big(2000)},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "the actual question"},
        ]}
        kept, dropped = trim_messages_to_fit(body, 500)

        self.assertGreater(dropped, 0)
        self.assertEqual(kept[-1]["content"], "the actual question",
                         "the message being answered must survive")

    def test_result_is_never_left_orphaned(self):
        """Cutting between a tool_use and its tool_result produces an invalid request."""
        body = {"messages": [
            {"role": "user", "content": _big(3000)},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "file body"}]},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "next question"},
        ]}
        kept, dropped = trim_messages_to_fit(body, 400)

        self.assertGreater(dropped, 0)
        first = kept[0]
        self.assertEqual(first["role"], "user")
        if isinstance(first.get("content"), list):
            self.assertFalse(
                any(b.get("type") == "tool_result" for b in first["content"]),
                "conversation begins with an orphaned tool_result",
            )

    def test_every_kept_tool_result_still_has_its_tool_use(self):
        body = {"messages": [
            {"role": "user", "content": _big(2000)},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_A", "name": "Read", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_A", "content": "x"}]},
            {"role": "user", "content": "question"},
        ]}
        kept, _ = trim_messages_to_fit(body, 300)

        seen_ids, orphans = set(), []
        for m in kept:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    seen_ids.add(b.get("id"))
                elif b.get("type") == "tool_result" and b.get("tool_use_id") not in seen_ids:
                    orphans.append(b.get("tool_use_id"))
        self.assertEqual(orphans, [], f"orphaned tool_results: {orphans}")

    def test_reserve_for_the_reply_is_counted(self):
        body = {"messages": [
            {"role": "user", "content": _big(200)},
            {"role": "user", "content": "q"},
        ]}
        _, without = trim_messages_to_fit(body, 1000, reserve=0)
        _, with_reserve = trim_messages_to_fit(body, 1000, reserve=900)

        self.assertGreaterEqual(with_reserve, without,
                                "reserving reply space must trim at least as hard")

    def test_impossible_case_reports_rather_than_corrupts(self):
        """One message too large to ever fit: keep it and let the caller reject."""
        body = {"messages": [{"role": "user", "content": _big(50_000)}]}
        kept, _ = trim_messages_to_fit(body, 100)

        self.assertEqual(len(kept), 1, "must not return an empty conversation")

    def test_disabled_when_limit_is_zero(self):
        body = {"messages": [{"role": "user", "content": _big(9999)}]}
        kept, dropped = trim_messages_to_fit(body, 0)

        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
