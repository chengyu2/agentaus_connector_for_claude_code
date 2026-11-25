"""HTTP surface of the bridge: an Anthropic Messages API that Claude Code can talk to.

Requests are routed per-request by model id:
  * model contains "agentaus"  -> translated and sent to the Agentaus API
  * anything else              -> forwarded byte-for-byte to api.anthropic.com

That per-request split is what lets Agentaus sit *alongside* the built-in Claude
models in one session instead of replacing them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import flight
from . import graph
from . import sessions
from . import throttle
from .agentaus_turn import _handle_agentaus
from .bridge_responses import _error_response
from .config import settings
from .conversation_text import _last_user_text
from .passthrough import (
    BRIDGE_TOKEN_HEADER, _anthropic_request_headers, _passthrough, _same_secret,
)
from .request_log import _new_request_id, _request_id, log, rlog
from .tokens import calibrator, has_tokeniser as _has_tokeniser
from .translate import estimate_request_tokens
from .upstream_http import _context_limit

app = FastAPI(title="Agentaus bridge for Claude Code", docs_url=None, redoc_url=None)


def _credential_key(headers: dict) -> str:
    """A stable identity for the Anthropic credential a request carries, for caching.

    Only the auth headers count: two callers presenting the same credential share one
    cache entry, and a caller with no credential is distinguished from every caller
    that has one (an empty key, never a cache hit).
    """
    parts = sorted(f"{name.lower()}={value}" for name, value in headers.items()
                   if name.lower() in ("x-api-key", "authorization"))
    if not parts:
        return ""
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


@app.on_event("startup")
async def _startup() -> None:
    # Flights belong to one event loop: a test client's, or the server's.
    flight.reset()
    global _SOURCE_HASH
    _SOURCE_HASH = _source_hash()
    # Every Agentaus call takes a slot from one adaptive limit, beneath every caller.
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.read_timeout,
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.connect_timeout,
        ),
        transport=throttle.LimitedTransport(httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))),
        follow_redirects=False,
    )
    # Claude traffic gets its own pool. Sharing one meant a burst of bridge helper calls
    # - a search fan-out, a cold compaction - could hold every connection, and a Claude
    # turn then queued for a slot behind Agentaus work it has nothing to do with.
    app.state.anthropic_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.read_timeout,
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.connect_timeout,
            pool=settings.connect_timeout,
        ),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=32),
        follow_redirects=False,
    )
    log.info(
        "bridge ready  agentaus=%s  passthrough=%s -> %s  markers=%s",
        settings.agentaus_url,
        "on" if settings.passthrough_enabled else "off",
        settings.anthropic_base_url,
        ",".join(settings.agentaus_model_markers),
    )
    if not settings.agentaus_api_key:
        log.warning("AGENTAUS_API_KEY is not set - Agentaus requests will fail with 401")
    if settings.bridge_token.startswith("sk-ant-") and settings.passthrough_enabled:
        log.warning(
            "BRIDGE_TOKEN looks like an Anthropic API key. The bridge token is stripped "
            "before any request goes to Anthropic, so Claude requests that carry only that "
            "key will reach Anthropic with no credential and fail with 401. Set "
            "BRIDGE_TOKEN to a separate secret, send it as the %s header, and keep your "
            "Anthropic key in x-api-key.",
            BRIDGE_TOKEN_HEADER,
        )


@app.on_event("shutdown")
async def _shutdown() -> None:
    flight.cancel_all()
    await app.state.client.aclose()
    await app.state.anthropic_client.aclose()


# --------------------------------------------------------------------------------------
# Small endpoints Claude Code probes at startup
# --------------------------------------------------------------------------------------

@app.api_route("/api/hello", methods=["GET", "HEAD"])
async def hello() -> Response:
    """Claude Code's connection-warming probe."""
    return Response(status_code=200)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "agentaus_url": settings.agentaus_url,
            "agentaus_key_configured": bool(settings.agentaus_api_key),
            "passthrough": settings.passthrough_enabled,
            "anthropic_upstream": settings.anthropic_base_url,
            "model_markers": settings.agentaus_model_markers,
            "context_limit": _context_limit(),
            "tokeniser": "tiktoken" if _has_tokeniser() else "chars/4 fallback",
            "token_calibration": {
                "ratio": round(calibrator.ratio, 3),
                "samples": calibrator.samples,
            },
        }
    )


def _source_hash() -> str:
    """A hash of the code this process runs: the graph script and skills as graph.py loaded
    them, and the bridge's own modules. Measured (d13): the bridge started at 19:25, dynamic.js
    was edited at 22:20, and the round's code was no commit - a score must name what ran."""
    import sys
    digest = hashlib.sha256()
    for text in (graph.SCRIPT, graph.DYNAMIC_SCRIPT, *(graph.SKILLS[k] for k in sorted(graph.SKILLS))):
        digest.update(text.encode())
    for name in sorted(n for n in sys.modules if n.startswith(__package__ or "agentaus_bridge")):
        path = getattr(sys.modules[name], "__file__", None)
        if path and path.endswith(".py"):
            try:
                with open(path, "rb") as fh:
                    digest.update(fh.read())
            except OSError:
                pass
    return digest.hexdigest()[:16]


_SOURCE_HASH: str | None = None


@app.get("/healthz/upstream")
async def healthz_upstream() -> JSONResponse:
    """Agentaus as the bridge has seen it over the last ten minutes: outcomes (request-specific
    failures - time-up turns, retries of a failed request - left out), the adaptive limit,
    whether it is down and for how long in all, and the hash of the code this process loaded.
    For the harness: a round launched into a slump, or scored on a bridge running other code,
    says nothing about the graph."""
    global _SOURCE_HASH
    if _SOURCE_HASH is None:
        _SOURCE_HASH = _source_hash()
    gate = throttle.limit()
    return JSONResponse({**throttle.health().snapshot(gate.limit, gate.ceiling, gate.busy),
                         "flights": flight.running(), "source_hash": _SOURCE_HASH})


# Claude Code's model picker, when CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1, asks the gateway
# for GET /v1/models?limit=1000 at startup, waits 3 s, keeps ids containing "claude" or
# "anthropic", and falls back to its cache or built-in list on any failure or redirect. The
# bridge answers with Anthropic's own list, live, fetched with the caller's credentials - so the
# picker follows every model Anthropic adds or retires with no change here - plus Agentaus.
# Agentaus's id holds neither word, so the picker drops it: ANTHROPIC_CUSTOM_MODEL_OPTION is what
# puts it in the picker, and the entry here serves any other client that lists models.
AGENTAUS_MODEL = {
    "id": "agentaus",
    "type": "model",
    "display_name": "Agentaus (Trellis Data)",
    "description": "Sovereign Australian model via the local Agentaus bridge",
    "created_at": "2026-05-01T00:00:00Z",
}
# Inside the picker's 3 s, with room for the bridge's own work.
_MODELS_TIMEOUT_S = 2.5
# The last list each *credential* got back from Anthropic, served when that credential
# cannot reach Anthropic in time. Keyed by the credential because a list fetched with one
# caller's key must never be served to another caller, nor to a caller with no credential
# at all - a cache holding nothing for that caller serves Agentaus alone. Bounded so a
# long-lived bridge with many keys cannot grow it without end.
_MODELS_LAST: "OrderedDict[str, list]" = OrderedDict()
_MODELS_CACHE_LIMIT = 8


async def _anthropic_models(request: Request) -> tuple[list | None, str]:
    """Anthropic's model list for this caller, plus the credential it was asked with.

    Returns (models, credential key), with models None when the list cannot be had in
    time or the caller sent no credential. One attempt, no retries: the picker stops
    waiting at 3 s. Only a caller that sent a credential of its own is asked about -
    the bridge holds none of its own for Anthropic.
    """
    if not settings.passthrough_enabled:
        return None, ""
    headers = _anthropic_request_headers(request)
    credential = _credential_key(headers)
    if not credential:
        return None, ""  # no credential of its own (a bridge-only token is stripped above)
    headers.setdefault("anthropic-version", "2023-06-01")
    # The whole list, whatever page size the caller asked for: what is kept for the next caller
    # must not be one caller's partial page.
    url = f"{settings.anthropic_base_url}/v1/models?limit=1000"
    client: httpx.AsyncClient = (
        getattr(request.app.state, "anthropic_client", None) or request.app.state.client
    )
    try:
        # One bound on the whole call: httpx's timeout bounds each phase (connect, write, read)
        # separately, and together they could run past the picker's 3 s.
        response = await asyncio.wait_for(client.get(url, headers=headers, timeout=_MODELS_TIMEOUT_S),
                                          _MODELS_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - the list must never fail: a bad header, a timeout, a refusal
        rlog(logging.INFO, "model list: Anthropic not reached in time (%s)", type(exc).__name__)
        return None, credential
    if response.status_code != 200:
        rlog(logging.INFO, "model list: Anthropic answered HTTP %s", response.status_code)
        return None, credential
    try:
        data = response.json().get("data")
    except (ValueError, AttributeError):
        return None, credential
    models = [m for m in data or [] if isinstance(m, dict) and m.get("id")]
    return (models or None), credential


@app.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    """Anthropic's current models, live where it can be reached, plus Agentaus."""
    if not _client_authorised(request):
        return _error_response(401, "authentication_error", "Invalid bridge token")
    live, credential = await _anthropic_models(request)
    if live is not None:
        _MODELS_LAST[credential] = live
        _MODELS_LAST.move_to_end(credential)
        while len(_MODELS_LAST) > _MODELS_CACHE_LIMIT:
            _MODELS_LAST.popitem(last=False)
        models, source = list(live), "live from Anthropic"
    else:
        # The fallback belongs to this caller's credential alone. An empty key - a caller
        # with no Anthropic credential - is never a hit, so it is served Agentaus only.
        cached = _MODELS_LAST.get(credential)
        models = list(cached or [])
        source = ("this credential's last list" if cached
                  else "no cached list for this credential")
    if not any(m.get("id") == AGENTAUS_MODEL["id"] for m in models):
        models.append(dict(AGENTAUS_MODEL))
    rlog(logging.INFO, "model list: %d model(s) served (%s)", len(models), source)
    return JSONResponse({
        "data": models,
        "has_more": False,
        "first_id": models[0]["id"],
        "last_id": models[-1]["id"],
    })


# --------------------------------------------------------------------------------------
# Auth guard (optional, for non-localhost deployments)
# --------------------------------------------------------------------------------------

def _client_authorised(request: Request) -> bool:
    """Whether this caller may use the bridge at all (BRIDGE_TOKEN, when one is set).

    Preferred: the dedicated `x-agentaus-bridge-token` header, which is for the bridge
    alone and never forwarded upstream. Legacy (kept for existing Agentaus-only clients
    such as abbench scripts and bridges configured before the dedicated header existed):
    `x-api-key` or `Authorization: Bearer`; a match in either is enough, so the other
    can carry the caller's real Anthropic credential. A dedicated header, when sent, is
    the only one checked. Compared in constant time either way.
    """
    if not settings.bridge_token:
        return True
    dedicated = request.headers.get(BRIDGE_TOKEN_HEADER) or ""
    if dedicated:
        return _same_secret(dedicated, settings.bridge_token)
    # Legacy: either auth header may hold the bridge token, and the other one the caller's
    # real Anthropic credential (x-api-key or OAuth Bearer). Accept a match in either -
    # no weaker, as the exact token must still be presented - and compare both, so the
    # time taken does not depend on which one matched.
    api_key = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    key_matches = bool(api_key) and _same_secret(api_key, settings.bridge_token)
    bearer_matches = bool(bearer) and _same_secret(bearer, settings.bridge_token)
    return key_matches or bearer_matches


# --------------------------------------------------------------------------------------
# Main entrypoint
# --------------------------------------------------------------------------------------

# Why `_route` sent a request to Agentaus when its model id alone would not have; logged on
# the recv line, so a reader blames the right mechanism.
ROUTE_FOLLOWS_SESSION = "subagent of an Agentaus session"
ROUTE_SUBAGENTS_ONLY = "AGENTAUS_SUBAGENTS_ONLY"


def _route(request: Request, body: dict, model: str, *, main_loop: bool) -> tuple[bool, str | None]:
    """(send to Agentaus, why when not by model id: ROUTE_FOLLOWS_SESSION, ROUTE_SUBAGENTS_ONLY
    or None).

    By model id, as always - plus a subagent goes wherever its session's main loop last
    went. See `agentaus_bridge.sessions` for why the model id alone sent Agentaus
    sessions' subagents to Claude. Only a main-loop turn (tools offered, no agent id)
    records the session's model: a title or summary call must not flip it.
    """
    by_model = settings.routes_to_agentaus(model) or not settings.passthrough_enabled
    if (settings.agentaus_subagents_only and not by_model
            and sessions.is_subagent(request.headers, body)):
        # Every subagent on Agentaus, whatever its session or model - including after a bridge
        # restart, when the in-memory session record is gone.
        return True, ROUTE_SUBAGENTS_ONLY
    if not settings.agentaus_subagents_follow_session:
        return by_model, None
    session = sessions.session_of(request.headers)
    if sessions.is_subagent(request.headers, body):
        if not by_model and sessions.main_loop_on_agentaus(session):
            return True, ROUTE_FOLLOWS_SESSION
        return by_model, None
    if main_loop and body.get("tools"):
        sessions.note_main_loop(session, by_model)
        if by_model:
            sessions.note_request(session, _last_user_text(body))
    return by_model, None


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    if not _client_authorised(request):
        return _error_response(401, "authentication_error", "Invalid bridge token")

    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, "invalid_request_error", f"Malformed JSON body: {exc}")

    model = body.get("model") or ""
    wants_stream = bool(body.get("stream"))
    to_agentaus, why = _route(request, body, model, main_loop=True)

    _request_id.set(_new_request_id())
    messages_in = len(body.get("messages") or [])
    rlog(
        logging.INFO,
        "recv model=%s route=%s stream=%s msgs=%d est=%d bytes=%d",
        model or "(none)",
        (f"agentaus ({why})" if why else "agentaus" if to_agentaus else "anthropic"),
        wants_stream,
        messages_in,
        estimate_request_tokens(body),
        len(raw),
    )

    if settings.log_bodies:
        rlog(logging.INFO, "request body: %s", json.dumps(body)[: settings.log_body_chars])

    if to_agentaus:
        return await _handle_agentaus(request, body, model, wants_stream)
    return await _passthrough(request, raw)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Response:
    if not _client_authorised(request):
        return _error_response(401, "authentication_error", "Invalid bridge token")

    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        body = {}
    model = body.get("model") or ""
    if _route(request, body, model, main_loop=False)[0]:
        # Agentaus exposes no tokenizer, so this is a char/4 estimate. It only feeds
        # Claude Code's context meter and auto-compact trigger, not billing.
        return JSONResponse({"input_tokens": estimate_request_tokens(body)})
    return await _passthrough(request, raw)
