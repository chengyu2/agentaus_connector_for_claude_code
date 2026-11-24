"""The stall re-ask runs whether or not the answer is held back for review.

Measured (bench lean-alloff, codebase_qa): with AGENTAUS_SELF_REVIEW=false the reply
streams straight to the client, nothing is buffered, and the stall check read an empty
answer - so "Let me find the settings values next." ended the run with no answers file
written (score 0.00). The check reads what the model said, not what was held back.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import server  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402

CALLS: list = []
STALL = "Let me read the settings file next."
DONE = "The limit is 10 requests per second."


class _Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)))
        CALLS.append(body)
        if body.get("tools"):
            mains = sum(1 for c in CALLS if c.get("tools"))
            text = STALL if mains == 1 else DONE
        else:
            asked = body["messages"][-1]["content"]
            text = "ANSWER" if DONE in asked else "STALLED"
        if body.get("stream"):
            chunks = [{"choices": [{"index": 0, "delta": {"content": text}}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            ctype = "text/event-stream"
        else:
            data = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": text}}]})
            ctype = "application/json"
        raw = data.encode()
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class StallCheckWithoutBuffering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        HTTPServer.allow_reuse_address = True
        # Port 0: test_helper_latency.py runs its own stub, and pytest-xdist may run both files at once.
        cls.stub = HTTPServer(("127.0.0.1", 0), _Upstream)
        cls.port = cls.stub.server_address[1]
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        CALLS.clear()
        server._STALL_REASKS.clear()
        names = ("agentaus_base_url", "agentaus_api_key", "agentaus_self_review",
                 "agentaus_thinking", "agentaus_grounding_check")
        self._saved = {n: getattr(settings, n) for n in names}
        settings.agentaus_base_url = f"http://127.0.0.1:{self.port}"
        settings.agentaus_api_key = "test-key"
        settings.agentaus_self_review = False
        settings.agentaus_thinking = False
        settings.agentaus_grounding_check = False

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(settings, n, v)

    def test_an_announced_step_is_re_asked_when_nothing_is_buffered(self):
        from fastapi.testclient import TestClient
        body = {"model": "agentaus", "max_tokens": 300, "stream": True,
                "messages": [{"role": "user", "content": "What is the production rate limit?"}],
                "tools": [{"name": "Read", "description": "Read a file",
                           "input_schema": {"type": "object", "properties": {}}}]}
        with TestClient(server.app) as client:
            raw = client.post("/v1/messages", json=body).text
        mains = [c for c in CALLS if c.get("tools")]
        self.assertEqual(len(mains), 2, "the stalled reply was not re-asked")
        self.assertTrue(any(not c.get("tools") for c in CALLS), "the stall check never ran")
        # The re-ask carries what the model said, so it continues rather than restarts.
        said = [m for m in mains[1]["messages"] if m.get("role") == "assistant"]
        self.assertEqual(said[-1]["content"], STALL)
        self.assertIn(DONE, raw)


if __name__ == "__main__":
    unittest.main()
