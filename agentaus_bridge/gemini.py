"""Gemini models in the same session, through Google's OpenAI-compatible API.

A model id starting with "gemini-" (GEMINI_MODEL_PREFIX) comes here instead of going to
Agentaus or Anthropic. Gemini reads images, runs long tool loops and has its own 1M window,
so none of the Agentaus scaffolding applies: the request is translated once, sent, and the
answer translated back.

Measured against the live API (gemini-4-hydrogen, 24 Nov 2026):
  * A tool-calling answer carries a thought signature on its first call, at
    tool_calls[0].extra_content.google.thought_signature. Sent back without it, the next
    request fails: HTTP 400 "Function call is missing a thought_signature". Anthropic's
    tool_use block has nowhere to keep it, so the bridge keeps it, keyed by the call id.
  * After a bridge restart those are gone. Google's documented placeholder,
    skip_thought_signature_validator, on the first call of each step is accepted instead.
    The same placeholder covers calls another model made earlier in the conversation.
  * Streamed tool calls arrive whole, one per chunk, with an id but no `index`, and the
    stream ends with finish_reason "stop" even when it called tools.
  * Errors arrive as a JSON list, [{"error": {...}}]. 503 "high demand" is common.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import settings
from .translate import (
    _STOP_REASON,
    AnthropicStreamBuilder,
    _map_tool_choice,
    _map_tools,
    _parse_arguments,
    _system_to_text,
    estimate_request_tokens,
    system_notes,
)

# Google's documented stand-in for a signature it never issued.
DUMMY_SIGNATURE = "skip_thought_signature_validator"

# Worth another go before the client sees them. Claude Code retries these too, so the
# bridge only rides out a short spike.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_DELAYS_S = (2.0, 5.0)

# Anthropic's tool_use id pattern. Gemini's own ids ("call_5874...") already fit.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    503: "overloaded_error",
}

# Claude Code's effort levels -> Gemini reasoning_effort. Gemini 4 Hydrogen and Neon take
# "xhigh" too, but the others reject it, so the top levels share "high".
_EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

# call id -> thought signature ("" for a call the API answered without one).
_SIGNATURES: OrderedDict[str, str] = OrderedDict()
_SIGNATURES_KEPT = 4096
# model -> its outputTokenLimit, read once from the API.
_OUTPUT_LIMITS: dict[str, int] = {}


def model_entries() -> list[dict]:
    """GET /v1/models rows for the configured Gemini models."""
    if not settings.gemini_api_key:
        return []
    return [{"id": model, "type": "model", "display_name": _display_name(model),
             "description": "Google Gemini via the local bridge",
             "created_at": "2026-11-01T00:00:00Z"} for model in settings.gemini_models]


def _display_name(model: str) -> str:
    words = model.split("-")
    return " ".join(w.capitalize() if not w[:1].isdigit() else w for w in words)


def remember_signature(call_id: str, signature: str) -> None:
    _SIGNATURES[call_id] = signature or ""
    _SIGNATURES.move_to_end(call_id)
    while len(_SIGNATURES) > _SIGNATURES_KEPT:
        _SIGNATURES.popitem(last=False)


def _signature_of(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    google = (item.get("extra_content") or {}).get("google") or {}
    return google.get("thought_signature") or ""


def _tool_id(raw: Any) -> str:
    if isinstance(raw, str) and _SAFE_ID.match(raw):
        return raw
    return f"toolu_{uuid.uuid4().hex[:24]}"


# --------------------------------------------------------------------------------------
# Claude Code -> Gemini
# --------------------------------------------------------------------------------------

def _image_part(block: dict) -> dict | None:
    source = block.get("source") or {}
    if source.get("type") == "base64" and source.get("data"):
        media = source.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{source['data']}"}}
    if source.get("type") == "url" and source.get("url"):
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    return None


def _content_parts(content: Any) -> tuple[list[str], list[dict]]:
    """The text and the images in Anthropic content, at any depth."""
    if content is None:
        return [], []
    if isinstance(content, str):
        return [content], []
    blocks = content if isinstance(content, list) else [content]
    texts: list[str] = []
    images: list[dict] = []
    for block in blocks:
        if isinstance(block, str):
            texts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(block.get("text", ""))
        elif kind == "image":
            part = _image_part(block)
            if part:
                images.append(part)
            else:
                texts.append("[Image omitted: its source type cannot be forwarded.]")
        elif kind == "document":
            source = block.get("source") or {}
            if source.get("type") == "text" and isinstance(source.get("data"), str):
                title = block.get("title")
                texts.append((f"[Document: {title}]\n" if title else "") + source["data"])
            else:
                texts.append("[Document omitted: only plain-text documents are forwarded to Gemini.]")
        elif kind in ("tool_use", "tool_result", "thinking", "redacted_thinking"):
            continue
        elif isinstance(block.get("content"), (list, str)):
            more_texts, more_images = _content_parts(block["content"])
            texts.extend(more_texts)
            images.extend(more_images)
    return texts, images


def _user_content(texts: list[str], images: list[dict]) -> str | list[dict]:
    joined = "\n".join(t for t in texts if t).strip()
    if not images:
        return joined
    return ([{"type": "text", "text": joined}] if joined else []) + images


def _effort(body: dict) -> str | None:
    if settings.gemini_reasoning_effort:
        return settings.gemini_reasoning_effort
    wanted = str((body.get("output_config") or {}).get("effort") or "").lower()
    return _EFFORTS.get(wanted)


def to_openai(body: dict, model: str, *, stream: bool, max_output: int) -> dict:
    """One Anthropic /v1/messages body as a Gemini chat-completions body."""
    messages: list[dict] = []
    system = "\n\n".join(
        t for t in [_system_to_text(body.get("system"))] + system_notes(body) if t)
    if system:
        messages.append({"role": "system", "content": system})

    for message in body.get("messages") or []:
        role = message.get("role", "user")
        content = message.get("content")
        if role == "system":
            continue                    # joined to the system prompt above
        if isinstance(content, str):
            if content.strip():
                messages.append({"role": role, "content": content})
            continue

        blocks = [b for b in content or [] if isinstance(b, (dict, str))]
        texts, images = _content_parts(blocks)
        calls = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]

        if role == "assistant":
            tool_calls = []
            for position, block in enumerate(calls):
                call_id = block.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
                entry: dict[str, Any] = {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": block.get("name", ""),
                                 "arguments": json.dumps(block.get("input") or {})},
                }
                # The first call of a step must carry a signature; later parallel calls never
                # had one. A call Gemini did not make here (an earlier model's, or one from
                # before a restart) gets the placeholder.
                signature = _SIGNATURES.get(call_id)
                if signature is None and position == 0:
                    signature = DUMMY_SIGNATURE
                if signature:
                    entry["extra_content"] = {"google": {"thought_signature": signature}}
                tool_calls.append(entry)
            joined = "\n".join(t for t in texts if t).strip()
            if joined or tool_calls:
                entry = {"role": "assistant", "content": joined or None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                messages.append(entry)
            continue

        # A tool message carries text only, so images a tool returned (a screenshot, an
        # image file read) follow the results as one user message.
        result_images: list[dict] = []
        for result in results:
            result_texts, found = _content_parts(result.get("content"))
            result_images.extend(found)
            text = "\n".join(t for t in result_texts if t).strip() or "(no output)"
            if result.get("is_error"):
                text = f"Error: {text}"
            messages.append({"role": "tool", "tool_call_id": result.get("tool_use_id", ""),
                             "content": text})
        if result_images:
            texts = ["Images returned by the tool calls above:"] + texts
        user = _user_content(texts, result_images + images)
        if user:
            messages.append({"role": "user", "content": user})

    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
    if stream:
        payload["stream_options"] = {"include_usage": True}
    requested = body.get("max_tokens")
    payload["max_tokens"] = min(int(requested), max_output) if requested else max_output
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            payload[key] = body[key]
    stops = body.get("stop_sequences")
    if stops:
        payload["stop"] = list(stops)[:5]
    tools = _map_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = _map_tool_choice(body.get("tool_choice")) or "auto"
    effort = _effort(body)
    if effort:
        payload["reasoning_effort"] = effort
    return payload


# --------------------------------------------------------------------------------------
# Gemini -> Claude Code
# --------------------------------------------------------------------------------------

def _usage(usage: dict | None) -> dict:
    usage = usage or {}
    return {"input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0)}


def to_anthropic(data: dict, model: str) -> dict:
    """A non-streaming Gemini completion as an Anthropic Message."""
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    content: list[dict] = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    calls = message.get("tool_calls") or []
    for call in calls:
        fn = call.get("function") or {}
        call_id = _tool_id(call.get("id"))
        remember_signature(call_id, _signature_of(call))
        content.append({"type": "tool_use", "id": call_id, "name": fn.get("name", ""),
                        "input": _parse_arguments(fn.get("arguments"))})
    if not content:
        content.append({"type": "text", "text": ""})
    finish = choice.get("finish_reason") or "stop"
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use" if calls else _STOP_REASON.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": _usage(data.get("usage")),
    }


def _error_message(text: str) -> str:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return (text or "").strip()[:800]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return (text or "").strip()[:800]


def error_response(status: int, text: str) -> JSONResponse:
    message = _error_message(text)
    # Claude Code compacts on "prompt is too long"; Gemini says the input "exceeds" its limit.
    if status == 400 and "token" in message.lower() and "exceed" in message.lower():
        message = f"prompt is too long: {message}"
    return JSONResponse(
        {"type": "error", "error": {"type": _ERROR_TYPES.get(status, "api_error"),
                                    "message": f"Gemini error: {message}"}},
        status_code=status,
    )


async def _relay(response: httpx.Response, builder: AnthropicStreamBuilder,
                 rlog: Callable[..., None]) -> AsyncIterator[bytes]:
    """Gemini's stream as Anthropic SSE, with pings while Gemini is silent."""
    yield builder.start()
    lines: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for line in response.aiter_lines():
                await lines.put(line)
        except Exception as exc:  # noqa: BLE001 - handed to the reader as the stream's end
            await lines.put(exc)
        finally:
            await lines.put(None)

    reader = asyncio.create_task(pump())
    calls: list[dict] = []
    finish: str | None = None
    usage: dict = {}
    try:
        while True:
            try:
                line = await asyncio.wait_for(lines.get(), settings.ping_interval_seconds)
            except asyncio.TimeoutError:
                yield builder.ping()
                continue
            if line is None:
                break
            if isinstance(line, Exception):
                rlog(logging.WARNING, "gemini stream broke: %s", type(line).__name__)
                yield builder.error(f"Gemini stream broke: {type(line).__name__}")
                return
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk, list) and chunk:
                chunk = chunk[0]
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                message = _error_message(json.dumps(chunk))
                rlog(logging.WARNING, "gemini stream error: %s", message)
                yield builder.error(f"Gemini error: {message}")
                return
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield builder.text(delta["content"])
                for call in delta.get("tool_calls") or []:
                    _collect(calls, call)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
    finally:
        reader.cancel()
        await response.aclose()

    for call in calls:
        call_id = _tool_id(call["id"])
        remember_signature(call_id, call["signature"])
        yield builder.tool_use(call_id, call["name"], call["arguments"] or "{}")
    rlog(logging.INFO, "gemini done finish=%s tools=%d in=%s out=%s", finish, len(calls),
         usage.get("prompt_tokens"), usage.get("completion_tokens"))
    yield builder.finish("tool_calls" if calls else finish or "stop",
                         {"output_tokens": usage.get("completion_tokens")})


def _collect(calls: list[dict], call: dict) -> None:
    """Gather one streamed tool-call delta. Gemini sends each call whole, without the
    `index` OpenAI uses to join fragments, so a call is matched by index, then id."""
    fn = call.get("function") or {}
    slot = None
    if call.get("index") is not None:
        slot = next((c for c in calls if c["index"] == call["index"]), None)
    elif call.get("id"):
        slot = next((c for c in calls if c["id"] == call["id"]), None)
    elif calls:
        slot = calls[-1]                # a bare fragment continues the last call
    if slot is None:
        slot = {"index": call.get("index"), "id": call.get("id") or "", "name": "",
                "arguments": "", "signature": ""}
        calls.append(slot)
    if call.get("id"):
        slot["id"] = call["id"]
    if fn.get("name"):
        slot["name"] = fn["name"]
    if fn.get("arguments"):
        slot["arguments"] += fn["arguments"]
    if _signature_of(call):
        slot["signature"] = _signature_of(call)


# --------------------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------------------

async def _model_info(client: httpx.AsyncClient, model: str) -> tuple[int, dict]:
    """(HTTP status, body) of Google's own entry for `model`; (0, {}) when unreachable."""
    try:
        response = await client.get(f"{settings.gemini_base_url}/models/{model}",
                                    headers={"x-goog-api-key": settings.gemini_api_key},
                                    timeout=10)
        return response.status_code, response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return 0, {}


async def _output_limit(client: httpx.AsyncClient, model: str) -> int:
    """The model's own output limit, capped by GEMINI_MAX_OUTPUT_TOKENS."""
    cap = settings.gemini_max_output_tokens
    if model not in _OUTPUT_LIMITS:
        _, info = await _model_info(client, model)
        _OUTPUT_LIMITS[model] = int(info.get("outputTokenLimit") or 0)
    return min(_OUTPUT_LIMITS[model] or cap, cap)


async def lookup(request: Request, model: str) -> Response:
    """GET /v1/models/{id} for a Gemini id. Claude Code asks this before switching a session
    to a model it does not know, and refuses the model on anything but a 200. Google is
    asked too, so a mistyped id is still refused rather than failing on its first turn."""
    entry = {"id": model, "type": "model", "display_name": _display_name(model),
             "created_at": "2026-11-01T00:00:00Z"}
    if not settings.gemini_api_key:
        # Accepted, so the first turn can say plainly that the key is missing.
        return JSONResponse(entry)
    client: httpx.AsyncClient = (
        getattr(request.app.state, "anthropic_client", None) or request.app.state.client)
    status, info = await _model_info(client, model)
    if status == 404:
        return JSONResponse(
            {"type": "error", "error": {"type": "not_found_error",
                                        "message": f"Gemini has no model named {model}"}},
            status_code=404)
    if info.get("outputTokenLimit"):
        _OUTPUT_LIMITS[model] = int(info["outputTokenLimit"])
    if info.get("displayName"):
        entry["display_name"] = info["displayName"]
    return JSONResponse(entry)


async def _send(client: httpx.AsyncClient, payload: dict, stream: bool,
                rlog: Callable[..., None]) -> httpx.Response:
    url = f"{settings.gemini_base_url}/openai/chat/completions"
    headers = {"Authorization": f"Bearer {settings.gemini_api_key}",
               "Content-Type": "application/json"}
    body = json.dumps(payload)
    for attempt in range(len(_RETRY_DELAYS_S) + 1):
        last = attempt == len(_RETRY_DELAYS_S)
        try:
            response = await client.send(
                client.build_request("POST", url, headers=headers, content=body), stream=stream)
        except httpx.TransportError as exc:
            if last:
                raise
            rlog(logging.WARNING, "gemini unreachable (%s), retrying", type(exc).__name__)
        else:
            if response.status_code not in _RETRY_STATUSES or last:
                return response
            await response.aread()
            await response.aclose()
            rlog(logging.WARNING, "gemini answered HTTP %d (%s), retrying", response.status_code,
                 _error_message(response.text)[:200])
        await asyncio.sleep(_RETRY_DELAYS_S[attempt])
    raise RuntimeError("unreachable")


async def handle(request: Request, body: dict, model: str, wants_stream: bool,
                 rlog: Callable[..., None]) -> Response:
    """Serve one /v1/messages request from Gemini."""
    if not settings.gemini_api_key:
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error", "message": (
                "GEMINI_API_KEY is not set. Add it to .env and restart the bridge.")}},
            status_code=401)
    client: httpx.AsyncClient = (
        getattr(request.app.state, "anthropic_client", None) or request.app.state.client)
    payload = to_openai(body, model, stream=wants_stream,
                        max_output=await _output_limit(client, model))
    rlog(logging.INFO, "gemini send model=%s stream=%s msgs=%d tools=%d max_tokens=%d effort=%s",
         model, wants_stream, len(payload["messages"]), len(payload.get("tools") or []),
         payload["max_tokens"], payload.get("reasoning_effort") or "default")
    try:
        response = await _send(client, payload, wants_stream, rlog)
    except httpx.HTTPError as exc:
        return error_response(502, f"could not reach Gemini ({type(exc).__name__})")

    if response.status_code != 200:
        text = (await response.aread()).decode("utf-8", "replace")
        await response.aclose()
        rlog(logging.WARNING, "gemini rejected the request: HTTP %d %s",
             response.status_code, _error_message(text)[:500])
        return error_response(response.status_code, text)

    if not wants_stream:
        message = to_anthropic(response.json(), model)
        rlog(logging.INFO, "gemini done stop=%s out=%s", message["stop_reason"],
             message["usage"]["output_tokens"])
        return JSONResponse(message)

    builder = AnthropicStreamBuilder(model, input_tokens=estimate_request_tokens(body),
                                     chunk_chars=settings.chunk_chars)
    return StreamingResponse(_relay(response, builder, rlog), media_type="text/event-stream",
                             headers={"cache-control": "no-cache"})
