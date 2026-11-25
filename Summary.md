# Agentaus Bridge — Plain-English Summary

*The readable version. For the full, exhaustive reference see `Knowledgebase.md`.*

---

## One paragraph

This repo is a small **local server** (the "bridge") that lets **Agentaus** — Trellis
Data's sovereign Australian model — run inside **Claude Code** at the same time as the
normal Claude models, so you can flip between them mid-session with `/model`. Claude Code
speaks only Anthropic's API; Agentaus speaks an OpenAI-style API. The bridge translates
between the two and routes each turn to the right place: model `agentaus` goes to Canberra,
anything else goes to Anthropic completely untouched.

```text
Claude Code  ──►  bridge (translates + babysits)  ──►  agentaus.com.au   (model = agentaus)
                        └─────────────────────────────►  api.anthropic.com  (everything else)
```

## Why Agentaus needs looking after

Agentaus is capable but behaves differently from Claude in a handful of specific,
repeatable ways, and the whole bridge exists to compensate for those gaps:

- **It speaks the wrong API** — so the bridge translates in both directions and streams.
- **It has a smaller memory window** — so the bridge *compacts* long conversations into a
  summary (kept in the system prompt) so long sessions don't break.
- **It guesses** — tool names, answers from files it never opened, regex against what it
  *thinks* the code looks like. So the bridge gives it search that reads by meaning,
  validates its tool calls, checks its answers against what it actually ran, and catches
  refusals.
- **It forgets** — that it already ran a tool, that a workflow is still going, what the
  session is trying to do. So the bridge keeps a ledger of what it's done and mirrors the
  task list back to it.
- **It can't see documents** on its own — `.docx`/`.xlsx`/PDF are zip archives or binary.
  So the bridge reads them itself (LibreOffice for office files, a PDF ladder with OCR)
  and hands Agentaus the text.
- **Its own plans and workflows are weak** — so the bridge plans its turns for it, and
  when it wants a multi-agent `Workflow`, the bridge swaps in its **own** reliable graph
  script instead of the one Agentaus wrote.

None of this touches Claude models — they pass through byte-for-byte, because adding
compensation where there's no gap only adds latency and noise.

## The smart parts, in plain English

- **Per-request routing** — the bridge keeps both models in one session; it doesn't
  replace anything.
- **Search that reads by meaning** (`agentaus_search`) — finds answers even when the
  question shares no words with the answer. It expands the question into likely code
  terms, shortlists files, aims at likely sections, then reads the promising chunks in
  parallel. `agentaus_zoom` opens a quoted passage verbatim so it can quote properly, and
  `agentaus_investigate` checks three different angles and only trusts what two agree on.
- **Documents it can actually read** — `.docx`/`.xlsx`/`.pptx` via LibreOffice (tables
  kept as rows with ` | ` between cells), PDFs via a ladder of extractors (best text first,
  then OCR for scans).
- **Self-correction built in** — plans each turn, reviews its own answer for real defects,
  checks claims against the tools it actually ran, checks its Python parses, and nudges it
  if it refuses to use the tools it has.
- **Resilience against a flaky upstream** — Agentaus sits behind Cloudflare, which throws
  timeout errors when busy. The bridge learns how many parallel calls the upstream can
  take (halves on failure, creeps back up on success, like TCP), retries transient
  failures sensibly, and runs each turn detached from its client so a cough doesn't kill
  it. A stream is so much more reliable than a buffered call that even buffered clients
  get their answer streamed and reassembled.
- **Every knob is a setting** — a long list of environment variables (`AGENTAUS_*`,
  `BRIDGE_*`) tune all of it, each with sensible measured defaults.

## How it's put together

| Part | What it does |
| --- | --- |
| `agentaus_bridge/` | The whole server — 55 Python modules. |
| `translate.py` + `tokens.py` | Convert Anthropic ↔ OpenAI messages; count tokens and calibrate against how many Agentaus actually charges (it's a fixed ~2,400-token overhead, not a ratio). |
| `augment.py` | The "brain" — all the guidance, planning, review, grounding, refusal-catching, skill injection, tool-budget trimming. |
| `compact.py` | Conversation summariser so long sessions fit the window; cached so it costs once per boundary move, not every turn. A cold compaction is capped by `AGENTAUS_COMPACT_TIMEOUT` (default 180 s): a long summarise falls back to trim/clip at 180 s instead of hanging to `BRIDGE_TURN_TIMEOUT` (900 s). |
| `tools.py` | The bridge's own tools (`agentaus_search`, `agentaus_zoom`, `agentaus_investigate`, `agentaus_web_search`, `agentaus_inventory`). |
| `server.py` | The HTTP surface: startup, health, the model list, and routing each request to Agentaus or to Anthropic. |
| `agentaus_turn.py` + `agentaus_stream.py` | One Agentaus turn, buffered or streamed. Their helpers: `upstream_http.py` (retries), `context_window.py` (fitting the window), `helper_calls.py` (planning, review, grounding), `tool_handling.py`, `graph_turn.py`, `stall_check.py`, `stream_delivery.py`, `passthrough.py`, `request_log.py`, `bridge_responses.py`, `conversation_text.py`. |
| `status.py` + `notify.py` | health dashboard. `status.py` is the "single source of truth for what is wrong right now": Incident records for rate-limits (429), quota (402), rejected keys (401/403), connection loss and slow upstream (504/520/522/524); `notify.py` turns hard incidents into Windows desktop toasts immediately and soft ones once they have lasted `AGENTAUS_NOTIFY_AFTER_S`. The in-chat "Still waiting for Agentaus (Ns)" heartbeat and the `/status` page are always on; toasts are Windows-only. |
| `graph.py` + `graphs/` | The reliable multi-agent workflow script the bridge substitutes for Agentaus's own. |
| `gate.py` + `throttle.py` | Keep the bridge from hammering the upstream: one shared, priority-aware cap on helper calls, and one adaptive cap on everything. |
| `flight.py` + `sessions.py` | Keep turns alive when clients disconnect, and make subagents follow their session's model. |
| `documents.py` + `pdf.py` | Read office files and PDFs (tables intact; OCR on scans). |
| `tests/` | 48 files / ~1070 tests, all offline except a few live ones, plus real-bridge integration and smoke scripts. |
| `abbench/` + `benchmarks/` | The harnesses that proved the "tuned" bridge actually helps — Opus vs Agentaus on HumanEval, MMLU, and economics tasks through the real Claude Code CLI. |

## Did it actually work? Yes — the numbers

The A/B harness ran Opus and Agentaus through the real Claude Code CLI on held-out test
sets (so nothing was fitted to what was scored):

- **HumanEval+**: tuned Agentaus **0.939** — tied with Opus **0.939**.
- **MMLU**: tuned **0.807** vs Opus 0.933 — and the *un*tuned base bridge scored just
  **0.047** (it produced no answer in most batches). The bridge took Agentaus from broken
  to within ~12 points of Opus.
- **MMLU-Pro**: tuned **0.707** vs Opus 0.867 (base **0.0**).
- **Economics tasks**: tuned **1.0**, equal to Opus (base 0.63 in the benchmark ledger).

The cost: tuned turns run about 2–4× slower than Opus, because all that checking and
searching takes calls. That's the honest trade — the bridge trades wall-clock for
correctness.

## Using it

1. Claude Code must already work by itself.
2. Run the bridge — on this machine `start-bridge.cmd` (it sets `PYTHONUTF8=1`, without
   which the repo's file reads crash as cp1252).
3. Point Claude Code at it (`ANTHROPIC_BASE_URL=127.0.0.1:8787`) in the project you
   actually work in, restart, and `/model` → **Agentaus (Trellis Data)**.
4. Ask for files explicitly ("and write it to `path`") — the bridge never writes files;
   Claude Code does the writing through its normal permission prompt.

## Watch-outs (worth knowing)

- **The settings only apply where they're stored.** This repo's `.claude/settings.json`
  only works when this repo is the open folder — put your bridge settings where you work.
- **A plaintext API key sits in `agentaus_api_doc.md`**, which is *not* gitignored. Remove
  and rotate it before this repo is shared anywhere.
- **No prompt caching** — every Agentaus turn re-sends the whole conversation, so long
  sessions cost more per turn.
- **Only the core packages are required** — without `fastapi`, `uvicorn`, `httpx` and
  `tiktoken` the bridge refuses to start and says what to install. A missing document
  reader is a warning, not a block: an absent LibreOffice means office documents are
  skipped, but the bridge still runs.
- **On Windows**: `AGENTAUS_SEARCH_ROOTS` must stay empty (it splits on `:`, which breaks
  drive letters) and `BRIDGE_TOKEN` must stay empty (Claude Code sends the claude.ai login
  as the token).
- **Windows-only toast layer** — `notify.py` pops desktop notifications (`AGENTAUS_NOTIFY=toast`
  turns them on; hard incidents at once, soft ones after `AGENTAUS_NOTIFY_AFTER_S`, per-kind
  cooldown `AGENTAUS_NOTIFY_COOLDOWN_S`). macOS/Linux get only the always-on heartbeat and
  `/status` page.
- **The `/status` page and `/status.json` (GET)** show current incidents worst-first and recent
  history — "cheap, read-only, no model calls". The in-chat "Still waiting for Agentaus (Ns)"
  heartbeat is a static beat and never consults status.

## Where to go deeper

- [Knowledge Base](Knowledgebase.md) — the full reference: every module, every setting, every measured
  number and the reasoning behind it.
- `README.md` — the project's own long-form documentation, in the team's voice.
  