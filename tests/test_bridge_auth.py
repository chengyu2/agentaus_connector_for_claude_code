"""BRIDGE_TOKEN must gate other machines (the phone) but never this machine.

Claude Code on this machine sends its Anthropic credentials in x-api-key /
Authorization for passthrough, so it cannot also present the bridge token. Before
loopback was trusted, turning on the token made every local request a 401.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.requests import Request  # noqa: E402


def _request(host: str, headers: dict[str, str]) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (host, 50000),
    })


class TestBridgeAuth(unittest.TestCase):
    def setUp(self):
        from agentaus_bridge.config import settings
        self.settings = settings
        self.old = settings.bridge_token
        settings.bridge_token = "s3cret"

    def tearDown(self):
        self.settings.bridge_token = self.old

    def check(self, host, headers):
        from agentaus_bridge import server
        return server._client_authorised(_request(host, headers))

    def test_local_claude_code_with_anthropic_key_is_allowed(self):
        self.assertTrue(self.check("127.0.0.1", {"x-api-key": "sk-ant-something"}))
        self.assertTrue(self.check("::1", {"authorization": "Bearer oauth-token"}))

    def test_remote_needs_the_token(self):
        self.assertFalse(self.check("192.168.15.20", {}))
        self.assertFalse(self.check("192.168.15.20", {"authorization": "Bearer wrong"}))
        self.assertTrue(self.check("192.168.15.20", {"authorization": "Bearer s3cret"}))
        self.assertTrue(self.check("192.168.15.20", {"x-api-key": "s3cret"}))

    def test_dedicated_header(self):
        self.assertTrue(self.check("192.168.15.20", {"x-bridge-token": "s3cret",
                                                     "x-api-key": "sk-ant-other"}))
        self.assertFalse(self.check("192.168.15.20", {"x-bridge-token": "nope"}))

    def test_no_token_means_open(self):
        self.settings.bridge_token = ""
        self.assertTrue(self.check("192.168.15.20", {}))


if __name__ == "__main__":
    unittest.main()
