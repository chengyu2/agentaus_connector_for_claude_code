"""A stalled upstream call should say so, not go quiet.

Observed live: a request was received, never forwarded upstream, and never answered.
The only evidence was that the log had stopped - which nobody notices while it is
happening, and which reads afterwards as "nothing was going on". A benchmark run sat in
that state for 26 minutes while the upstream itself was healthy and answering other
requests in under a second.
"""

import asyncio
import logging
import unittest

from agentaus_bridge import server
from agentaus_bridge.config import settings


def run(coroutine):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


class StallWatchdog(unittest.TestCase):
    def setUp(self):
        self.original = settings.stall_warning_seconds
        settings.stall_warning_seconds = 0.05

    def tearDown(self):
        settings.stall_warning_seconds = self.original

    def test_a_call_that_does_not_return_is_reported_while_it_is_happening(self):
        async def scenario():
            async with server._watching_for_stall("upstream POST"):
                await asyncio.sleep(0.2)

        with self.assertLogs("agentaus-bridge", level=logging.WARNING) as caught:
            run(scenario())
        self.assertTrue(any("still waiting" in line for line in caught.output))

    def test_the_warning_repeats_so_the_log_shows_how_long(self):
        async def scenario():
            async with server._watching_for_stall("upstream POST"):
                await asyncio.sleep(0.34)

        with self.assertLogs("agentaus-bridge", level=logging.WARNING) as caught:
            run(scenario())
        waits = [line for line in caught.output if "still waiting" in line]
        self.assertGreaterEqual(len(waits), 2, "one line cannot show a stall growing")

    def test_a_fast_call_is_not_announced(self):
        async def scenario():
            async with server._watching_for_stall("upstream POST"):
                await asyncio.sleep(0)
            logging.getLogger("agentaus-bridge").info("marker")

        with self.assertLogs("agentaus-bridge", level=logging.INFO) as caught:
            run(scenario())
        self.assertFalse(any("still waiting" in line for line in caught.output))

    def test_the_watcher_stops_when_the_call_finishes(self):
        """A leaked watcher would warn about a call that already returned."""
        async def scenario():
            async with server._watching_for_stall("upstream POST"):
                await asyncio.sleep(0)
            await asyncio.sleep(0.2)
            logging.getLogger("agentaus-bridge").info("marker")

        with self.assertLogs("agentaus-bridge", level=logging.INFO) as caught:
            run(scenario())
        self.assertFalse(any("still waiting" in line for line in caught.output))

    def test_an_exception_still_stops_the_watcher(self):
        async def scenario():
            try:
                async with server._watching_for_stall("upstream POST"):
                    raise RuntimeError("upstream blew up")
            except RuntimeError:
                pass
            await asyncio.sleep(0.2)
            logging.getLogger("agentaus-bridge").info("marker")

        with self.assertLogs("agentaus-bridge", level=logging.INFO) as caught:
            run(scenario())
        self.assertFalse(any("still waiting" in line for line in caught.output))


class ReadTimeout(unittest.TestCase):
    def test_the_read_budget_is_minutes_not_half_an_hour(self):
        """1800s meant a dead connection cost 26 minutes of silence.

        This is a per-read budget: streaming resets it on every token, so the only thing
        it actually bounds is how long the bridge waits having received nothing at all.
        """
        self.assertLessEqual(settings.read_timeout, 600)
        self.assertGreaterEqual(settings.read_timeout, 120,
                                "Agentaus web search legitimately takes a while")


if __name__ == "__main__":
    unittest.main()
