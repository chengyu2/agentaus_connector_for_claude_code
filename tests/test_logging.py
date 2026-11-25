"""Logging must be sufficient to diagnose a stall without guessing.

This exists because it was not. A plain "hello" on a large session produced no
response, and the log showed only sixty identical "200 OK" lines: no request arrival,
no phase timings, no way to tell a summarisation call from the real turn, and complete
silence when the client gave up. The cause had to be inferred from an *absence* of
lines rather than read from them.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import server  # noqa: E402
from agentaus_bridge import request_log  # noqa: E402

_SPLIT = ("server", "request_log", "bridge_responses", "upstream_http", "passthrough",
          "conversation_text", "helper_calls", "graph_turn", "tool_handling",
          "stall_check", "context_window", "stream_delivery", "agentaus_stream",
          "agentaus_turn")


def _bridge_source() -> str:
    """server.py and the modules split out of it: the request path spans all of them."""
    folder = os.path.dirname(server.__file__)
    return "".join(open(os.path.join(folder, f"{name}.py")).read() for name in _SPLIT)


class TestRequestScopedLogging(unittest.TestCase):
    def test_every_request_path_log_call_carries_a_request_id(self):
        """A line without an id cannot be tied to the turn that produced it."""
        source = _bridge_source()
        bare = re.findall(r"^\s*log\.(?:info|warning|error)\(", source, re.M)

        # Only the three startup lines may be bare: they belong to no request.
        self.assertLessEqual(len(bare), 3,
                             f"{len(bare)} request-path log calls have no request id")

    def test_log_helper_prefixes_the_id(self):
        with self.assertLogs("agentaus-bridge", level="INFO") as captured:
            token = request_log._request_id.set("abcd1234")
            try:
                request_log.rlog(logging.INFO, "something happened")
            finally:
                request_log._request_id.reset(token)

        self.assertIn("req abcd1234 something happened", captured.output[0])

    def test_ids_are_distinct_per_request(self):
        self.assertNotEqual(request_log._new_request_id(), request_log._new_request_id())


class TestPhaseLogging(unittest.TestCase):
    def test_a_phase_logs_both_start_and_end(self):
        """A start with no matching end is the signature of a hang - which is only
        visible if the start was logged at all."""
        with self.assertLogs("agentaus-bridge", level="INFO") as captured:
            with request_log._Phase("compaction", "est 400,000 tok"):
                pass

        joined = "\n".join(captured.output)
        self.assertIn("compaction start", joined)
        self.assertIn("est 400,000 tok", joined, "the start line must carry the detail")
        self.assertRegex(joined, r"compaction done in \d+\.\d+s")

    def test_a_failed_phase_names_the_exception_type(self):
        """CancelledError carries no message; "ended after 0.0s:" says nothing."""
        with self.assertLogs("agentaus-bridge", level="WARNING") as captured:
            try:
                with request_log._Phase("compaction"):
                    raise __import__("asyncio").CancelledError()
            except BaseException:
                pass

        self.assertIn("CancelledError", "\n".join(captured.output))

    def test_a_failed_phase_still_reports_its_duration(self):
        with self.assertLogs("agentaus-bridge", level="WARNING") as captured:
            try:
                with request_log._Phase("compaction"):
                    raise RuntimeError("upstream refused")
            except RuntimeError:
                pass

        joined = "\n".join(captured.output)
        self.assertIn("upstream refused", joined)
        self.assertRegex(joined, r"ended after \d+\.\d+s")


class TestDiagnosticCoverage(unittest.TestCase):
    """The specific things whose absence made the original stall unreadable."""

    def setUp(self) -> None:
        self.source = _bridge_source()

    def test_requests_are_logged_on_arrival_not_only_on_completion(self):
        """A hung request never completes, so a completion-only log never mentions it.

        This is precisely what hid the stall: the turn was logged only when it
        finished, and it never finished.
        """
        self.assertIn('rlog(\n        logging.INFO,\n        "recv model=', self.source)
        self.assertIn("bytes=%d", self.source,
                      "arrival should record the payload size")
        self.assertIn("msgs=%d", self.source,
                      "arrival should record how many messages were sent")

    def test_client_disconnects_are_logged(self):
        self.assertIn("client disconnected after", self.source,
                      "an abandoned turn must not look like one still running")

    def test_compaction_is_timed(self):
        self.assertIn('_Phase("compaction"', self.source,
                      "the slowest phase must report its own duration")

    def test_passthrough_is_logged(self):
        """Claude-model turns were previously invisible in the log entirely."""
        self.assertIn("passthrough -> ", self.source)


if __name__ == "__main__":
    unittest.main()
