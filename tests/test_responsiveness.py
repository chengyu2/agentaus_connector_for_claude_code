"""The bridge must keep answering while one turn does slow work.

Observed live: one `agentaus_search` over a tree that reached into application bundles
converted every RTF, spreadsheet and licence PDF inside them - LibreOffice and OCR, run
synchronously on the event loop. For 90 minutes the bridge received nothing: no pings
for the turn doing the work, no other Agentaus turns, and no Claude passthrough either.
A `claude-opus-5` request arrived at the start of that window and was never forwarded.
In VS Code that is indistinguishable from Claude Code itself hanging.

These tests pin the properties that fix it:

  * disk and conversion work runs off the event loop
  * a bridge tool has a deadline, and the deadline reaches the worker thread
  * a walk does not descend into macOS bundles and is capped in size
  * a streamed turn has a wall-clock ceiling that pings cannot defeat
  * a passthrough stream that dies mid-response ends with an error event, not a crash
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import documents, server, tools  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402


def run(coroutine):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


async def _no_model(_prompt: str) -> str:
    return ""


class _SlowDocuments:
    """Every .docx takes `delay` seconds to "convert", like LibreOffice does."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.reads = 0
        self._saved = None

    def __enter__(self) -> "_SlowDocuments":
        self._saved = (documents.available, documents.extract)

        def slow_extract(path: str) -> str:
            time.sleep(self.delay)
            self.reads += 1
            return f"text of {os.path.basename(path)}"

        documents.available = lambda path=None: True
        documents.extract = slow_extract
        return self

    def __exit__(self, *exc) -> None:
        documents.available, documents.extract = self._saved


def _tree(count: int, suffix: str = ".docx") -> str:
    root = tempfile.mkdtemp(prefix="agentaus-responsive-")
    for i in range(count):
        with open(os.path.join(root, f"doc{i:03d}{suffix}"), "w") as handle:
            handle.write("x")
    return root


class ToolsDoNotBlockTheLoop(unittest.TestCase):
    def test_other_coroutines_keep_running_while_an_inventory_reads_documents(self):
        root = _tree(6)

        async def scenario():
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            beat = asyncio.ensure_future(ticker())
            await tools.execute("agentaus_inventory", {"path": root}, _no_model)
            beat.cancel()
            return ticks

        with _SlowDocuments(0.1) as slow:
            ticks = run(scenario())
        self.assertEqual(slow.reads, 6)
        # 0.6s of conversion; a loop blocked for all of it would tick at most once.
        self.assertGreater(ticks, 10, "the event loop was blocked while files were read")

    def test_a_zoom_reads_its_file_off_the_loop(self):
        root = _tree(1)
        path = os.path.join(root, "doc000.docx")

        async def scenario():
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            beat = asyncio.ensure_future(ticker())
            result = await tools.execute(
                "agentaus_zoom", {"file_path": path, "start_line": 1}, _no_model)
            beat.cancel()
            return ticks, result

        with _SlowDocuments(0.3):
            ticks, result = run(scenario())
        self.assertRegex(result, r"(?i)passage")
        self.assertGreater(ticks, 5)


class ToolDeadline(unittest.TestCase):
    def setUp(self):
        self._saved = settings.agentaus_tool_timeout_seconds
        settings.agentaus_tool_timeout_seconds = 0.3

    def tearDown(self):
        settings.agentaus_tool_timeout_seconds = self._saved

    def test_a_runaway_tool_returns_a_result_saying_so(self):
        root = _tree(40)
        with _SlowDocuments(0.05):
            started = time.monotonic()
            result = run(tools.execute("agentaus_inventory", {"path": root}, _no_model))
            elapsed = time.monotonic() - started
        self.assertIn("stopped after", result)
        self.assertIn("narrow `path`", result)
        self.assertLess(elapsed, 1.5)

    def test_the_worker_thread_stops_too(self):
        # Cancelling the awaiting coroutine cannot stop a thread. Without the deadline
        # check inside the reads, this one would go on converting all 40 files.
        root = _tree(40)
        with _SlowDocuments(0.05) as slow:
            run(tools.execute("agentaus_inventory", {"path": root}, _no_model))
            time.sleep(0.4)
            settled = slow.reads
            time.sleep(0.3)
            self.assertEqual(slow.reads, settled, "the thread kept reading after the deadline")
        self.assertLess(settled, 40)

    def test_zero_disables_the_deadline(self):
        settings.agentaus_tool_timeout_seconds = 0
        root = _tree(3)
        with _SlowDocuments(0.01):
            result = run(tools.execute("agentaus_inventory", {"path": root}, _no_model))
        self.assertNotIn("stopped after", result)

    def test_the_deadline_does_not_leak_past_the_call(self):
        root = _tree(2)
        with _SlowDocuments(0.0):
            run(tools.execute("agentaus_inventory", {"path": root}, _no_model))
        self.assertIsNone(tools._deadline.get())


class WalkBounds(unittest.TestCase):
    def test_app_bundles_are_not_walked(self):
        root = tempfile.mkdtemp(prefix="agentaus-walk-")
        bundle = os.path.join(root, "LibreOffice.app", "Contents", "Resources")
        os.makedirs(bundle)
        with open(os.path.join(bundle, "Errors.txt"), "w") as handle:
            handle.write("installer text")
        with open(os.path.join(root, "notes.txt"), "w") as handle:
            handle.write("mine")
        self.assertEqual(
            [os.path.basename(f) for f in tools.enumerate_files(root)], ["notes.txt"]
        )

    def test_the_walk_is_capped_and_says_so(self):
        saved = settings.agentaus_search_max_files
        settings.agentaus_search_max_files = 5
        try:
            root = _tree(12, suffix=".txt")
            files, truncated = tools.enumerate_files_bounded(root)
            self.assertEqual(len(files), 5)
            self.assertTrue(truncated)
            listing = run(tools.execute("agentaus_inventory", {"path": root}, _no_model))
            self.assertIn("Stopped listing at 5 files", listing)
        finally:
            settings.agentaus_search_max_files = saved

    def test_a_small_tree_is_not_reported_as_truncated(self):
        root = _tree(3, suffix=".txt")
        files, truncated = tools.enumerate_files_bounded(root)
        self.assertEqual(len(files), 3)
        self.assertFalse(truncated)


class TurnDeadline(unittest.TestCase):
    def test_pings_cannot_keep_a_wedged_turn_open_forever(self):
        async def wedged():
            await asyncio.sleep(3600)
            yield b"never"

        async def scenario():
            out = []
            async for item in server._keepalive(wedged(), 0.05, lambda: b"ping",
                                                deadline=0.3):
                out.append(item)
            return out

        started = time.monotonic()
        out = run(scenario())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn(b"ping", out)
        last = out[-1].decode()
        self.assertIn("event: error", last)
        self.assertIn("BRIDGE_TURN_TIMEOUT", last)

    def test_a_turn_inside_the_deadline_is_untouched(self):
        async def quick():
            await asyncio.sleep(0.05)
            yield b"answer"

        async def scenario():
            return [item async for item in server._keepalive(
                quick(), 0.5, lambda: b"ping", deadline=5)]

        self.assertEqual(run(scenario()), [b"answer"])


# --------------------------------------------------------------------------------------
# Passthrough that dies mid-response
# --------------------------------------------------------------------------------------

PORT = 9947


class _DiesMidStream(BaseHTTPRequestHandler):
    """Stands in for api.anthropic.com: one SSE event, then the socket goes away."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        first = b'event: message_start\ndata: {"type":"message_start"}\n\n'
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        # Promise more than is sent, so the close is a truncated body rather than a
        # legitimate end of response.
        self.send_header("content-length", str(len(first) + 1000))
        self.end_headers()
        self.wfile.write(first)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args) -> None:
        pass


class PassthroughMidStream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HTTPServer.allow_reuse_address = True
        cls.stub = HTTPServer(("127.0.0.1", PORT), _DiesMidStream)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def test_the_client_gets_an_error_event_not_a_dropped_socket(self):
        from fastapi.testclient import TestClient

        saved = settings.anthropic_base_url
        settings.anthropic_base_url = f"http://127.0.0.1:{PORT}"
        try:
            with TestClient(server.app) as client:
                response = client.post(
                    "/v1/messages",
                    content=json.dumps({"model": "claude-opus-5", "stream": True,
                                        "messages": []}),
                    headers={"content-type": "application/json"},
                )
        finally:
            settings.anthropic_base_url = saved
        self.assertEqual(response.status_code, 200)
        self.assertIn("message_start", response.text)
        self.assertIn("event: error", response.text)
        self.assertIn("Retry the turn", response.text)


class DocumentCacheIsBounded(unittest.TestCase):
    def setUp(self):
        self._saved = settings.agentaus_document_cache_chars
        documents.reset_cache()

    def tearDown(self):
        settings.agentaus_document_cache_chars = self._saved
        documents.reset_cache()

    def test_least_recently_used_goes_first(self):
        settings.agentaus_document_cache_chars = 10
        documents._remember("a", "12345")
        documents._remember("b", "12345")
        self.assertEqual(documents._cached("a"), "12345")      # a is now most recent
        documents._remember("c", "12345")
        self.assertIsNone(documents._cached("b"))
        self.assertEqual(documents._cached("a"), "12345")
        self.assertEqual(documents._cached("c"), "12345")

    def test_an_entry_larger_than_the_cache_evicts_nothing(self):
        settings.agentaus_document_cache_chars = 10
        documents._remember("a", "12345")
        documents._remember("huge", "x" * 50)
        self.assertEqual(documents._cached("a"), "12345")
        self.assertIsNone(documents._cached("huge"))


if __name__ == "__main__":
    unittest.main()
