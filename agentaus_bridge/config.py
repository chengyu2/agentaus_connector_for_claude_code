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
    # Snap the compaction boundary to a multiple of this many messages. Unquantised,
    # the boundary advances every turn, the summary cache never hits, and every single
    # turn re-summarises the whole history - 173 seconds per turn on a real session.
    agentaus_compact_block: int = field(
        default_factory=lambda: _int("AGENTAUS_COMPACT_BLOCK", 20)
    )
    # Re-read the source after summarising to recover specifics the first pass missed.
    # Costs an extra call per chunk and is the largest single fidelity gain.
    agentaus_verify_summary: bool = field(
        default_factory=lambda: _bool("AGENTAUS_VERIFY_SUMMARY", True)
    )
    # Deprecated in favour of AGENTAUS_MAX_CONCURRENCY below, which caps every
    # bridge-initiated call rather than only summarisation. Still read when set, so an
    # existing .env keeps working. See gate.max_concurrency().
    agentaus_summary_concurrency: int = field(
        default_factory=lambda: _int("AGENTAUS_SUMMARY_CONCURRENCY", 8)
    )
    summary_concurrency_is_explicit: bool = field(
        default_factory=lambda: bool(
            os.environ.get("AGENTAUS_SUMMARY_CONCURRENCY", "").strip()
        )
    )
    # Tokens of conversation per summarisation call. Smaller means more calls, but
    # each returns faster and is far less likely to hit an origin timeout - a 32k
    # chunk took long enough that Cloudflare answered 524. Smaller chunks also raise
    # fidelity: a fact is a larger share of its chunk, so it is less likely to be
    # judged unimportant and dropped.
    agentaus_summary_chunk_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_SUMMARY_CHUNK_TOKENS", 6000)
    )

    # --- Bridge-initiated concurrency ------------------------------------------------
    # One global cap on every Agentaus call the bridge makes on its own initiative:
    # summarising, gap-checking, merging, self-review, query expansion and chunk search.
    # Global rather than per-component, because two components each capped at 6 permit
    # 12, and the number that matters is how hard the bridge hits one upstream.
    #
    # The user's own turn is deliberately NOT gated - see gate.py.
    # Stream the bridge's own helper calls instead of buffering them. A buffered call
    # holds a connection while the server composes the whole reply; a fan-out of those
    # is what saturates Agentaus. Streaming drains sooner and shows progress.
    # Off by default. Streaming a helper reply is sound in principle and returns first
    # bytes sooner, but an unattended fan-out found the failure mode: a stalled stream
    # holds its socket with nothing flowing, and the read timeout is sized for a main
    # turn, so the call sat for as long as the timeout allowed. Six of those and a batch
    # run stops making any upstream calls at all while looking perfectly healthy.
    # Enable it once the timeout below has been exercised under load.
    agentaus_stream_helpers: bool = field(
        default_factory=lambda: _bool("AGENTAUS_STREAM_HELPERS", False)
    )
    # Hard ceiling on ONE bridge-initiated call, streamed or buffered. Generous on
    # purpose: a slow call is not a failed one, and abandoning work that was still
    # coming wastes the whole call. It exists to bound a socket that will never speak
    # again, not to give up on a busy upstream - 6 abandoned calls in one run were all
    # of the second kind.
    agentaus_helper_timeout_seconds: float = field(
        default_factory=lambda: _float("AGENTAUS_HELPER_TIMEOUT", 900.0)
    )
    agentaus_max_concurrency: int = field(
        default_factory=lambda: _int("AGENTAUS_MAX_CONCURRENCY", 8)
    )
    max_concurrency_is_explicit: bool = field(
        default_factory=lambda: bool(
            os.environ.get("AGENTAUS_MAX_CONCURRENCY", "").strip()
        )
    )

    # --- Office documents -----------------------------------------------------------
    # Word documents and spreadsheets are zip archives, so the bridge used to skip them
    # as binary - which meant search and zoom could not see a tender response or a
    # requirements matrix, which is where that material actually lives. LibreOffice
    # converts them with their tables intact.
    #
    # On by default: reading a .docx as noise is never the behaviour anyone wanted. Set
    # false to go back to skipping them.
    agentaus_office_extract: bool = field(
        default_factory=lambda: _bool("AGENTAUS_OFFICE_EXTRACT", True)
    )
    # Path to `soffice`. Empty means look in the usual places, then $PATH.
    soffice_path: str = field(
        default_factory=lambda: os.environ.get("AGENTAUS_SOFFICE_PATH", "").strip()
    )
    # A conversion that hangs must not hang the turn that touched the file.
    agentaus_office_timeout_seconds: float = field(
        default_factory=lambda: _float("AGENTAUS_OFFICE_TIMEOUT", 120.0)
    )

    # --- Bridge-executed search -----------------------------------------------------
    # Agentaus is handed Grep - a regex tool - as its way of finding things, and a
    # smaller model writes a regex against a guess about what the code looks like. This
    # replaces that with a search that reads by meaning: shortlist, chunk, read every
    # chunk with a real model call, merge the hits.
    agentaus_search: bool = field(default_factory=lambda: _bool("AGENTAUS_SEARCH", True))
    # Expose Agentaus' own web search as a tool. Claude Code's WebSearch is an Anthropic
    # server-side stub with no input_schema, so it is dropped in translation and an
    # Agentaus turn otherwise has no way to search the web at all.
    agentaus_web_search: bool = field(
        default_factory=lambda: _bool("AGENTAUS_WEB_SEARCH", True)
    )
    # Multi-angle investigation: three independent searches, and only what two of them
    # agreed on is reported as established. Slower than one search, so it is offered as
    # a separate tool rather than made the default.
    agentaus_investigate: bool = field(
        default_factory=lambda: _bool("AGENTAUS_INVESTIGATE", True)
    )
    # Zoom: read a cited passage in context. A search quotes enough to prove a fact is
    # there, not enough to paraphrase it accurately - so a model working from search
    # output alone writes "(evidence: lines 3585-3586)" where a substantive clause was
    # wanted. Observed on a real tender.
    agentaus_zoom: bool = field(default_factory=lambda: _bool("AGENTAUS_ZOOM", True))
    # How far either side of the cited lines to look for a section boundary.
    agentaus_zoom_radius_lines: int = field(
        default_factory=lambda: _int("AGENTAUS_ZOOM_RADIUS_LINES", 80)
    )
    # A floor on the window, in tokens. Boundary detection alone can return a
    # three-line "section" - tender and spec documents use a bold single line as a
    # sub-heading constantly - and three lines is technically a section and useless to
    # quote from. Below this, keep widening outward.
    #
    # In tokens, not lines, because lines are not a unit of size: 40 lines is 300 tokens
    # of source code and 4,000 tokens of tender prose. The line-counted version of this
    # setting, and a line-counted ceiling beside it, are what let a passage reach 25,000
    # characters and break the turn carrying it.
    agentaus_zoom_min_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_ZOOM_MIN_TOKENS", 600)
    )
    # Below this the passage is returned whole; above it, trimmed outward from the
    # citation so the nearest lines survive. There is no separate line ceiling: this is
    # the only size limit, and it is the one that matters.
    agentaus_zoom_max_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_ZOOM_MAX_TOKENS", 6000)
    )

    # --- Tool-result distillation ----------------------------------------------------
    # What exhausts the window is tool output, not conversation: one Read of a large
    # file can be most of the real headroom, and it stays in the transcript for the rest
    # of the session. Claude Code sends those results back to the bridge on the next
    # turn, so they can be condensed in flight - cached by content, so the conversation
    # prefix stays stable and the compaction cache keeps hitting.
    # OFF by default, on live evidence rather than principle. Distilling a 60k-character
    # document added a minute to one turn, and for work where the tool output IS the
    # subject - a tender document being edited, a log being read line by line -
    # condensing it destroys the thing the user asked about. It earns its place on long
    # agentic sessions that would otherwise compact; turn it on there and measure.
    agentaus_distill_results: bool = field(
        default_factory=lambda: _bool("AGENTAUS_DISTILL_RESULTS", False)
    )
    # Results smaller than this are left exactly as they are.
    agentaus_distill_threshold_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_DISTILL_THRESHOLD_TOKENS", 12000)
    )
    agentaus_distill_chunk_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_DISTILL_CHUNK_TOKENS", 4000)
    )

    # --- Truncated tool output -------------------------------------------------------
    # When a tool produces more than the client will carry, Claude Code saves the whole
    # output to a file and passes a 2 KB preview. Sensible for a client, terrible for a
    # model: one observed session surveyed a repository from 2 KB of a 1.6 MB listing and
    # invented the rest. The bridge runs on the same machine and reads the file.
    agentaus_restore_persisted: bool = field(
        default_factory=lambda: _bool("AGENTAUS_RESTORE_PERSISTED", True)
    )
    # Ceiling on what is read back. Beyond this the caller is told to narrow the command.
    agentaus_restore_max_bytes: int = field(
        default_factory=lambda: _int("AGENTAUS_RESTORE_MAX_BYTES", 400_000)
    )

    # --- Tool ledger -----------------------------------------------------------------
    # Derived from the conversation, never stored, and costs no Agentaus calls. Built
    # from the pre-compaction message list on purpose: the calls that fall out of the
    # window are exactly the ones the model forgets it already made.
    agentaus_tool_ledger: bool = field(
        default_factory=lambda: _bool("AGENTAUS_TOOL_LEDGER", True)
    )
    agentaus_tool_ledger_limit: int = field(
        default_factory=lambda: _int("AGENTAUS_TOOL_LEDGER_LIMIT", 40)
    )
    # Tokens of file content per search call. Agentaus has a 131k window and a helper
    # call carries no system prompt, so most of that window was going unused - and the
    # cost of NOT using it is not latency, it is call volume. Measured on an 89,579-token
    # tender document, searching for facts known to be in it:
    #
    #    8000 tok  ->  25s, 10 calls, 2676 chars of evidence, 5/5 facts found
    #   48000 tok  ->  23s,  4 calls, 1782 chars,             5/5 facts found
    #
    # Same wall-clock and same recall for 60% fewer calls. That matters because the real
    # failure mode is saturating Agentaus: three concurrent searches at 8k queue ~30
    # requests behind a cap of 6 and the whole run stalls, which is how a batch job came
    # to make no progress at all. Bigger chunks quote less back per hit, so this is a
    # trade - but fewer, larger requests are markedly kinder to a busy upstream.
    agentaus_search_chunk_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_SEARCH_CHUNK_TOKENS", 48000)
    )
    # Ceiling on calls for one search. Truncation is reported in the result and logged -
    # a silent cap reads as full coverage, which is worse than a stated partial one.
    agentaus_search_max_chunks: int = field(
        default_factory=lambda: _int("AGENTAUS_SEARCH_MAX_CHUNKS", 120)
    )
    # Below this many shortlisted files, distrust the shortlist and read everything.
    # This is the case a keyword prefilter gets wrong: the words simply are not there.
    agentaus_search_min_candidates: int = field(
        default_factory=lambda: _int("AGENTAUS_SEARCH_MIN_CANDIDATES", 3)
    )
    # Ceiling on files one search will read. The shortlist is ranked, so this keeps the
    # best-matching ones. Measured live: without it a loose expansion shortlisted 21 of
    # 40 files and the search took 75 seconds.
    # Aim before reading: build a free outline of the candidate files, spend ONE call
    # asking which sections matter, then read only those. The outline costs no upstream
    # request at all, and on a 434 KB tender response it is 9,276 tokens of structure
    # standing in for 89,579 tokens of content.
    #
    # This is symbol indexing applied to the corpus that is actually here. A Tree-sitter
    # index answers "where is this function defined" without a fan-out, and answers
    # nothing about a tender response, which has no functions in it. Documents have
    # headings; those are the addressable structure.
    agentaus_search_outline_first: bool = field(
        default_factory=lambda: _bool("AGENTAUS_SEARCH_OUTLINE_FIRST", True)
    )
    # Sections one aimed search will read. Beyond this it is cheaper to read everything.
    # Tokens per aimed section. Deliberately NOT the chunk size: a chunk is sized to
    # cover a whole file in a few reads, and a section is a passage around one citation.
    # Reusing the chunk budget here made the first window swallow the file and every
    # later pick get skipped as overlapping, so an aimed search read one chunk where an
    # unaimed one read eleven - the opposite of the point.
    agentaus_search_section_tokens: int = field(
        default_factory=lambda: _int("AGENTAUS_SEARCH_SECTION_TOKENS", 3000)
    )
    agentaus_search_max_sections: int = field(
        default_factory=lambda: _int("AGENTAUS_SEARCH_MAX_SECTIONS", 8)
    )
    agentaus_search_max_candidates: int = field(
        default_factory=lambda: _int("AGENTAUS_SEARCH_MAX_CANDIDATES", 12)
    )
    agentaus_search_max_file_bytes: int = field(
        default_factory=lambda: _int("AGENTAUS_SEARCH_MAX_FILE_BYTES", 1024 * 1024)
    )
    # Colon-separated directories the search may read. Empty means any absolute path the
    # model names, which matches what Claude Code's own Read tool would allow. Set it to
    # confine the bridge to specific trees.
    agentaus_search_roots: str = field(
        default_factory=lambda: os.environ.get("AGENTAUS_SEARCH_ROOTS", "").strip()
    )
    # How many rounds of bridge-executed tool calls one turn may run before the answer
    # has to stand. Stops a model that keeps searching from never replying.
    # 3 was chosen when search was the only bridge tool. A real workflow is now search,
    # then a zoom per citation, then the answer - and a zoom costs a round despite being
    # free (no model call, returned verbatim). Observed live: an evidence review died at
    # zoom, search, zoom with the work half done. The runaway protection that matters is
    # that every round needs the model to emit another tool call.
    agentaus_tool_rounds: int = field(
        default_factory=lambda: _int("AGENTAUS_TOOL_ROUNDS", 12)
    )
    # Rounds spent telling the model a tool it named does not exist. Counted separately
    # from tool rounds on purpose: a correction is the bridge fixing its upstream's
    # mistake, and charging it to the budget for real work is what made a live turn run
    # out of rounds before it could answer.
    agentaus_correction_rounds: int = field(
        default_factory=lambda: _int("AGENTAUS_CORRECTION_ROUNDS", 3)
    )

    # --- Synthesised thinking --------------------------------------------------------
    # Agentaus has no native thinking, so it acts before it plans. The bridge runs one
    # planning call first and feeds the result into the answer call.
    agentaus_thinking: bool = field(default_factory=lambda: _bool("AGENTAUS_THINKING", True))
    # Show the plan as a real thinking block. Turn off if the client mishandles an
    # unsigned one; the plan is then folded into the answer call without being shown.
    agentaus_thinking_visible: bool = field(
        default_factory=lambda: _bool("AGENTAUS_THINKING_VISIBLE", True)
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
    # Check an answer against the tools the turn actually ran. This is the pass ordinary
    # self-review cannot be - the reviewer sees the ledger of executed calls, so "you
    # claim this about a file you never opened" is checkable rather than a guess. It runs
    # on exactly the turns review has to sit out, which is where claims outrun evidence.
    agentaus_grounding_check: bool = field(
        default_factory=lambda: _bool("AGENTAUS_GROUNDING_CHECK", True)
    )
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
    # How much of a body to log. The old fixed 4000 was shorter than the system prompt
    # the bridge builds, so the interesting part - the tool list and the tool_selection
    # block - fell off the end of exactly the line you were reading the log to see.
    log_body_chars: int = field(
        default_factory=lambda: _int("BRIDGE_LOG_BODY_CHARS", 20000)
    )
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
