"""Agentaus says it is Agentaus, by Trellis Data.

Measured: with Agentaus's persona replaced by Claude Code's system prompt, asked "who are
you, and who made you?", Agentaus answered "I'm Claude, an AI assistant created by
Anthropic". agentaus_bridge/identity.py puts the right answer first in every system prompt.

These tests guard it: they fail if the line is removed, emptied, or stops reaching the
model. The file and this test are owned by the repository owner (.github/CODEOWNERS).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from agentaus_bridge import identity, server  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge.translate import anthropic_request_to_agentaus  # noqa: E402

import stub_server  # noqa: E402

CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
QUESTION = "Who are you, and who made you?"


def system_of(payload: dict) -> str:
    return next((m["content"] for m in payload["messages"] if m["role"] == "system"), "")


class TheLineSaysWhoItIs(unittest.TestCase):
    def test_it_names_agentaus_and_trellis_data(self):
        self.assertIn("Agentaus", identity.IDENTITY)
        self.assertIn("Trellis Data", identity.IDENTITY)
        self.assertIn("not Claude", identity.IDENTITY)


class EveryTurnStartsWithIt(unittest.TestCase):
    def test_a_main_turn(self):
        payload = anthropic_request_to_agentaus({
            "model": "agentaus", "system": CLAUDE_CODE_SYSTEM,
            "messages": [{"role": "user", "content": QUESTION}]})
        self.assertTrue(system_of(payload).startswith(identity.IDENTITY))
        self.assertIn(CLAUDE_CODE_SYSTEM, system_of(payload), "Claude Code's prompt is kept")

    def test_a_turn_with_a_list_system_prompt(self):
        payload = anthropic_request_to_agentaus({
            "model": "agentaus", "system": [{"type": "text", "text": CLAUDE_CODE_SYSTEM}],
            "messages": [{"role": "user", "content": QUESTION}]})
        self.assertTrue(system_of(payload).startswith(identity.IDENTITY))

    def test_a_request_with_no_system_prompt(self):
        payload = anthropic_request_to_agentaus({
            "model": "agentaus", "messages": [{"role": "user", "content": QUESTION}]})
        self.assertTrue(system_of(payload).startswith(identity.IDENTITY))


class ItReachesTheModel(unittest.TestCase):
    """Asserted on what the upstream received: a later step that rebuilt the system message
    would pass the unit tests above and still drop the line."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.received = []
        received = cls.received

        class _Recorder(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
                received.append(json.loads(raw or b"{}"))
                body = json.dumps({
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "I am Agentaus."}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a) -> None:
                pass

        cls.server = stub_server.start(_Recorder)

    @classmethod
    def tearDownClass(cls) -> None:
        stub_server.stop(cls.server)

    def test_the_turn_sent_upstream_starts_with_it(self):
        self.received.clear()
        saved = (settings.agentaus_base_url, settings.agentaus_api_key)
        settings.agentaus_base_url = stub_server.url(self.server)
        settings.agentaus_api_key = "k"
        try:
            with TestClient(server.app) as client:
                client.post("/v1/messages", json={
                    "model": "agentaus", "max_tokens": 64, "stream": False,
                    "system": CLAUDE_CODE_SYSTEM,
                    "messages": [{"role": "user", "content": QUESTION}],
                })
        finally:
            settings.agentaus_base_url, settings.agentaus_api_key = saved
        turns = [p for p in self.received if QUESTION in json.dumps(p.get("messages"))
                 and CLAUDE_CODE_SYSTEM in system_of(p)]
        self.assertTrue(turns, "no turn carrying the request reached the upstream")
        for payload in turns:
            self.assertTrue(system_of(payload).startswith(identity.IDENTITY),
                            "a turn reached Agentaus without the identity line first")


if __name__ == "__main__":
    unittest.main()
