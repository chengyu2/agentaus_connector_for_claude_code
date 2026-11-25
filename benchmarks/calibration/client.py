"""The one way this study calls Agentaus: straight to the API, never through the bridge.

The baseline is the model with no special handling, so nothing the bridge adds (its
guidance, its no-vision note, its repairs) may reach these requests. Each call:

- streams, because Cloudflare answers 524 to a non-streamed request that thinks for
  more than 100 s;
- declares Agentaus's built-in web tools with tool_choice "none" when it offers no tools
  of its own, because a request without tools turns its web search on by itself;
- gives up on a stream after AGENTAUS_ATTEMPT_SECONDS and retries, because some streams
  stall forever;
- is appended to a usage log in the {time, input_tokens, output_tokens, cost_aud} format,
  priced at the published AUD rates.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BASE_URL = os.environ.get("AGENTAUS_BASE_URL", "https://agentaus.com.au/api/v1")
PRICE_IN, PRICE_OUT = 1.70, 6.70          # AUD per million tokens (input, output)
ATTEMPT_SECONDS = float(os.environ.get("AGENTAUS_ATTEMPT_SECONDS", "1500"))
RETRIES = 3
RATE_LIMIT_RETRIES = 8          # about 18 minutes of waiting in all
NO_WEB = {"tools": [{"type": "agentaus_web_search"}, {"type": "agentaus_web_fetch"}],
          "tool_choice": "none"}


def api_key() -> str:
    key = os.environ.get("AGENTAUS_API_KEY", "").strip()
    if not key:
        raise SystemExit("AGENTAUS_API_KEY is not set")
    return key


def cost_aud(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * PRICE_IN + output_tokens * PRICE_OUT) / 1_000_000


@dataclass
class Completion:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    attempts: int = 1
    waited_seconds: float = 0.0   # abandoned attempts and back-off sleeps before this one

    @property
    def cost_aud(self) -> float:
        return cost_aud(self.input_tokens, self.output_tokens)

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    def as_message(self) -> dict:
        message: dict = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


class TransientError(Exception):
    pass


def _log_usage(completion: Completion) -> None:
    path = os.environ.get("CALIBRATION_USAGE_LOG")
    if not path:
        return
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "input_tokens": completion.input_tokens,
           "output_tokens": completion.output_tokens, "cost_aud": round(completion.cost_aud, 6)}
    with Path(path).expanduser().open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _stream(payload: dict) -> Completion:
    started = time.monotonic()
    out = Completion()
    calls: dict[int, dict] = {}
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
               "Accept": "*/*", "User-Agent": "agentaus-calibration/1.0"}
    timeout = httpx.Timeout(300.0, connect=30.0)
    with httpx.stream("POST", f"{BASE_URL}/chat/completions", json=payload, headers=headers,
                      timeout=timeout) as response:
        if response.status_code in (408, 425, 429, 500, 502, 503, 504, 520, 522, 524):
            raise TransientError(f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.read()[:300]!r}")
        for line in response.iter_lines():
            if time.monotonic() - started > ATTEMPT_SECONDS:
                raise TransientError(f"stream still open after {ATTEMPT_SECONDS:.0f}s")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if "error" in chunk:
                message = str(chunk["error"].get("message", chunk["error"]))
                if "max_model_len" in message or "context length" in message:
                    raise RuntimeError(message)
                raise TransientError(message)
            if chunk.get("usage"):
                out.input_tokens = int(chunk["usage"].get("input_tokens")
                                       or chunk["usage"].get("prompt_tokens") or 0)
                out.output_tokens = int(chunk["usage"].get("output_tokens")
                                        or chunk["usage"].get("completion_tokens") or 0)
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    out.content += delta["content"]
                for call in delta.get("tool_calls") or []:
                    slot = calls.setdefault(call.get("index", 0) or 0,
                                            {"id": "", "type": "function",
                                             "function": {"name": "", "arguments": ""}})
                    if call.get("id"):
                        slot["id"] = call["id"]
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    out.finish_reason = choice["finish_reason"]
    out.tool_calls = [calls[k] for k in sorted(calls)]
    for i, call in enumerate(out.tool_calls):
        call["id"] = call["id"] or f"call_{i}"
    out.seconds = time.monotonic() - started
    return out


def chat(messages: list, *, max_tokens: int, reasoning_effort: str = "high",
         tools: list | None = None, tool_choice: str = "auto") -> Completion:
    """One completion, retried on transient failures. Every completed attempt's usage is
    logged; an attempt abandoned mid-stream reports no usage, so its cost is not."""
    payload = {"messages": messages, "stream": True, "max_tokens": max_tokens,
               "reasoning_effort": reasoning_effort, "system_prompt_overwrite": True,
               "stream_options": {"include_usage": True}}
    if tools:
        payload.update(tools=tools, tool_choice=tool_choice)
    else:
        payload.update(NO_WEB)
    last: Exception | None = None
    attempt = 0
    waited = 0.0
    while True:
        attempt += 1
        started = time.monotonic()
        try:
            completion = _stream(payload)
            completion.attempts = attempt
            completion.waited_seconds = waited
            _log_usage(completion)
            return completion
        except (TransientError, httpx.TransportError) as exc:
            last = exc
            waited += time.monotonic() - started
            # A tenant rate limit (429) lasts minutes, not seconds: measured, four tries
            # over 75 s all failed while six requests were in flight.
            limited = "429" in str(exc)
            if attempt > (RATE_LIMIT_RETRIES if limited else RETRIES):
                raise RuntimeError(f"gave up after {attempt} attempts: {last}") from None
            pause = min(300, 30 * attempt) if limited else min(60, 5 * 2 ** (attempt - 1))
            time.sleep(pause)
            waited += pause


# Asking for the final answer: high effort first. Measured, a model told to stop and
# answer spent 16k tokens reasoning and wrote nothing at effort high, so an empty reply
# is asked once more at effort none - and the path is recorded, because effort changes
# the answer itself (the same transcript gave "could not determine, 30%" at none and an
# invented value at 85% at low).
FINAL_STEPS = (("high", 16_384, "reask_high"), ("none", 4_096, "reask_none"))


def ask_final(messages: list, *, tools: list | None = None) -> tuple[list[Completion], str]:
    """The final answer and confidence. Returns every reply made, and which path won:
    reask_high, reask_none, or missing (no confidence even then)."""
    from .metrics import parse_confidence
    replies = []
    for effort, cap, path in FINAL_STEPS:
        reply = chat(messages, max_tokens=cap, reasoning_effort=effort, tools=tools,
                     tool_choice="none")
        replies.append(reply)
        if parse_confidence(reply.content) is not None:
            return replies, path
    return replies, "missing"
