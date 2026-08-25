"""Configuration for the Agentaus <-> Claude Code bridge.

Every setting is an environment variable so the bridge can be driven from a
.env file, a shell profile, or a systemd/launchd unit without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _csv(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


@dataclass
class Settings:
    # --- where the bridge listens -------------------------------------------------
    host: str = field(default_factory=lambda: os.environ.get("BRIDGE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("BRIDGE_PORT", 8787))

    # --- Agentaus upstream --------------------------------------------------------
    agentaus_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "AGENTAUS_BASE_URL", "https://agentaus.com.au"
        ).rstrip("/")
    )
    agentaus_path: str = field(
        default_factory=lambda: os.environ.get(
            "AGENTAUS_PATH", "/api/v1/chat/completions"
        )
    )
    agentaus_api_key: str = field(
        default_factory=lambda: os.environ.get("AGENTAUS_API_KEY", "")
    )
    # Agentaus injects its own "I am Agentaus" persona unless we overwrite it.
    # Claude Code's system prompt IS the agent, so overwriting is the default.
    system_prompt_overwrite: bool = field(
        default_factory=lambda: _bool("AGENTAUS_SYSTEM_PROMPT_OVERWRITE", True)
    )
    # Ask Agentaus to stream. Its stream is currently buffered (one chunk), but
    # honouring it means the bridge benefits automatically if that improves.
    upstream_stream: bool = field(
        default_factory=lambda: _bool("AGENTAUS_UPSTREAM_STREAM", True)
    )

    # --- Anthropic passthrough ----------------------------------------------------
    anthropic_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "ANTHROPIC_UPSTREAM_BASE_URL", "https://api.anthropic.com"
        ).rstrip("/")
    )
    # When true, a model the bridge does not recognise as Agentaus is forwarded
    # untouched to Anthropic. Turn off to run Agentaus-only.
    passthrough_enabled: bool = field(
        default_factory=lambda: _bool("BRIDGE_PASSTHROUGH", True)
    )

    # --- routing ------------------------------------------------------------------
    # Any model id containing one of these substrings routes to Agentaus.
    agentaus_model_markers: list[str] = field(
        default_factory=lambda: _csv("AGENTAUS_MODEL_MARKERS", ["agentaus"])
    )
    # Send *everything* to Agentaus, ignoring the model id entirely.
    force_all_to_agentaus: bool = field(
        default_factory=lambda: _bool("AGENTAUS_FORCE_ALL", False)
    )

    # --- stream shaping -----------------------------------------------------------
    # Claude Code aborts a stream that emits no bytes for 300s. Agentaus is silent
    # until the whole answer is ready, so the bridge emits its own ping events.
    ping_interval_seconds: float = field(
        default_factory=lambda: float(_int("BRIDGE_PING_INTERVAL", 10))
    )
    # Re-chunk buffered text so the terminal renders progressively. 0 disables.
    chunk_chars: int = field(default_factory=lambda: _int("BRIDGE_CHUNK_CHARS", 60))

    # --- timeouts -----------------------------------------------------------------
    connect_timeout: float = field(default_factory=lambda: float(_int("BRIDGE_CONNECT_TIMEOUT", 30)))
    read_timeout: float = field(default_factory=lambda: float(_int("BRIDGE_READ_TIMEOUT", 1800)))

    # --- retries ------------------------------------------------------------------
    # Transient upstream failures (DNS blips, connection resets, 502/503/504 from a
    # gateway) otherwise surface in Claude Code as a hard "API Error 502" that kills
    # the turn. Retrying is only safe before any content has been emitted, which the
    # streaming path enforces.
    max_retries: int = field(default_factory=lambda: _int("BRIDGE_MAX_RETRIES", 4))
    retry_backoff_seconds: float = field(
        default_factory=lambda: _float("BRIDGE_RETRY_BACKOFF", 0.5)
    )
    # Ceiling on a single backoff wait. Claude Code gives up on a silent stream at
    # 300s, so unbounded doubling would trade a fast error for a hung turn.
    retry_max_delay_seconds: float = field(
        default_factory=lambda: _float("BRIDGE_RETRY_MAX_DELAY", 30.0)
    )

    # --- Agentaus context window ---------------------------------------------------
    # Agentaus rejects anything over max_model_len with:
    #   "The engine prompt length N exceeds the max_model_len 131072"
    # and - when streaming - reports it as HTTP 200 with the error inside the SSE
    # body, which is easy to mistake for an empty reply.
    #
    # This default lives in CODE, not .env, on purpose: it is a property of the
    # Agentaus model rather than a user preference, so a fresh clone with no .env
    # (or an .env someone trimmed) still gets the correct limit. The env var is an
    # escape hatch for when Trellis Data changes the window.
    agentaus_max_input_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_MAX_INPUT_TOKENS", 131072)
    )
    # Whether the operator set the window explicitly. If they did, a value the bridge
    # later learns from an Agentaus error must not silently override their choice.
    max_input_tokens_is_explicit: bool = field(
        default_factory=lambda: bool(os.environ.get("AGENTAUS_MAX_INPUT_TOKENS", "").strip())
    )
    # When a conversation exceeds the window, drop the oldest messages so the turn
    # still succeeds instead of dying. This is what /compact does in effect, and it
    # is the only automatic recovery available to the bridge: Claude Code's model
    # picker and /compact are client-side and cannot be driven from here.
    #
    # On by default because the alternative is a hard failure with no way forward on
    # Agentaus - /compact cannot rescue it either, since compaction must itself fit
    # in the window. Set false to get the error instead of silent trimming.
    agentaus_auto_trim: bool = field(
        default_factory=lambda: _bool("AGENTAUS_AUTO_TRIM", True)
    )
    # Compact at this fraction of the window rather than waiting for it to be full.
    # Running a turn at 99% leaves no headroom: one large tool result tips it over and
    # the user sees a failure. Compacting at 80% keeps a working margin.
    agentaus_compact_threshold: float = field(
        default_factory=lambda: _float("AGENTAUS_COMPACT_THRESHOLD", 0.8)
    )
    # How much of the budget stays verbatim. The rest becomes the summarised head.
    agentaus_keep_fraction: float = field(
        default_factory=lambda: _float("AGENTAUS_KEEP_FRACTION", 0.5)
    )
    # Re-read the source after summarising to recover specifics the first pass missed.
    # Costs an extra call per chunk and is the largest single fidelity gain.
    agentaus_verify_summary: bool = field(
        default_factory=lambda: _bool("AGENTAUS_VERIFY_SUMMARY", True)
    )
    # Concurrent summarisation calls. Higher is faster on long histories.
    agentaus_summary_concurrency: int = field(
        default_factory=lambda: _int("AGENTAUS_SUMMARY_CONCURRENCY", 8)
    )
    # Tokens of conversation per summarisation call. Smaller means more calls, but
    # each returns faster and is far less likely to hit an origin timeout - a 32k
    # chunk took long enough that Cloudflare answered 524. Smaller chunks also raise
    # fidelity: a fact is a larger share of its chunk, so it is less likely to be
    # judged unimportant and dropped.
    agentaus_summary_chunk_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_SUMMARY_CHUNK_TOKENS", 6000)
    )

    # --- Agentaus compensation ------------------------------------------------------
    # Applied ONLY to Agentaus turns. Claude models keep default Claude Code behaviour:
    # the compensation exists for a specific capability gap, and adding it where there
    # is no gap would only cost latency.
    #
    # Supplement Claude Code's system prompt with operating notes aimed at the failure
    # modes actually observed from Agentaus - repeated tool calls, guessing at APIs,
    # happy-path-only code, editing before planning.
    agentaus_guidance: bool = field(
        default_factory=lambda: _bool("AGENTAUS_GUIDANCE", True)
    )
    # Have the model review its own answer and revise it when defects are found.
    # "What is wrong with this?" is a much easier question for a smaller model than
    # getting it right first time, which is what makes the extra round trip pay.
    agentaus_self_review: bool = field(
        default_factory=lambda: _bool("AGENTAUS_SELF_REVIEW", True)
    )
    # Answers shorter than this skip the review: they are usually acknowledgements.
    agentaus_review_min_chars: int = field(
        default_factory=lambda: _int("AGENTAUS_REVIEW_MIN_CHARS", 200)
    )

    # How many times to recompact and retry when Agentaus itself rejects the prompt
    # as too long. Preferred over refusing on our own estimate: the API states its real
    # limit in the rejection, which is better evidence than anything computed here.
    agentaus_fit_attempts: int = field(
        default_factory=lambda: _int("AGENTAUS_FIT_ATTEMPTS", 3)
    )
    # How much tighter each retry aims. 0.6 shrinks fast enough to converge in a few
    # attempts without discarding far more of the conversation than necessary.
    agentaus_fit_shrink: float = field(
        default_factory=lambda: _float("AGENTAUS_FIT_SHRINK", 0.6)
    )

    # --- misc ---------------------------------------------------------------------
    log_level: str = field(default_factory=lambda: os.environ.get("BRIDGE_LOG_LEVEL", "info"))
    log_bodies: bool = field(default_factory=lambda: _bool("BRIDGE_LOG_BODIES", False))
    # Optional shared secret clients must present. Leave empty for localhost use.
    bridge_token: str = field(default_factory=lambda: os.environ.get("BRIDGE_TOKEN", ""))

    def routes_to_agentaus(self, model: str) -> bool:
        if self.force_all_to_agentaus:
            return True
        lowered = (model or "").lower()
        return any(marker in lowered for marker in self.agentaus_model_markers)

    @property
    def agentaus_url(self) -> str:
        return f"{self.agentaus_base_url}{self.agentaus_path}"


settings = Settings()
