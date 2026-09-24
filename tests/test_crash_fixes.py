"""Failures that ended Agentaus turns in Claude Code, found in a day of benchmark logs.

Two remained after the event-loop fix, across ~3,000 Agentaus requests:

* Agentaus answered HTTP 200 and then reported, inside the stream, that its own backend
  had dropped - "peer closed connection without sending complete message body
  (incomplete chunked read)", 20 times. The bridge treated it as final and ended the
  turn; Claude Code recovered only by re-sending the whole request without streaming.
* A search handed `/` by a model that did not know its working directory walked into
  /Library/Apple and sent installer receipts (`.bom`, binary) to Agentaus as text; each
  chunk came back HTTP 400.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from agentaus_bridge import server, tools  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _no_model(_prompt):
    return "NONE"


class BinaryFiles(unittest.TestCase):
    def test_a_binary_file_is_not_read_as_text(self):
        with tempfile.TemporaryDirectory() as tree:
            with open(os.path.join(tree, "receipt.bom"), "wb") as fh:
                fh.write(b"BOMStore\x00\x00\x00\x01" + bytes(range(256)) * 20)
            with open(os.path.join(tree, "notes.txt"), "w") as fh:
                fh.write("plain text")
            names = [os.path.basename(f) for f in tools.enumerate_files(tree)]
            self.assertEqual(names, ["notes.txt"])
            self.assertEqual(tools.read_text(os.path.join(tree, "receipt.bom")), "")

    def test_text_with_odd_bytes_is_still_text(self):
        with tempfile.TemporaryDirectory() as tree:
            path = os.path.join(tree, "latin1.txt")
            with open(path, "wb") as fh:
                fh.write("café résumé".encode("latin-1"))
            self.assertFalse(tools.looks_binary(path))


class WholeDisk(unittest.TestCase):
    def test_search_and_inventory_refuse_the_root(self):
        for tool, args in (("agentaus_search", {"query": "x", "path": "/"}),
                           ("agentaus_inventory", {"path": "/"})):
            with self.subTest(tool=tool):
                out = run(tools.execute(tool, args, _no_model))
                self.assertIn("is the whole disk", out)

    def test_system_trees_are_not_walked_from_the_root(self):
        self.assertTrue(tools._skip_dir("/", "Library"))
        self.assertTrue(tools._skip_dir("/", "System"))
        self.assertFalse(tools._skip_dir("/Users/me/project", "Library"))


class Transient(unittest.TestCase):
    def test_what_counts(self):
        self.assertTrue(server._is_transient(
            "peer closed connection without sending complete message body (incomplete chunked read)"))
        self.assertTrue(server._is_transient("HTTP 524 origin timed out"))
        self.assertFalse(server._is_transient("invalid tool schema"))
        self.assertFalse(server._is_transient(
            "The engine prompt length 224662 exceeds the max_model_len 131072"))


PORT = 9961
STATE = {"calls": 0, "mode": "stream"}
DROP = "peer closed connection without sending complete message body (incomplete chunked read)"


class _DropsOnce(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)))
        STATE["calls"] += 1
        first = STATE["calls"] == 1
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            if first:
                chunk = {"error": {"message": DROP, "type": "api_error"}}
            else:
                chunk = {"choices": [{"index": 0, "delta": {"content": "recovered"},
                                      "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        data = ({"error": {"message": DROP}} if first else
                {"choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": "recovered"}}]})
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class InBandDropsAreRetried(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HTTPServer.allow_reuse_address = True
        cls.stub = HTTPServer(("127.0.0.1", PORT), _DropsOnce)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        STATE["calls"] = 0
        self._saved = (settings.agentaus_base_url, settings.agentaus_api_key,
                       settings.retry_backoff_seconds, settings.agentaus_self_review)
        settings.agentaus_base_url = f"http://127.0.0.1:{PORT}"
        settings.agentaus_api_key = "test-key"
        settings.retry_backoff_seconds = 0.01
        settings.agentaus_self_review = False

    def tearDown(self):
        (settings.agentaus_base_url, settings.agentaus_api_key,
         settings.retry_backoff_seconds, settings.agentaus_self_review) = self._saved

    def test_a_streamed_turn_recovers_without_the_client_seeing_an_error(self):
        async def go():
            async with httpx.AsyncClient() as client:
                body = {"model": "agentaus", "messages": [{"role": "user", "content": "q"}]}
                out = b""
                async for chunk in server._agentaus_event_stream(
                        client, {"messages": [], "stream": True}, body, "agentaus", 0.0):
                    out += chunk
                return out.decode()
        text = run(go())
        self.assertEqual(STATE["calls"], 2)
        self.assertNotIn("event: error", text)
        self.assertIn("recovered", text)

    def test_a_buffered_call_recovers_too(self):
        async def go():
            async with httpx.AsyncClient() as client:
                r = await server._post_with_retry(client, settings.agentaus_url,
                                                  json_body={"messages": []}, headers={})
                return r.json()
        data = run(go())
        self.assertEqual(STATE["calls"], 2)
        self.assertEqual(data["choices"][0]["message"]["content"], "recovered")

    def test_a_real_error_is_still_reported(self):
        STATE["calls"] = 1          # skip the drop; stub now answers normally
        self.assertFalse(server._is_transient("tools[3].function.parameters is invalid"))


if __name__ == "__main__":
    unittest.main()
