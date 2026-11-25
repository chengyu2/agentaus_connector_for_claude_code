"""Long-chat warning (LONG_CHAT_WARN_RATIO).

When a main-loop reply to the user is estimated at or above the configured fraction
of Agentaus' context window, the bridge appends one line telling them to start a
fresh chat. It never fires for a subagent, never for Claude Code's own tool-less
calls (title, summary), and at most once every 10 eligible turns per session.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import server  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge import sessions  # noqa: E402

LIMIT = 131072


class _Headers:
    """A minimal request stand-in: only `.headers` is read by the warning gate."""

    def __init__(self, headers=None):
        self.headers = headers or {"x-claude-code-session-id": "sess"}


class TestLongChatWarning(unittest.TestCase):
    def setUp(self):
        self.old = settings.long_chat_warn_ratio
        settings.long_chat_warn_ratio = 0.8
        sessions.reset()

    def tearDown(self):
        settings.long_chat_warn_ratio = self.old
        sessions.reset()

    def body(self, *, tools=True, subagent=False):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        if tools:
            body["tools"] = [{"name": "agentaus_search"}]
        if subagent:
            body["system"] = "cc_is_subagent=true billing line"
        return body

    def pct(self, estimate, *, tools=True, subagent=False):
        return server._long_chat_warning_pct(
            _Headers(), self.body(tools=tools, subagent=subagent), estimate, LIMIT
        )

    def test_below_threshold_no_warning(self):
        self.assertIsNone(self.pct(int(LIMIT * 0.5)))

    def test_above_threshold_warns_then_only_once_every_10_turns(self):
        # First over-threshold turn warns.
        self.assertIsNotNone(self.pct(int(LIMIT * 0.9)))
        # The next nine eligible turns are throttled.
        for _ in range(9):
            self.assertIsNone(self.pct(int(LIMIT * 0.9)))
        # The eleventh overall warns again.
        self.assertIsNotNone(self.pct(int(LIMIT * 0.9)))

    def test_subagent_call_no_warning(self):
        self.assertIsNone(self.pct(int(LIMIT * 0.99), subagent=True))

    def test_toolless_title_summary_call_no_warning(self):
        self.assertIsNone(self.pct(int(LIMIT * 0.99), tools=False))

    def test_zero_ratio_disables(self):
        settings.long_chat_warn_ratio = 0
        self.assertIsNone(self.pct(int(LIMIT * 0.99)))

    def test_wording_and_percentage(self):
        text = server._long_chat_warning(85)
        self.assertIn("Start a new one with + for faster replies.", text)
        self.assertIn("about 85%", text)


if __name__ == "__main__":
    unittest.main()
