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
    connect_timeout: float = field(default_factory=lambda: float(_int("BRIDGE_CONNECT_TIMEOUT", 15)))
    read_timeout: float = field(default_factory=lambda: float(_int("BRIDGE_READ_TIMEOUT", 600)))

    # --- retries ------------------------------------------------------------------
    # Transient upstream failures (DNS blips, connection resets, 502/503/504 from a
    # gateway) otherwise surface in Claude Code as a hard "API Error 502" that kills
    # the turn. Retrying is only safe before any content has been emitted, which the
    # streaming path enforces.
    max_retries: int = field(default_factory=lambda: _int("BRIDGE_MAX_RETRIES", 2))
    retry_backoff_seconds: float = field(
        default_factory=lambda: _float("BRIDGE_RETRY_BACKOFF", 0.5)
    )
    # Ceiling on a single backoff wait. Claude Code gives up on a silent stream at
    # 300s, so unbounded doubling would trade a fast error for a hung turn.
    retry_max_delay_seconds: float = field(
        default_factory=lambda: _float("BRIDGE_RETRY_MAX_DELAY", 8.0)
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
