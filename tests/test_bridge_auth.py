"""Bridge authentication and credential passthrough.

The failures these guard against, each confirmed on the bridge before the fix:

- BRIDGE_TOKEN guarded only /v1/messages: /v1/messages/count_tokens and /v1/models
  answered unauthenticated callers.
- The credential a client authenticated to the bridge with (x-api-key / Bearer) was
  forwarded to Anthropic byte-for-byte, so bridge auth and passthrough fought over the
  same header and the bridge token could be handed to Anthropic.
- /v1/models kept one global cache of Anthropic's last list and served it to any caller
  - including one with no Anthropic credential at all - whoever had fetched it last.
- The token comparison was `==`, timing-observable.

The design: clients authenticate with the dedicated `x-agentaus-bridge-token` header
(preferred) or the legacy x-api-key / Bearer forms; whichever header actually holds the
bridge token is stripped before anything goes to Anthropic, while a separate real
Anthropic credential survives; and the model-list fallback cache is per credential.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import stub_server  # noqa: E402

BRIDGE_TOKEN = "bridge-secret-e11a"
ANTHROPIC_KEY = "sk-real-anthropic-credential"
ANTHROPIC_OAUTH = "oauth-real-token"
LIVE_A = [
    {"id": "claude-opus-a", "type": "model", "display_name": "Opus A", "created_at": "2027-01-01T00:00:00Z"},
]
LIVE_B = [
    {"id": "claude-sonnet-b", "type": "model", "display_name": "Sonnet B", "created_at": "2027-01-01T00:00:00Z"},
]

STATE = {"status": 200, "delay": 0.0, "seen": []}


class _Anthropic(BaseHTTPRequestHandler):
    """Stands in for api.anthropic.com: /v1/models on GET, /v1/messages on POST.

    The model list it returns depends on the credential, so a test can tell a live
    answer from a cached one.
    """

    def do_GET(self) -> None:
        STATE["seen"].append({"method": "GET", "path": self.path,
                              "headers": {k.lower(): v for k, v in self.headers.items()}})
        key = STATE["seen"][-1]["headers"].get("x-api-key")
        data = LIVE_A if key == ANTHROPIC_KEY else LIVE_B
        body = json.dumps({"data": data, "has_more": False}).encode()
        self.send_response(STATE["status"])
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        STATE["seen"].append({"method": "POST", "path": self.path,
                              "headers": {k.lower(): v for k, v in self.headers.items()}})
        body = json.dumps({
            "id": "msg_stub", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()
        self.send_response(STATE["status"])
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class BridgeAuth(unittest.TestCase):
    server: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = stub_server.start(_Anthropic)

    @classmethod
    def tearDownClass(cls) -> None:
        stub_server.stop(cls.server)

    def setUp(self) -> None:
        from agentaus_bridge import server
        from agentaus_bridge.config import settings
        STATE.update({"status": 200, "delay": 0.0, "seen": []})
        server._MODELS_LAST.clear()
        self._saved = (settings.bridge_token, settings.anthropic_base_url)
        settings.bridge_token = BRIDGE_TOKEN
        settings.anthropic_base_url = stub_server.url(self.server)

    def tearDown(self) -> None:
        from agentaus_bridge.config import settings
        settings.bridge_token, settings.anthropic_base_url = self._saved

    # -- helpers ----------------------------------------------------------------------

    def client(self):
        from agentaus_bridge import server
        return TestClient(server.app)

    @staticmethod
    def messages_body() -> dict:
        return {"model": "claude-sonnet-5", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}]}

    @staticmethod
    def count_body() -> dict:
        return {"model": "claude-sonnet-5",
                "messages": [{"role": "user", "content": "hi"}]}

    @staticmethod
    def bridge_header() -> dict:
        from agentaus_bridge import server
        return {server.BRIDGE_TOKEN_HEADER: BRIDGE_TOKEN}

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return [m["id"] for m in response.json()["data"]]

    def last_upstream(self) -> dict:
        return STATE["seen"][-1]

    # -- the guard covers all three endpoints -----------------------------------------

    def test_without_a_token_all_three_endpoints_refuse(self):
        with self.client() as client:
            self.assertEqual(client.post("/v1/messages", json=self.messages_body()).status_code, 401)
            self.assertEqual(
                client.post("/v1/messages/count_tokens", json=self.count_body()).status_code, 401)
            self.assertEqual(client.get("/v1/models").status_code, 401)
        self.assertEqual(STATE["seen"], [], "an unauthenticated caller reached Anthropic")

    def test_a_wrong_token_is_refused_on_all_three(self):
        wrong = {"x-agentaus-bridge-token": "not-the-token"}
        with self.client() as client:
            self.assertEqual(
                client.post("/v1/messages", json=self.messages_body(), headers=wrong).status_code, 401)
            self.assertEqual(
                client.post("/v1/messages/count_tokens", json=self.count_body(),
                            headers=wrong).status_code, 401)
            self.assertEqual(client.get("/v1/models", headers=wrong).status_code, 401)

    def test_the_dedicated_header_authorises_all_three(self):
        with self.client() as client:
            self.assertEqual(
                client.post("/v1/messages", json=self.messages_body(),
                            headers=self.bridge_header()).status_code, 200)
            self.assertEqual(
                client.post("/v1/messages/count_tokens", json=self.count_body(),
                            headers=self.bridge_header()).status_code, 200)
            self.assertEqual(
                client.get("/v1/models", headers=self.bridge_header()).status_code, 200)

    def test_legacy_x_api_key_and_bearer_still_authorise_all_three(self):
        with self.client() as client:
            for headers in ({"x-api-key": BRIDGE_TOKEN},
                            {"authorization": f"Bearer {BRIDGE_TOKEN}"}):
                self.assertEqual(
                    client.post("/v1/messages", json=self.messages_body(),
                                headers=headers).status_code, 200)
                self.assertEqual(
                    client.post("/v1/messages/count_tokens", json=self.count_body(),
                                headers=headers).status_code, 200)
                self.assertEqual(client.get("/v1/models", headers=headers).status_code, 200)

    def test_no_bridge_token_configured_leaves_the_bridge_open(self):
        from agentaus_bridge.config import settings
        settings.bridge_token = ""
        with self.client() as client:
            self.assertEqual(
                client.post("/v1/messages/count_tokens", json=self.count_body()).status_code, 200)
            self.assertEqual(client.get("/v1/models").status_code, 200)

    def test_health_endpoints_stay_open(self):
        with self.client() as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertEqual(client.get("/healthz/upstream").status_code, 200)
            self.assertEqual(client.get("/api/hello").status_code, 200)

    # -- the bridge credential never reaches Anthropic --------------------------------

    def test_dedicated_header_and_a_real_key_pass_the_real_key_only(self):
        headers = {**self.bridge_header(), "x-api-key": ANTHROPIC_KEY}
        with self.client() as client:
            response = client.post("/v1/messages", json=self.messages_body(), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        upstream = self.last_upstream()
        self.assertEqual(upstream["headers"].get("x-api-key"), ANTHROPIC_KEY)
        self.assertNotIn(BRIDGE_TOKEN, json.dumps(upstream["headers"]),
                         "the bridge token leaked to Anthropic")
        from agentaus_bridge import server
        self.assertNotIn(server.BRIDGE_TOKEN_HEADER, upstream["headers"])

    def test_legacy_bridge_x_api_key_is_dropped_and_a_separate_bearer_survives(self):
        headers = {"x-api-key": BRIDGE_TOKEN, "authorization": f"Bearer {ANTHROPIC_OAUTH}"}
        with self.client() as client:
            response = client.post("/v1/messages", json=self.messages_body(), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        upstream = self.last_upstream()
        self.assertNotIn("x-api-key", upstream["headers"],
                         "the bridge credential was forwarded as x-api-key")
        self.assertEqual(upstream["headers"].get("authorization"), f"Bearer {ANTHROPIC_OAUTH}",
                         "the caller's real credential was lost")
        self.assertNotIn(BRIDGE_TOKEN, json.dumps(upstream["headers"]))

    def test_legacy_bridge_bearer_is_dropped_and_a_separate_key_survives(self):
        # Authenticated with the dedicated header; the legacy Bearer header carries the
        # bridge token too, and must not carry it to Anthropic.
        from agentaus_bridge import server
        headers = {server.BRIDGE_TOKEN_HEADER: BRIDGE_TOKEN,
                   "authorization": f"Bearer {BRIDGE_TOKEN}", "x-api-key": ANTHROPIC_KEY}
        with self.client() as client:
            response = client.post("/v1/messages", json=self.messages_body(), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        upstream = self.last_upstream()
        self.assertEqual(upstream["headers"].get("x-api-key"), ANTHROPIC_KEY)
        self.assertNotIn("authorization", upstream["headers"])
        self.assertNotIn(BRIDGE_TOKEN, json.dumps(upstream["headers"]))

    def test_a_legacy_client_with_no_other_credential_forwards_no_credential(self):
        """Legacy Agentaus-only clients auth with the bridge token alone. It must not be
        sent to Anthropic as if it were one; Anthropic's own 401 is the honest answer."""
        with self.client() as client:
            response = client.post("/v1/messages", json=self.messages_body(),
                                   headers={"x-api-key": BRIDGE_TOKEN})
        self.assertEqual(response.status_code, 200, response.text)
        upstream = self.last_upstream()
        self.assertNotIn("x-api-key", upstream["headers"])
        self.assertNotIn("authorization", upstream["headers"])

    def test_count_tokens_passthrough_is_sanitised_too(self):
        headers = {**self.bridge_header(), "x-api-key": ANTHROPIC_KEY}
        with self.client() as client:
            response = client.post("/v1/messages/count_tokens", json=self.count_body(),
                                   headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        upstream = self.last_upstream()
        self.assertEqual(upstream["headers"].get("x-api-key"), ANTHROPIC_KEY)
        self.assertNotIn(BRIDGE_TOKEN, json.dumps(upstream["headers"]))

    # -- model discovery is sanitised and credential-isolated --------------------------

    def test_model_discovery_forwards_the_real_credential_and_no_bridge_header(self):
        from agentaus_bridge import server
        headers = {**self.bridge_header(), "x-api-key": ANTHROPIC_KEY,
                   "anthropic-beta": "oauth-2025-04-20"}
        with self.client() as client:
            ids = self.ids(client.get("/v1/models?limit=1000", headers=headers))
        self.assertEqual(ids, ["claude-opus-a", "agentaus"])
        upstream = self.last_upstream()
        self.assertEqual(upstream["path"], "/v1/models?limit=1000")
        self.assertEqual(upstream["headers"].get("x-api-key"), ANTHROPIC_KEY)
        self.assertEqual(upstream["headers"].get("anthropic-beta"), "oauth-2025-04-20")
        self.assertEqual(upstream["headers"].get("anthropic-version"), "2023-06-01")
        self.assertNotIn(server.BRIDGE_TOKEN_HEADER, upstream["headers"])

    def test_model_discovery_drops_a_bridge_valued_legacy_credential(self):
        with self.client() as client:
            # Authorised via the dedicated header; the legacy header carries the token too.
            headers = {**self.bridge_header(), "x-api-key": BRIDGE_TOKEN}
            response = client.get("/v1/models", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(STATE["seen"], [],
                         "the bridge token was forwarded to Anthropic as a credential")

    def test_the_cached_fallback_is_per_credential(self):
        with self.client() as client:
            bridge = self.bridge_header()
            # A's list is fetched live and cached for A.
            self.ids(client.get("/v1/models", headers={**bridge, "x-api-key": ANTHROPIC_KEY}))
            # Upstream goes down.
            STATE["status"] = 500
            # A still gets its own last list.
            self.assertEqual(
                self.ids(client.get("/v1/models", headers={**bridge, "x-api-key": ANTHROPIC_KEY})),
                ["claude-opus-a", "agentaus"])
            # A different credential is never served A's list.
            self.assertEqual(
                self.ids(client.get("/v1/models",
                                    headers={**bridge, "authorization": f"Bearer {ANTHROPIC_OAUTH}"})),
                ["agentaus"])
            # Nor is a caller with no Anthropic credential at all.
            self.assertEqual(self.ids(client.get("/v1/models", headers=bridge)), ["agentaus"])
        # Every request that reached Anthropic carried a credential; the no-credential
        # caller never asked, and only A's authorised fetch ever got a 200 list.
        self.assertTrue(STATE["seen"])
        for seen in STATE["seen"]:
            self.assertTrue(seen["headers"].get("x-api-key") or
                            seen["headers"].get("authorization"),
                            "a credential-less request reached Anthropic")

    def test_the_same_credential_still_shares_its_cache(self):
        with self.client() as client:
            bridge = self.bridge_header()
            headers = {**bridge, "x-api-key": ANTHROPIC_KEY}
            self.ids(client.get("/v1/models", headers=headers))
            STATE["status"] = 500
            self.assertEqual(self.ids(client.get("/v1/models", headers=headers)),
                             ["claude-opus-a", "agentaus"],
                             "the per-credential fallback stopped working")

    def test_credential_cache_keys_do_not_depend_on_header_order(self):
        from agentaus_bridge.server import _credential_key
        first = _credential_key({"x-api-key": "key", "authorization": "Bearer oauth"})
        second = _credential_key({"authorization": "Bearer oauth", "x-api-key": "key"})
        self.assertEqual(first, second)
        self.assertNotIn("key", first)
        self.assertNotIn("oauth", first)

    def test_a_no_credential_caller_never_asked_anthropic_nor_got_a_cached_list(self):
        with self.client() as client:
            bridge = self.bridge_header()
            self.ids(client.get("/v1/models", headers={**bridge, "x-api-key": ANTHROPIC_KEY}))
            # A caller with no Anthropic credential: served Agentaus alone, never A's list.
            self.assertEqual(self.ids(client.get("/v1/models", headers=bridge)), ["agentaus"])
        self.assertEqual(len(STATE["seen"]), 1,
                         "a caller with no credential reached Anthropic")
        self.assertEqual(STATE["seen"][0]["headers"].get("x-api-key"), ANTHROPIC_KEY)


if __name__ == "__main__":
    unittest.main()
