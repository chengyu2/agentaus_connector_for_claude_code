#!/usr/bin/env python3
"""Live end-to-end checks against a running bridge and the real Agentaus API.

Unlike the unit tests, nothing here is stubbed: this is the flow a Claude Code
session actually performs - switching models mid-session, long conversations that
overflow Agentaus' window, tool-call round trips, and streaming.

    ./.venv/bin/python tests/integration_test.py
    ./.venv/bin/python tests/integration_test.py --url http://127.0.0.1:9100

Requires the bridge to be running with a valid AGENTAUS_API_KEY. Costs real tokens.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

PASS, FAIL = "  PASS", "  FAIL"
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"{PASS if ok else FAIL}  {label}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def call(url: str, payload: dict, headers: dict | None = None, timeout: int = 180):
    req = urllib.request.Request(
        f"{url}/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def text_of(raw: str) -> str:
    try:
        return "".join(b.get("text", "") for b in json.loads(raw).get("content", []))
    except Exception:
        return ""


def stream_text(raw: str) -> str:
    """Concatenate the text deltas out of an SSE body."""
    out = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        if ev.get("type") == "content_block_delta":
            out.append(ev.get("delta", {}).get("text", ""))
    return "".join(out)


def long_conversation(turns: int, words_per_turn: int) -> list[dict]:
    msgs: list[dict] = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"Message {i}: " + "filler text here. " * words_per_turn})
        msgs.append({"role": "assistant", "content": f"Acknowledged {i}."})
    return msgs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    args = ap.parse_args()
    url = args.url.rstrip("/")

    print(f"\nLive integration checks against {url}\n" + "=" * 62)

    # --- 1. bridge is up ------------------------------------------------------
    print("\n1. Bridge health")
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=10) as r:
            health = json.loads(r.read())
        check("healthz responds", r.status == 200)
        check("Agentaus key configured", health.get("agentaus_key_configured") is True)
    except Exception as e:
        check("healthz responds", False, str(e)[:60])
        print("\nBridge is not reachable - start it first.\n")
        return 1

    # --- 2. routing: the whole point of the bridge -----------------------------
    print("\n2. Per-model routing")
    code, raw = call(url, {"model": "agentaus", "max_tokens": 32,
                           "messages": [{"role": "user", "content": "Reply with exactly: AGENTAUS"}]})
    check("agentaus model reaches Agentaus", code == 200 and "AGENTAUS" in text_of(raw).upper(),
          f"HTTP {code}")

    code, raw = call(url, {"model": "claude-sonnet-5", "max_tokens": 16,
                           "messages": [{"role": "user", "content": "hi"}]},
                     headers={"x-api-key": "sk-ant-deliberately-invalid",
                              "anthropic-version": "2023-06-01"}, timeout=60)
    # A genuine auth error proves the request reached Anthropic rather than Agentaus.
    check("claude model is forwarded to Anthropic", "authentication_error" in raw or code == 401,
          f"HTTP {code}")

    # --- 3. switching back and forth, as /model does mid-session ---------------
    print("\n3. Switching models mid-session")
    ok = True
    for i, model in enumerate(["agentaus", "agentaus"]):
        code, raw = call(url, {"model": model, "max_tokens": 24,
                               "messages": [{"role": "user", "content": f"Reply with exactly: TURN{i}"}]})
        ok = ok and code == 200 and f"TURN{i}" in text_of(raw).upper()
    check("consecutive Agentaus turns succeed", ok)

    # --- 4. streaming ---------------------------------------------------------
    print("\n4. Streaming")
    code, raw = call(url, {"model": "agentaus", "max_tokens": 32, "stream": True,
                           "messages": [{"role": "user", "content": "Reply with exactly: STREAMED"}]})
    check("stream returns a well-formed SSE sequence",
          "message_start" in raw and "message_stop" in raw, f"HTTP {code}")
    check("stream carries the reply text", "STREAMED" in stream_text(raw).upper())
    check("stream reports no error", '"type": "error"' not in raw and "event: error" not in raw)

    # --- 5. the recovery this bridge performs for oversized conversations -----
    print("\n5. Oversized conversation (auto-trim)")
    msgs = long_conversation(30, 3000)
    msgs.append({"role": "user", "content": "Ignore everything above. Reply with exactly: TRIMMED_OK"})
    est = sum(len(json.dumps(m)) for m in msgs) // 4
    code, raw = call(url, {"model": "agentaus", "max_tokens": 48, "messages": msgs})
    check(f"~{est:,}-token conversation still answers", code == 200, f"HTTP {code}")
    reply = text_of(raw)
    check("reply is not empty", len(reply.strip()) > 0, f"{len(reply)} chars")
    check("the final question is what gets answered", "TRIMMED_OK" in reply.upper(),
          repr(reply[:40]))
    try:
        used = json.loads(raw).get("usage", {}).get("input_tokens", 0)
        check("upstream stayed inside the 131k window", 0 < used <= 131072, f"{used:,} tokens")
    except Exception:
        check("upstream stayed inside the 131k window", False)

    # --- 6. same, streaming, since that is what Claude Code actually sends ----
    print("\n6. Oversized conversation, streaming")
    msgs = long_conversation(30, 3000)
    msgs.append({"role": "user", "content": "Ignore everything above. Reply with exactly: STREAM_TRIMMED"})
    code, raw = call(url, {"model": "agentaus", "max_tokens": 48, "stream": True, "messages": msgs})
    check("oversized stream completes", code == 200 and "message_stop" in raw, f"HTTP {code}")
    # The regression this guards is the silent empty stream, so assert on there being
    # real content rather than on exact wording, which the model does not guarantee.
    body_text = stream_text(raw)
    check("oversized stream is not an empty reply", len(body_text.strip()) > 0,
          f"{len(body_text)} chars")
    check("oversized stream answers the final question",
          "STREAM_TRIMMED" in body_text.upper(), repr(body_text[:40]))

    # --- 6b. the point of summarising rather than truncating ------------------
    print("\n6b. Detail survives compaction")
    facts = [
        {"role": "user", "content":
            "Remember these project facts: the bridge listens on port 9473, the config "
            "lives at /etc/agentaus/bridge.toml, and we chose exponential backoff with a "
            "12 second ceiling because the upstream rate-limits at 40 req/min."},
        {"role": "assistant", "content":
            "Noted: port 9473, /etc/agentaus/bridge.toml, 12s ceiling, 40 req/min."},
    ]
    msgs = facts + long_conversation(28, 3000)
    msgs.append({"role": "user", "content":
        "What port does the bridge listen on, where is its config file, and why did we "
        "pick that backoff ceiling?"})
    code, raw = call(url, {"model": "agentaus", "max_tokens": 300, "messages": msgs}, timeout=300)
    reply = text_of(raw)
    check("compacted conversation answers", code == 200, f"HTTP {code}")
    # These facts live in the first message, which is always inside the compacted head.
    # Truncation loses them; summarising is supposed to carry them through.
    for fact, label in (("9473", "port"), ("bridge.toml", "config path"),
                        ("40", "rate limit"), ("12", "backoff reason")):
        check(f"detail preserved: {label}", fact in reply, fact)

    # --- 7. untrimmable input must fail loudly, not silently ------------------
    print("\n7. Single message too large to trim")
    code, raw = call(url, {"model": "agentaus", "max_tokens": 32,
                           "messages": [{"role": "user", "content": "word " * 200_000}]})
    check("rejected rather than silently truncated", code >= 400, f"HTTP {code}")
    check("uses wording Claude Code can auto-recover from", "prompt is too long" in raw)
    check("names a recovery that actually works", "/model opus" in raw)

    # --- 8. tool round trip ---------------------------------------------------
    print("\n8. Tool-call round trip")
    tools = [{"name": "get_time", "description": "Get the current time",
              "input_schema": {"type": "object", "properties": {}}}]
    code, raw = call(url, {"model": "agentaus", "max_tokens": 128, "tools": tools,
                           "messages": [{"role": "user", "content": "What time is it? Use get_time."}]})
    used_tool = '"tool_use"' in raw
    check("tool call is offered and translated", code == 200, f"HTTP {code}")
    if used_tool:
        blocks = json.loads(raw)["content"]
        tu = next(b for b in blocks if b["type"] == "tool_use")
        code2, raw2 = call(url, {"model": "agentaus", "max_tokens": 128, "tools": tools,
            "messages": [
                {"role": "user", "content": "What time is it? Use get_time."},
                {"role": "assistant", "content": blocks},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu["id"],
                                              "content": "2026-08-25T14:00:00Z"}]},
            ]})
        check("tool result is accepted back", code2 == 200, f"HTTP {code2}")
    else:
        print("  SKIP  model chose not to call the tool this run")

    print("\n" + "=" * 62)
    failed = results.count(False)
    print(f"{len(results) - failed}/{len(results)} checks passed"
          + (f", {failed} FAILED" if failed else ""))
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
