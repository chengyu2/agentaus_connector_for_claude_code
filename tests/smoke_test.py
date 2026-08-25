"""End-to-end check against a RUNNING bridge.

    ./scripts/start-bridge.sh                  # terminal 1
    ./.venv/bin/python tests/smoke_test.py     # terminal 2

Exercises the paths Claude Code actually depends on: a plain completion, a
streaming completion with a well-formed event sequence, a two-turn tool-use
round trip, and the token-counting endpoint.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

PASS, FAIL = "  PASS", "  FAIL"
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    if condition:
        print(f"{PASS}  {label}")
    else:
        failures += 1
        print(f"{FAIL}  {label}  {detail}")


def parse_sse(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--model", default="agentaus")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    print(f"\nSmoke-testing the bridge at {base}\n")

    print("1. health")
    try:
        health = httpx.get(f"{base}/healthz", timeout=10).json()
    except httpx.HTTPError as exc:
        print(f"{FAIL}  bridge unreachable: {exc}")
        return 1
    check("responds", health.get("status") == "ok")
    check("Agentaus key configured", health.get("agentaus_key_configured") is True)

    print("\n2. non-streaming message")
    reply = httpx.post(
        f"{base}/v1/messages",
        json={
            "model": args.model,
            "max_tokens": 128,
            "system": [{"type": "text", "text": "Answer with one word only."}],
            "messages": [{"role": "user", "content": "Capital of Australia?"}],
        },
        timeout=120,
    )
    check("HTTP 200", reply.status_code == 200, reply.text[:200])
    if reply.status_code == 200:
        body = reply.json()
        check("type=message", body.get("type") == "message")
        check("role=assistant", body.get("role") == "assistant")
        check("has a text block", any(b["type"] == "text" for b in body.get("content", [])))
        check("stop_reason set", bool(body.get("stop_reason")))
        check("usage reported", body.get("usage", {}).get("input_tokens", 0) > 0)
        text = " ".join(b.get("text", "") for b in body.get("content", []))
        check("answer mentions Canberra", "canberra" in text.lower(), repr(text[:120]))

    print("\n3. streaming message")
    with httpx.stream(
        "POST",
        f"{base}/v1/messages",
        json={
            "model": args.model,
            "max_tokens": 256,
            "stream": True,
            "messages": [{"role": "user", "content": "List three colours, comma separated."}],
        },
        timeout=120,
    ) as response:
        raw = "".join(response.iter_text())
    events = parse_sse(raw)
    types = [e.get("type") for e in events]
    check("starts with message_start", types[:1] == ["message_start"], str(types[:3]))
    check("ends with message_stop", types[-1:] == ["message_stop"], str(types[-3:]))
    check("has content_block_start", "content_block_start" in types)
    check("has text deltas", any(
        e.get("delta", {}).get("type") == "text_delta" for e in events
    ))
    check("has message_delta with stop_reason", any(
        e.get("type") == "message_delta" and e.get("delta", {}).get("stop_reason") for e in events
    ))
    opens = types.count("content_block_start")
    closes = types.count("content_block_stop")
    check("every block is closed", opens == closes, f"{opens} opened vs {closes} closed")

    print("\n4. tool use, turn 1 (model should request the tool)")
    tools = [
        {
            "name": "get_exchange_rate",
            "description": "Get the exchange rate between two currencies",
            "input_schema": {
                "type": "object",
                "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                "required": ["from", "to"],
            },
        }
    ]
    turn1 = httpx.post(
        f"{base}/v1/messages",
        json={
            "model": args.model,
            "max_tokens": 512,
            "system": [{"type": "text", "text": "Use the supplied tools when relevant."}],
            "messages": [{"role": "user", "content": "What is the AUD to USD rate? Use the tool."}],
            "tools": tools,
            "tool_choice": {"type": "auto"},
        },
        timeout=120,
    ).json()
    tool_uses = [b for b in turn1.get("content", []) if b.get("type") == "tool_use"]
    check("returned a tool_use block", bool(tool_uses), json.dumps(turn1)[:200])
    check("stop_reason=tool_use", turn1.get("stop_reason") == "tool_use", str(turn1.get("stop_reason")))
    if tool_uses:
        check("tool input parsed to an object", isinstance(tool_uses[0].get("input"), dict))

    print("\n5. tool use, turn 2 (model should use the tool result)")
    if tool_uses:
        block = tool_uses[0]
        turn2 = httpx.post(
            f"{base}/v1/messages",
            json={
                "model": args.model,
                "max_tokens": 512,
                "system": [{"type": "text", "text": "Use the supplied tools when relevant."}],
                "messages": [
                    {"role": "user", "content": "What is the AUD to USD rate? Use the tool."},
                    {"role": "assistant", "content": [block]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block["id"],
                                "content": '{"rate": 0.6612}',
                            }
                        ],
                    },
                ],
                "tools": tools,
            },
            timeout=120,
        ).json()
        text = " ".join(b.get("text", "") for b in turn2.get("content", []) if b.get("type") == "text")
        check("answered from the tool result", "0.66" in text, repr(text[:160]))

    print("\n6. count_tokens")
    counted = httpx.post(
        f"{base}/v1/messages/count_tokens",
        json={"model": args.model, "messages": [{"role": "user", "content": "hello world"}]},
        timeout=30,
    )
    check("HTTP 200", counted.status_code == 200)
    check("returns input_tokens", counted.json().get("input_tokens", 0) > 0)

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) FAILED.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
