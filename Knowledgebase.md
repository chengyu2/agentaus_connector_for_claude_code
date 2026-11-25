# Agentaus Bridge — Complete Knowledgebase

The exhaustive reference for the connector that runs **Agentaus** (Trellis Data's sovereign
Australian model) inside **Claude Code** alongside the stock Claude models. This document
records what every module does, why it exists, how the pieces interconnect, the measured
numbers behind the design decisions, and the task/benchmark evidence. It is the source of
truth; `Summary.md` is the human-readable condensation of this file.

---

## 1. What the project is

A small local HTTP server (**the bridge**) that makes **Agentaus** available as a model
inside **Claude Code**, switchable mid-session with `/model`, at the same time as the
built-in Claude models.

The problem it solves is translation and compensation:

- **Claude Code** speaks the **Anthropic Messages API** only.
- **Agentaus** speaks an **OpenAI-style chat-completions API** (`/api/v1/chat/completions`).
- The bridge listens on `127.0.0.1:8787` by default and Claude Code is pointed at it via
  `ANTHROPIC_BASE_URL`. Each request is routed **per request by the `model` field**:
  - `model` containing `agentaus` → translated to the Agentaus format and sent to
    `agentaus.com.au`.
  - anything else → forwarded **byte-for-byte** to `api.anthropic.com` with the caller's own
    credentials. Claude models behave exactly as if the bridge were not there.

```text
Claude Code (CLI)
   │  ANTHROPIC_BASE_URL=127.0.0.1:8787
   ▼
agentaus_bridge (this repo)          routes on the request's `model` field
   ├── model=agentaus  ──►  agentaus.com.au /api/v1/chat/completions  (translated)
   └── model=claude-*  ──►  api.anthropic.com                          (forwarded untouched)
```

The bridge is deliberately the *last* piece of a working setup: it does not install Claude
Code, cannot make it work, and only adds a second model to one that already runs. It also
**never writes deliverable files** — when Agentaus calls `Write` or `Edit`, Claude Code
executes them through its own permission prompt. The only files the bridge touches are its
own log and its caches.

### Why Agentaus needs a bridge at all

Agentaus is described as a "general-purpose assistant with a 131k window" (the repo's
standing measure — note `CLAUDE.local.md` records a newer, larger window measured
24 Nov 2026, see §3) driven by a system prompt written for a much stronger agentic model
(Claude). The gap shows up as specific, repeatable behavioural defects rather than "worse
output", and each is addressed with instruction or a targeted mechanism:

- re-calls a tool it already ran, having lost track of what it did;
- answers from a guess about an API rather than reading the file in front of it;
- produces code that handles the happy path and ignores edge cases;
- starts editing before working out what the change requires;
- writes a regex against a *guess* about what the code looks like, gets a plausible match,
  and answers from it (the reason `agentaus_search` exists);
- cannot see `.docx`/`.xlsx`/PDF content on its own (they are zip archives or binary);
- writes tool calls into its prose as angle-bracket markup instead of issuing real calls;
- forgets it already ran a tool; plans before it acts; invents tool names;
- struggles with one-line self-written workflow scripts.

None of the compensation touches Claude models — those are forwarded byte-for-byte and keep
default Claude Code behaviour, because applying compensation where there is no gap would
only add latency and noise.

---

## 2. Repository layout

| Path | What it is |
| --- | --- |
| `agentaus_bridge/` | The package: 38 Python modules, ~800 KB. The connector itself. |
| `agentaus_bridge/graphs/` | `solve.js`, `dynamic.js` (the agent-graph scripts), `skills/` (8 markdown skill files). |
| `agentaus_bridge/skill_library/` | 9 bundled skills adapted from Anthropic's plugins, with LICENSE and NOTICE. |
| `tests/` | 48 `.py` test files (1070 test functions) + 2 `.mjs` graph dry-runs + `integration_test.py` + `smoke_test.py` + `conftest.py` + `stub_server.py` + `xml_style.py`. |
| `benchmarks/` | Thin benchmark harness: `run.py`, `harness.py`, `baselines.py`, `humaneval.py`, `retrieval.py`, `injection.py`, `documents.py`, `coverage.py`, `data/`, `results/`. |
| `abbench/` | The heavy A/B harness: Opus vs Agentaus through the real Claude Code CLI. `ab.py`, `bridges.sh`, round scripts, `ultra/`, `results/`, `report/`. |
| `.claude/skills/` | 10 project skills (analyse-data, bridge-diagnose, find-in-code, investigate, multi-step-work, read-documents, repo-survey, research, search-exhaustively, tender-evidence). |
| `scripts/` | `install.sh`, `start-bridge.sh`, `claude-agentaus.sh` (macOS/launchd), `diagnose.py`. |
| Root | `README.md` (135 KB master doc), `.env`/`.env.example`, `agentaus_api_doc.md`, `requirements.txt`, `.gitignore`, Windows launchers `start-bridge.cmd`, `claude-agentaus.cmd`, `.1.cmd`. |

---

## 3. Config — `agentaus_bridge/config.py`

Every setting is an **environment variable** so the bridge can be driven from a `.env`,
shell profile, or a systemd/launchd unit without code changes. Parsed by small helpers
`_bool` / `_int` / `_float` / `_csv` (CSV lists are lower-cased) into a single `Settings`
dataclass, instantiated once at import as `settings`.

**Where the bridge listens:** `BRIDGE_HOST` (127.0.0.1), `BRIDGE_PORT` (8787).

**Agentaus upstream:** `AGENTAUS_BASE_URL` (`https://agentaus.com.au`), `AGENTAUS_PATH`
(`/api/v1/chat/completions`), `AGENTAUS_API_KEY`. `AGENTAUS_SYSTEM_PROMPT_OVERWRITE`
(default **true**): Agentaus injects its own "I am Agentaus" persona unless overwritten;
because Claude Code's system prompt *is* the agent, overwriting is the default — without it
Agentaus prepends its persona ahead of the agent prompt, costing ~2.2k tokens and fighting
the tool instructions.

**Streaming:** `AGENTAUS_STREAM_HELPERS` — ask Agentaus to stream for *every* call, whether
or not the client asked. Measured (d13/d14 slumps): buffered calls got Cloudflare 524 about
**76 times in 109**; streamed **7 in 38** over the same windows; d12 streamed successes ran
up to 263 s — so the stream almost always helped.

**Window:** `AGENTAUS_MAX_INPUT_TOKENS` (131072 as documented; `CLAUDE.local.md` records
1,048,576 measured 24 Nov 2026) — the largest prompt+reply Agentaus accepts. `max_tokens`
is allowed up to 131072 output.

**Routing:** `AGENTAUS_MODEL_MARKERS` (CSV), `AGENTAUS_FORCE_ALL` (bool).

**Compensation pass toggles:** self-review (`AGENTAUS_SELF_REVIEW`), planning, grounding, refusal
detection, stall handling, tool-budget trimming, thinking visibility, workflow graph, import
guard, restore-persisted (all `bool`, mostly default on).

**Full settings list** (each `AGENTAUS_*` / `BRIDGE_*`): search chunk/min/max candidates,
max files, max chunks, max sections, section tokens, outline-first, max file/document bytes;
zoom max/min tokens, radius lines, zoom on; pdf extract/timeout/min chars per page/dpi/ocr/
ocr-max-pages; office extract/timeout/document-cache-chars/soffice path; tool timeout;
restore-max-bytes; review-min-chars; concurrency (max, summary, upstream ceiling/floor/ramp);
retry backoff/max-delay/max-retries; stall-warning seconds; distill-results; fit shrink;
stream-idle seconds; flight grace/keep-done/turn-timeout; tool budget bytes; log level/bodies/
body-chars; bridge token; upstream/api config; graph kind/total; subagents-follow-session;
import-guard; prompt style; force-all; model markers.

`routes_to_agentaus(model)`: True if `AGENTAUS_FORCE_ALL` is set, else any marker in the
lower-cased model id. `agentaus_url` = base + path.

---

## 4. Startup requirements — `requirements.py`, `__init__.py`, `__main__.py`

**Only the core Python packages are required.** Without `fastapi`, `uvicorn`, `httpx` and
`tiktoken` the bridge **refuses to start** (exit 3) and says what to install. The document
readers — LibreOffice, poppler, tesseract, the PDF modules — are **optional components**:
a reader you chose at install but that is then missing logs a warning naming the command
that fixes it, and one you deliberately skipped is a single info line. The bridge still
starts without any of them: an absent LibreOffice means office documents are skipped, an
absent OCR tier means scanned PDF pages come back empty — it degrades loudly rather than
silently, but it does not refuse.

`SYSTEM` tier (external binaries): **LibreOffice** (`soffice` — reads .docx/.xlsx/.pptx with
tables intact), **poppler** (`pdftotext` — best PDF text tier with column/table layout;
`pdftoppm` — renders scanned pages for OCR), **tesseract** (OCR for scans).
`PYTHON` tier (pip modules): `pdfminer.six`, `pypdf`, `tree-sitter`, `tree-sitter-language-pack`,
and on macOS `pyobjc-framework-Vision` + `pyobjc-framework-Quartz` (macOS's own OCR).

`missing()` returns one line per missing requirement with its install command;
`report()` wraps them with "./scripts/install.sh" as the alternative. macOS vs Debian
commands given. (On this Windows machine, LibreOffice and Poppler are installed; macOS-only
Vision OCR does not exist — tesseract is the only OCR tier.)

`__main__.py` is the CLI entry (`python -m agentaus_bridge`) that runs the FastAPI server
(`uvicorn`).

---

## 5. Translation layer — `translate.py`

Pure translation between the Anthropic Messages API and Agentaus' OpenAI-style API. No I/O,
so every branch is unit-testable (`tests/test_translate.py`, 25 tests).

**Direction A — Claude Code → Agentaus: `anthropic_request_to_agentaus(body, ...)`.**

- Hoists the system prompt: both the top-level `system` and any `system`-role entries inside
  `messages` are gathered into one leading system message joined by "\n\n". Rationale:
  unhoisted, a system note lands *after* the user request and the model answers the note
  instead. Sets `payload["system_prompt_overwrite"] = True` unless disabled (paying ~2.2k
  extra tokens of Agentaus persona otherwise).
- Builds the message list: user text blocks, assistant text, tool calls (function-call
  objects), and tool results. Tool results are wrapped in a **tagged envelope** —
  `prompt_style.data("tool_result", text, tool=name)` — plus an explicit imperative: *"The
  content above is the real output of your own {tool} call. It is already here - use it to
  answer now, and never ask for it to be provided, pasted or uploaded."* Handed a bare
  60,000-character document as a `tool` message, Agentaus answered "the DOCX hasn't been
  provided" about half the time; tagging the result and saying plainly it is real output
  made it stop. Error results get `prompt_style.data("tool_error", ...)` + "That call
  failed. Do not treat its output as an answer."
- `_map_tools`: maps Anthropic tool defs → OpenAI function defs, **skipping server-side
  tool stubs** (tools with no `input_schema`, e.g. Anthropic server-side
  `web_search_*`/`code_execution_*` — Agentaus cannot execute these). Each mapped tool:
  `{"type":"function","function":{"name","description","parameters"}}` with a default empty
  parameters schema.
- `_map_tool_choice`: Anthropic `auto`→`auto`, `any`→`required`, `none`→`none`,
  `tool`+name→`{"type":"function","function":{"name":...}}`, else `auto`.
- **Grep/Bash restriction text**: `_GREP_RESTRICTION` and `_BASH_RESTRICTION` are long
  strings **prepended** to the Grep and Bash tool descriptions (a caveat appended after a
  paragraph loses to a strong prior). The schema is untouched: if the model does call Grep,
  Claude Code executes it exactly as before — only the advice about *when* to reach for it
  changes. Bash is only restricted for search-shaped uses; it remains how you run tests.
- `inject_bridge_tools(body, extra_tools)`: returns a **new** body (caller's unmutated),
  rewriting Grep/Bash descriptions, and appends any extra tools (the bridge's own) whose
  name is not already present. No-op unless `tools` is non-empty (a turn with no tools is
  not one where search was on offer).

**Direction B — Agentaus → Claude Code (non-stream): `agentaus_response_to_anthropic(data, model)`**

- Builds Anthropic content blocks: if a `thinking` plan is present it becomes a
  `{"type":"thinking"}` block; text goes through `normalise_for_display()`; tool calls map to
  `{"type":"tool_use","id","name","input"}` where `input` is JSON-parsed via
  `_parse_arguments` (dict passthrough, JSON parse, or `{"__raw_arguments__": ...}`).
  A message carrying only a thinking block still appends a trailing empty text block so the
  client doesn't get a turn that reasoned and said nothing.
- Maps `finish_reason`→`stop_reason` via `_STOP_REASON`: `stop`→`end_turn`, `tool_calls`/
  `function_call`→`tool_use`, `length`→`max_tokens`, `content_filter`→`end_turn`. Usage
  fields are mapped (input/output tokens) into Anthropic's shape.

**Streaming builder — `AnthropicStreamBuilder`.** Anthropic's SSE contract is strict: every
block needs start, deltas, and stop with indices matching the final message. Builder
emits `message_start` (empty content, 0 output tokens — the counter climbs as deltas
stream), then `content_block_delta` frames. Two thinking mechanisms:

- `live_thinking(text)` — streams tokens into a thinking block that **stays open** while
  more arrive; this is what makes the client's token counter move while the bridge holds a
  checked answer back. Display only — nothing replays thinking blocks.
- `thinking(text)` — one complete thinking block with a `signature_delta` carrying
  `_plan_signature` (a stable base64 digest of the text, prefixed `agentaus.`).
- `ping()` — heartbeat SSE frames.

**Token estimators.** `estimate_tokens(text)` = `calibrator.adjust(count_tokens(text))`.
`raw_token_count` = uncalibrated `count_tokens`. `estimate_request_tokens(body)` = token
count of the JSON dump of `{"s": system, "m": messages, "t": tools}`. `chunk_text` lives
here (not in `text.py`): `chunk_text(text, size)` slices text into `size`-char pieces —
"Agentaus returns whole paragraphs at once; re-chunk so the TUI paints smoothly."

**Other helpers.** `_flatten_text` (blocks→string), `_system_to_text`, `frame_skill_text`
(labels loaded-skill text as "a procedure to carry out now" — "## Procedure to follow
now") and, for image/document blocks, "[attachment omitted: the Agentaus API accepts text
input only]". `ToolCallAccumulator` reassembles streamed tool-call argument deltas by
`index` into complete `{id, name, arguments}` dicts. `trim_messages_to_fit(messages, limit)` drops oldest messages until the request fits, always keeping the tail valid (never
splitting `tool_use`/`tool_result` pairs), oldest-first as `/compact` does — the bridge is
stateless, so this is the cheap last resort before compaction.

---

## 6. Token accounting — `tokens.py`

Token counting for the context-window arithmetic. Characters/4 is wrong in both directions:
dense code and JSON tokenise far worse than prose, and the error matters when deciding
whether a request fits a hard limit. An under-count sends a request Agentaus rejects; an
over-count compacts a conversation that did not need it.

Two mechanisms: a real BPE tokeniser (`tiktoken`, `cl100k_base`) when installed (falls back
to `len(text)//4`), and **calibration against Agentaus itself**. Agentaus doesn't publish
its tokeniser, but every response reports `usage.input_tokens` for a request the bridge
counted, so the ratio is directly observable.

`TokenCalibrator` learns an **additive** model, not multiplicative:

```text
our count      Agentaus      difference
       75         2,490         +2,415
      525         2,940         +2,415
    5,025         7,440         +2,415
```

A constant ~2,415 tokens of per-request overhead with slope ≈ 1. Modelling this as a ratio
is *actively harmful*: fitted on a small request the ratio looks like 33× and applying it to
a large one inflates a 50,000-token conversation past the window and rejects a request that
would have been fine — an error that actually happened in development. Guards: slope clamped
to (0.5, 2.0), overhead to (0, 32768), sample window of 64, exponential moving average
(alpha 0.35), least-squares fit once ≥3 samples with ≥3 distinct counted values. `adjust`
adds a 256-token safety margin — under-counting gets a request rejected; over-counting only
compacts slightly early. The module never fails a request over counting.

---

## 7. The compensation brain — `augment.py` (2116 lines)

`augment.py` is where most of the "help Agentaus do its job" logic lives. It **appends to**
Claude Code's own system prompt rather than replacing it, and splits guidance into two — the
tool half is only sent when the request actually carries tools (a pure code-generation turn
spending tokens on advice it can't use dilutes the parts that matter, which costs more on a
smaller model).

**Base guidance:** `CORE_GUIDANCE` — the "Operating notes" (work out what the problem
requires; find out rather than assume; restate the requirement; handle the empty/zero/edge
cases; prefer the simplest solution; be concise; read before you edit; tool results are
real; track what you've done, never re-run; report analyses with their numbers and what
would change them). `TOOL_GUIDANCE`, `ANALYSIS_GUIDANCE` (the reporting contract),
`AGENTAUS_GUIDANCE = CORE + TOOL` for callers wanting everything.

**Tool selection advice:** `_WHEN_TO_USE` gives one-line advice keyed by tool name for
`agentaus_search`, `agentaus_zoom`, `agentaus_investigate`, `agentaus_web_search`, `Grep`,
`Bash`, `Skill`, `Agent`, `TodoWrite`, `Glob` — keyed by name so the advice can only describe
a tool that is actually on the wire. `tool_selection(body)` builds a block describing only
the tools offered, sorted to put `Skill`/`Agent`/`TodoWrite` first.

**Analysis/data detection:** `ran_an_analysis(body)` is *structural* — it looks at tool
**inputs** (a script that imports pandas, a `.csv`/`.xlsx` path) rather than results (a Read
that merely mentions numpy isn't computation). `skills_for(body)` returns `analyse-data` if
it ran an analysis or a data file is present, and `multi-step-work` if ≥5 numbered items.
Measured: the model will not make this choice itself — Skill was offered first among 28 tools
and Agentaus never called it in 5 runs (Opus did).

**Planning pass:** `should_think(body)` — plan when the turn is agentic (has tools); *not*
mid-tool-loop (the model is waiting on a result; a planner there re-plans the taken step and
measured "I am unable to access files" while holding the file), *not* for subagent/
notification turns. `plan_prompt(request, body, routing=True)` builds the planning prompt —
"Plan the turn below before you act. Private working. Nobody reads it." — with a "Tools
available" block giving each tool's name *and* the opening of its description, because a
planner shown a bare list picks the name it recognises from training — which is `Grep`.
`with_plan(system, plan)` folds the plan into the **system** prompt as "[Your plan for this
turn, which you wrote just now…]" (system, not an assistant message, to avoid two assistant
messages in a row).

**Self-review:** `worth_reviewing_turn` = not mid-tool-loop and not a subagent turn (a
reviewer can't see tool results, so it rewrites a grounded answer into a refusal — "Please
provide the DOCX file"; a subagent's answer is a return value, not a rewrite candidate).
`REVIEW_INSTRUCTION` looks *only* for real defects (wrong/non-running code, unverified
claims, unaddressed parts, contradictions — not style) and demands "VERDICT: OK" or
"VERDICT: DEFECTS" ("what is wrong" is easier for a smaller model than getting it right
first time). If the verdict isn't in the required format, `ADJUDICATE_INSTRUCTION` asks the
model "YES or NO". `REVISE_INSTRUCTION` produces the corrected answer with no preamble.

**Grounding check:** `worth_grounding_check` = mid-tool-loop or a tool was run, AND the
answer is ≥ min chars. `GROUNDING_INSTRUCTION` judges with a ledger of
`<tools_it_actually_ran>` + `<its_answer>`, flagging content claims about unread files,
comparisons to unopened material, named policies nowhere in the run, contradictory numbers;
outputs exactly "GROUNDED" or "GAPS" + one line per unsupported statement.
`STRIP_UNGROUNDED_INSTRUCTION` reissues the answer with each unsupported statement removed
or rewritten as the open question it actually is, keeping every grounded statement. The
bridge also builds `_bridge_evidence` — the bridge's *own* tool calls (searches/reads,
which never enter the Claude Code message list) rendered into the ledger so the grounding
pass doesn't rewrite a correct, evidenced answer as unread.

**Refusal/stall detection:** `could_be_a_refusal(answer, tools_offered, called_a_tool)` is a
*structural gate* (no language): not a refusal if a tool was called or none were offered;
else a three-way judge (`CLASSIFY_REFUSAL_INSTRUCTION`) decides "REFUSAL", "STALLED" or
"ANSWER". REFUSAL = claims no filesystem/access, asks the human to paste/upload/run what
tools could do; STALLED = stopped before done ("We will write the file now") with nothing
written. Biased deliberately: wrongly re-asking a good answer wastes a round trip; wrongly
accepting a refusal costs one repeatable turn. Canned corrections (`REFUSAL_CORRECTION`,
`STALLED_CORRECTION`) tell it plainly it DOES have tools and to call them, or to do the
remaining step now.

**Skill injection:** `with_library_skills(body, ...)` adds the bridge's **library** skills
to the listing (only names the bridge may answer — a same-named project skill stays the
client's; nothing added unless `Skill` is on the wire). `with_skill_descriptions` fills in
descriptions for bare project-skill entries (Claude Code lists skills by bare name; Agentaus
needs to see descriptions). `with_guidance(system, body, ...)` is the main injector — it
runs `skills_for`, locates the skill root, and appends the located skills' text **injected**
(not offered — this model doesn't call `Skill`).

**Routing section:** parses the agent list ("Available agent types for the Agent tool:")
from the system prompt and emits "If {when} → `Skill` with skill:" lines, capped
(`_MAX_ROUTES_SKILLS=28`, 5 agents), plus a parallel-Agent line, helping the planner pick
the right skill/subagent.

**Skill routing / subagent rules:** `is_subagent_turn` delegates to `sessions`; subagent
turns get `SUBAGENT_GUIDANCE` (return-value instructions) instead of the routing lines
(a subagent hands back a value, not a write-up). `PARENT_REQUEST` +
`with_parent_request` appends the session's request onto a subagent's system prompt (truncated
to 8000 chars) — workflow subagents see only the thin prompt their script wrote, and
Agentaus writes thin ones. `pending_workflows`/`WRITE_WHILE_RUNNING`/
`early_deliverable_writes`/`writes_before_workflow` stop Agentaus from writing deliverables
while a launched workflow is still running or before it starts (it once wrote `response.md`
from one file while its workflow read the rest; a draft written before the workflow becomes
its spec).

**Workflow opt-in:** `workflow_opted_in(body)` matches regexes for "ultracode", multi-agent
workflow/orchestration phrasing, `workflow-authoring` skill use, or "use a workflow"
(+ automation terms). `focus_tools` is the **tool-focus / budget pass**: it drops held-back
tools (the deny-list includes Artifact, ArtifactComments, ArtifactData, DesignSync, Monitor,
`Cron*`/`Enter*`/`Exit*Worktree`, TodoWrite, SendMessage, TaskStop, Workflow, ScheduleWakeup,
RemoteTrigger, MCP-resource tools, ...) — a deny-list, not an allow-list: the first
allow-list hid a smoke-test `get_exchange_rate` and the model answered from memory. Then a
budget trim: if the tool schemas exceed `_TOOL_BUDGET_BYTES` (40,000 bytes — about 28k
tokens; claude.ai Slack+Docs connectors alone were 85 KB, which compaction can't touch),
**unnamed, unused MCP servers** are dropped largest-first until under budget.

**Where files go:** `_where_files_go` (when both cwd and "scratchpad" appear) emits a "Where
files go" block so Agentaus writes deliverables into the working directory and never splices
scratchpad onto a working-dir path (it once produced a path that didn't exist).

---

## 8. Compaction — `compact.py`

The conversation outgrows Agentaus' context window; the bridge must compress it. Trimming
the oldest messages keeps a turn alive but throws the early conversation away — decisions,
file paths, constraints the user stated once and expects to hold. So the head of the
conversation is **replaced with a model-produced summary**: `[earlier conversation summary]`
in the **system prompt** (not a message — injecting a synthetic turn risks two user messages
in a row and detaches `tool_result` from its `tool_use`), followed by the untouched recent tail.

The hard constraint is that the bridge is **stateless**: Claude Code re-sends the whole
conversation every turn, so a naive implementation would re-summarise the entire history on
every request. The summarised region is a stable **prefix** — turn N and turn N+1 summarise
the same head — so the result is **cached by content hash** and computed once per boundary
move, not once per turn.

- `split_head_tail(messages, keep_tokens, block=20)`: tail grows backwards from the newest
  message up to `keep_tokens`, always keeps the newest, then extends/snaps to a **clean user
  turn** (never opening on a `tool_result` whose `tool_use` is in the head). The `block`
  snap (to a multiple of 20 messages) keeps the boundary still for many turns — without it
  the boundary advances ~a turn per turn and the cache never hits (measured **173 seconds
  per turn** on a real session).
- `render_for_summary(messages)`: flattens to `[role] text` lines; tool traffic included but
  labelled (`[role calls name] args`, `[tool result] ...`) because which tool ran and what it
  returned is often the substance of an engineering conversation.
- `_chunk`: sizes by **characters, never line count** (a line-counted overlap produced
  chunks six times the model's whole context window; a single message can be tens of
  thousands of chars).
- `verbatim_record(head)`: what compaction keeps **whole** from the summarised region — the
  user's first message and every `<task-notification>` (a background workflow's result),
  word for word.

`ConversationCompactor` (the class):

- **Summary cache** (bounded LRU, 64 entries) keyed by sha256 of the JSON head.
- `_find_prefix`: longest cached summary covering a prefix (last turn's summary almost always
  covers most of this turn's head → incremental path summarises only what arrived since).
- `_summarise_chunk`: **two-pass** — the summary, then a GAP pass ("what is missing" — a far
  easier question than "summarise well") that shows the source again and folds in anything
  elided; this is described as the single biggest fidelity win. Runs under the bridge-wide
  concurrency gate at "normal" priority (compaction blocks the turn, so it is not background
  work); the gap pass inside demotes itself to "background".
- Merge step: partial summaries are **merged by the model** rather than concatenated
  (consecutive chunks overlap; concat repeats itself and reads as several disjoint records).
  The result is shrunk with up to 3 merges if it still exceeds budget.
- The class keeps two **in-memory LRUs keyed by content hash**, each bounded by `_cache_size`
  (64): `_chunk_cache` (the per-chunk summary cache) and `_timeout_mem` (typed
  `OrderedDict[str, bool]`), which holds the head hashes whose summarisation blew the budget so
  an immediate retry trims directly instead of re-entering the loop that just froze. Both are
  process-lifetime only — they survive across turns in one bridge run but not a restart.
- `compact(body, limit, reserve, keep_fraction=0.5, threshold=1.0, timeout_s=None)`:
  - **Timeout / retry fast path:** when `timeout_s > 0`, the summarise run is wrapped in
    `asyncio.wait_for(run, timeout=timeout_s)`; an `asyncio.TimeoutError` writes
    `_timeout_mem[head_key] = True`, `move_to_end`s it, and evicts past `_cache_size` (LRU), and the
    turn falls back to the trim/clip path instead of hanging to `BRIDGE_TURN_TIMEOUT` (900 s). A
    finished run pops the flag. The bound exists because on a 1M-window session a cold compaction
    of a ~250k-token head takes minutes (measured 4–7 minutes of serial merging) — capping it means
    the turn still answers. The server passes `timeout_s=settings.agentaus_compact_timeout_s`, wired
    from env key `AGENTAUS_COMPACT_TIMEOUT` (default `180.0`); 0 disables. The `_timeout_mem` flag is
    in-memory (process-lifetime only), so the direct-trim fast path applies to an immediate retry in
    the same bridge run.
  - `budget = limit - reserve - overhead`; `trigger = limit * threshold` (compaction happens
    *before* the window fills — at 0.8 it fires at 80% occupancy; waiting for the hard limit
    leaves no headroom for the reply, and one big tool result tips it over into a failure).
  - Three methods: `"none"` (fits), `"summarised"` (head → cached summary + pinned verbatim
    record if it fits), `"trimmed"` (fallback — summarisation failed or still too large:
    drops whole turns at a time so the list never opens on an orphaned `tool_result`), and
    never fails the turn to do it. A summary that can't be made degrades to trimming rather
    than failing.

---

## 9. HTTP surface & turn pipeline — `server.py` (3958 lines)

The FastAPI app. Key endpoints: `GET /healthz` (liveness), `GET /healthz/upstream` (ten
minutes of upstream outcomes, adaptive limit, whether it is down and for how long, and the
hash of the code this process loaded — for the harness: a round launched into a slump or
scored on a bridge running other code says nothing about the graph), `GET /v1/models`
(Anthropic's live list — fetched with the caller's credentials, cached to a fallback, plus
Agentaus — so the model picker follows every model Anthropic adds/retires, one attempt within
2.5 s of the picker's 3 s), `GET /status.json` (the current-picture snapshot from
`status.snapshot()`), `GET /status` (the HTML incidents page), `POST /v1/messages` (the main
entry), `POST /v1/messages/count_tokens` (feeds Claude Code's context meter; Agentaus exposes no
tokeniser so this is the calibrated estimate, never billing).

**Routing (`_route`):** per-request by model id — plus a **subagent goes wherever its
session's main loop last went** (see §12). Only a main-loop turn (tools offered, no agent id)
records the session's model; a title/summary call must not flip it.

**Request-scoped logging (`rlog`, `_Phase`):** every line for one turn carries the same
short request id (a long request is indistinguishable from the concurrent summarisation calls
it spawns — which is what made a 90-second stall unreadable: sixty identical "200 OK" lines).
`_Phase` logs start *and* end; a phase that never finishes only shows as a start with no
matching end — the signature of a hang.

**Retries (`_post_with_retry`):** retries transient faults — status codes 402/408/425/429/5xx/
520-527 (Cloudflare's own; Agentaus sits behind it and 524 "origin timed out" is common on a
long summarisation) and transport exceptions (ConnectError/Timeouts/RemoteProtocolError).
Exponential backoff with jitter (so concurrent failing requests don't retry in lockstep and
re-hammer a struggling upstream), capped so a long outage is retried a few times an hour.
`retry_capacity=False` surfaces a gateway timeout immediately instead of replaying — callers
that can make the request smaller want the failure now, not four timeouts later. `_watching_for_stall` logs at intervals that an upstream call is still pending (a stall is otherwise
indistinguishable from silence — a request was once observed received, never forwarded, and
the only evidence was that the log had stopped). `give_up` lets a graph agent past its own
budget say "don't replay". A **429 that names its own wait** is honoured: `_retry_after_of`
(server.py:576-588) parses a numeric `Retry-After` header into seconds and `_sleep_before_retry`
waits at least that long (capped at one turn timeout), because the 429 is the upstream's own
word on when the rate limit lifts. A 429 wait never counts against the turn's timeout — only a
52x/504/ReadTimeout replay carries the `_FAILED_ATTEMPT_S` = 125 s penalty.

**Streaming-to-buffered (`_post_streamed`):** streams one Agentaus call and assembles it into
the response a buffered call returns. Buffered calls drew Cloudflare 524 ~76× in 109;
streamed 7 in 38 — so even buffered clients get the upstream streamed and re-assembled.
A streamed *helper* call that 429s does **not** fall back to buffered: re-sending the same
payload would double the hit on an upstream that just said to slow down, so `_helper_call`
re-raises instead (the one carve-out from the fallback).

**Turn deadline (`flight.Flight`) is further upstream §11; stall-reask:** the bridge
re-issues a stalled turn up to a per-request budget — but the budget is per **request**, and
Claude Code sends a fresh request after every tool call, so a per-request judge kept a
finished turn stalled until the client's cap (76 re-asks in one run, writing notes into the
user's Claude memory folder). `_may_reask_stalled` keys on `sha256(original_request \x00 working_directory)` with a lifetime budget of 2, refilled once 3 tool calls happen since the
last re-ask.

**Tool-call validation (`_validate_tool_calls`, `repair.repair`):** the bridge fixes
unambiguous slips *for free* — a misnamed field, a relative path, "read"→"Read" (`_canonical`
is a case/spacing-insensitive squash; observed one of three rounds burned correcting `read`
to `Read`) — and validates arguments against the client-published schema; unresolved invented
tool names get a correction ("There is no tool named X. You invented it…") rather than a
dead `tool_use` that makes Claude Code fail the turn. A schema the subset can't read is
forwarded unchecked (raising killed whole turns). `repair` tracks a `pending` dict so an
Edit is matched against prior Edits' text.

**The agent-graph substitution** is orchestrated here (see §13): when Agentaus calls
`Workflow`, the bridge swaps in its own graph script, guards against a second Workflow call
in a session whose graph already ran, strips a giant graph script down to a stand-in before
it blows the window (one such call held 115,148 chars of script/skills/inventory that alone
pushed the next request into compaction), and routes the graph's background result back via
`<task-notification>`.

**The per-turn order of operations** (the pipeline the compensation modules feed):
`focus_tools` / `with_library_skills` / `with_skill_descriptions` / `with_guidance` (guidance
envelope) → `with_plan` (when `should_think`) → compaction application → upstream call (via
flight, under throttle) → `tool_call` partition/validation/repair → `_run_bridge_tools`
(bridge-owned, deduped by call signature) → graph substitution/rerun guards → answer-side
passes: `_fix_syntax` (`syntax.first_error` → one helper call to fix non-parsing code) →
`_check_grounding` (tool turns) vs `_self_review` (prose/subagent-sat-out) →
`_answer_without_tools` when the model searched its budget away or returned nothing. This is all
gated so a pass **never raises past returning the original answer** — a rewrite pass shown only
part of an answer must not replace all of it (`_REWRITE_CHARS = 12000`, `_too_long_to_rewrite`).

---

## 10. The bridge's own tools — `tools.py` (1585 lines)

Claude Code owns every other tool. The bridge **appends its own** tools to the list Agentaus
sees, and when Agentaus calls one the bridge runs it and feeds the result straight back —
the `tool_use` **never reaches Claude Code**. The five are `agentaus_search`,
`agentaus_web_search`, `agentaus_investigate`, `agentaus_zoom`, `agentaus_inventory`. Why,
when Claude Code offers `Grep` already: Grep is a regex, and a smaller model writes a regex
against a *guess* about the code, gets a plausible match, and answers from it. `agentaus_search` reads by meaning instead.

### `agentaus_search`

1. **Enumerate files** bounded by skip-lists: `_SKIP_DIRS` (`.git`, `node_modules`, `.venv`,
   `__pycache__`, `dist`, `build`, `target`, ...), `_SKIP_SUFFIXES` (binary/generated — office and PDF
   are *not* in it when the readers are installed, so search can see them), `_NEVER_READ`
   and `_NEVER_READ_SUFFIXES`/`_NEVER_READ_STEMS` (`.env`, key files, `id_rsa`, credentials —
   files the bridge must never read however a search matched them; `test_secrets.py`
   enforces this), plus a size limit and a file cap (with a "[Stopped listing at N files…
   NOT everything. Narrow `path` or pass a `glob`.]" note — *never silently*).
2. **Query expansion** (`expand_query`): a model call asks for 5–10 literal strings most
   likely to appear in source — function/variable names, class names, config keys, error
   text — plus the query's own words; the bridge uploads those literal strings to a
   **shortlist** of files by plain substring scan (not ripgrep subprocess — `rg` is a shell
   function of the Claude Code extension, not on a clean PATH under launchd). Terms in
   >50% of files are treated as noise (`_TERM_UBIQUITY_CEILING`), measured over *whatever is
   being ranked* (a 434 KB doc read 29/29 chunks makes cross-file selectivity meaningless).
3. **Aiming**: an `agentaus_zoom` on the outline first if `agentaus_search_outline_first` —
   a free structural map (headings/sections) so it can aim before it reads (each chunk costs
   a full model call). "Aiming is an optimisation; missing the answer is not an acceptable
   price" — it falls back to reading every chunk on any surprise. A thin shortlist is a weak
   signal, not a wrong one: with too few candidates it puts them first and follows with the
   rest (the SC-NFR-11 case: a unique id in 1 of 485 files matched 1, got replaced by all
   485 — 566 chunks).
4. **Chunking + parallel look**: `effective_chunk_tokens` is the largest helper prompt the
   upstream has *proved* it can answer, **learned at runtime** — halve on a capacity failure
   (524/522/504/timeout), recover slowly on success, never below a floor, because how long a
   prompt takes depends on how busy the origin is (48k tokens answered in 23 s alone, then
   524s under two concurrent searches). Chunks are asked about in **parallel**
   (`asyncio.gather`, one summariser call per chunk under the gate), results merged by the
   model, identifiers normalised (`normalise_identifiers`), and the sections that answered
   are returned with file paths and line ranges as **citations**, each cluster capped and
   the whole thing optionally fuse-merged if over budget.

### `agentaus_zoom`

Opens a cited section verbatim so the model can quote it properly. **Never calls a model** —
it used to condense over-budget passages, and that was wrong: truncating verbatim is
strictly better and instant. It widens to the containing section (markdown headings,
numbered clauses), fits the token budget *outward from the citation* (line-count ceilings
are wrong when line lengths vary 10×; cited lines are never trimmed), and returns
`passage`-framed text with **line numbers intact**, a completion flag, and a pointer to call
`agentaus_zoom` with the next line for the rest. A search result is a citation, not the
whole passage.

### `agentaus_investigate`

Asks three **deliberately different angles** (definition / usage / consequence — "Where is
this implemented? Where is this called from elsewhere? What behaviour depends on this and
would break if it changed?") because redundancy catches a flaky answer, but only diversity
catches a failure mode a single framing cannot see. It then runs a `corroborate` pass that
separates facts into "Established" (in ≥2 reports, with file/line) and "Single-source"
(may still be true but not corroborated). Three searches share the one global cap, not six.

### `agentaus_inventory`

"What is actually in a folder, answered by looking rather than searching" — a pure directory
walk (no model calls, under a second on hundreds of files) returning counts, groupings, real
filenames, and one headline per file. Used FIRST for whole-tree questions, which semantic
search can't answer (each chunk says NONE — two such searches took 183 s and 52 s returning
116 and 130 chars). Above 120 files the per-file list is dropped for a summary **with a
note saying so** — "a summary that looks like a listing is how a partial survey gets
reported as a complete one".

### `agentaus_web_search`

The only web search available on this model — Claude Code's own WebSearch is an Anthropic
server-side tool not reachable from Agentaus. Prompt asks for answers from search not
memory, every fact with a source URL, saying plainly if nothing useful.

**Timeboxing:** a bridge tool runs under `AGENTAUS_TOOL_TIMEOUT_SECONDS`: `asyncio.wait_for`
stops the coroutine *and* its awaiting model calls, while a thread-level `ToolDeadline`
context var stops a worker thread cancellation can't. An unknown tool name gets "[bridge]
Unknown tool …", never a raised exception — a broken tool must not end the turn.

---

## 11. Concurrency & upstream health — `gate.py`, `throttle.py`

**`gate.py` — one process-wide limit on bridge-initiated Agentaus calls.** Summarising the
head, gap-checking, reviewing, expanding a query, reading each search chunk — left unbounded
these fan out to hundreds of concurrent requests. The cap is **global**, deliberately, not
per-component: two components each capped at 6 permit 12, and how hard the bridge hits
Agentaus then becomes a property of which features are enabled. It must **not** guard the
main turn's own upstream call — that call is the user's actual request; queueing it behind
background work turns a busy gate into a visible stall. The gate is **prioritised, not FIFO**
(urgent < normal < background): a search chunk is inside a tool the model is blocked on —
the user is watching a cursor; a quality pass nobody asked for should not delay it.

**`throttle.py` — one *adaptive* limit on every call, whoever makes it**, beneath every
caller (a main turn, a graph agent, a summariser chunk all queue for the same slots). It
exists because an ultracode run is many Claude Code sessions at once (a main loop and up to
a dozen graph agents), each "the user's turn" from the bridge's view — so nothing bounded
them together. Measured: past ~8 concurrent sessions Agentaus's Cloudflare front returns 524
after 125 s, and a run kept sending at full width got nothing but 524s for two hours —
1,200 failed calls with no success. The limit adapts like TCP: starts at the ceiling
(concurrency, default 10); a 5xx/429/transport failure **halves** it (never below the
floor, default 2); successes at the current level (as many as the level, and ≥ ramp seconds
since last change) raise it by one. A failure that says something about *one request* rather
than the upstream does not narrow it (a `time_up` graph turn, or a retry of a failed
request — measured: time-up turns got 524 20–50% of the time vs ~0.1% for ordinary turns;
the collapse fed on the bridge's own doomed requests). A streamed response holds its slot
until the stream closes (that's how long the upstream is busy). `LimitedTransport` is an
httpx transport that takes the slot and records each outcome in `Health`. `Health` keeps
the last ten minutes of outcomes and the seconds the upstream has been **down** (its own
clock — a run's tight turn deadline must not count outage time against the work it could do).
`StreamIdle` is a custom ReadTimeout when an upstream stream sends nothing for N seconds.

**`settings`-driven:** the gate is created lazily inside a running loop (on Python 3.9 a
`Semaphore` binds to the loop at construction; one built at import would bind to whichever
loop happened to be current — this venv is 3.14).

**Throttle ↔ status coupling.** `LimitedTransport` is the single pass-through point for
every Agentaus call; `settle()` (throttle.py:389) calls `_note_status(outcome, status_code, kind)` once per settled, non-`cancelled` outcome — recording to `Health` and handing to
`_note_status`. The rationale from the code: the transport is the one place every Agentaus
call passes, and it is where rate limits, connection failures, quota and recovery are
noticed. Outcome → incident-kind mapping, verbatim from `_note_status` (throttle.py:50–80):

- HTTP 429 → `status.record("rate_limited", "HTTP 429 - the tenant is rate-limited, retrying with backoff")` (soft)
- HTTP 402 → `status.record("quota", "top up the Agentaus quota before turns succeed")` (hard)
- HTTP 401/403 → `status.record("key_rejected", f"HTTP {status_code} - check AGENTAUS_API_KEY")` (hard)
- outcome `ok` → `status.resolve(...)` clears all five of `rate_limited`, `no_connection`,
  `slow_upstream`, `quota`, `key_rejected`
- outcome `fail` with no status code → `status.record("no_connection", "network or DNS failure reaching agentaus.com.au")` (soft)
- outcome `fail` with 504/520/522/524 → `status.record("slow_upstream", f"HTTP {status_code} origin/gateway timeout")` (soft)
- outcome `fail` with any other code → `status.record("slow_upstream", f"HTTP {status_code}")` (soft)

These are exactly the incident kinds `status.py` records (429 rate-limit storms, quota
exhaustion, rejected keys). `status.record`'s docstring (status.py:109–111): "Report an
incident. Call whenever a producer sees one: throttle failures, 429s, connection errors,
compaction timeouts. Repeated reports of a live kind bump its occurrence counter rather
than starting a new incident." Implication: a 429 storm is one incident with a climbing
counter, not one per failed call.

Documented gap: `_note_status`'s docstring (throttle.py:51–56) opens saying a `kind != "normal"` failure "is left to the log", but the body has no `kind` guard — it branches only
on `status_code`/`outcome`. So a `time_up`/`retry` request that gets a 429 still records
`rate_limited`; a 524 still records `slow_upstream`. The docstring/body mismatch is
documented so a reader does not rely on a suppression that does not exist.

Severity meaning: `rate_limited`, `no_connection`, `slow_upstream` are soft; `quota` and
`key_rejected` are hard (alert immediately); order table `_ORDER={"hard":0,"soft":1}`
(status.py:37–49). This tracker is the single source feeding `server.py`'s `/status.json`
and `/status` and the notify layer — "Single source of truth for 'what is wrong right now'".

---

## 12. Turn lifecycle & subagent routing — `flight.py`, `sessions.py`

**`flight.py` — one upstream turn per logical request.** Measured: a turn held past 600 s was
cut by Claude Code, which at once re-sent the same body (136,301 bytes streamed, 136,302
not — only `stream` differed), then re-sent that copy nine more times 300 s apart. The
bridge never noticed the buffered client had gone, so every copy queued for a slot and made
up to five upstream attempts — 96 of d14's 100 buffered outcomes came *after their client
had left*, and a stopped round's bridge was still calling Agentaus an hour later with nobody
listening. So a turn runs as a **flight**, detached from its client:

- an identical request (body without `stream`/`max_tokens`, plus the agent id — two
  subagents given the same prompt must each get their own sample) **joins the flight already
  running** and gets the same answer;
- a client that leaves does not stop the turn at once — it keeps running `flight_grace`
  seconds for the retry to join, cancelled then if none has;
- a turn that finished after its client left is kept for a late re-send; a failed turn never
  is, and neither is one every client saw to the end;
- each turn has a `BRIDGE_TURN_TIMEOUT` deadline, counted **only while the upstream can
  serve** (minus outage time and failed-attempt waits — retrying through a slump is waiting,
  not wedged). `flight.assemble` turns a stream's SSE events back into one buffered Message,
  dropping thinking blocks (display) and surfacing error events.

**`sessions.py` — which model each session's main loop is on, so its subagents can follow
it.** A subagent started from an Agentaus session did *not* run on Agentaus — Claude Code
cannot "inherit" a custom model id for a subagent, so it asked for a default Claude model
instead (measured on 2.1.281: main loop on `agentaus`, its Explore subagent on
`claude-opus-5-5`, forwarded to Anthropic — the work and the user's Claude quota went to the
model they'd switched away from). The client marks a subagent: an `x-claude-code-agent-id`
header and `cc_is_subagent=true` in its system prompt; every request carries
`x-claude-code-session-id` shared by a session and its subagents. The bridge remembers which
model each session's main loop used and sends that session's subagents the same way; switch
to Opus and they go to Anthropic again. Tracks each session's live request too, so
workflow subagents (which see only the thin prompt their script wrote) can see what the
session is actually doing.

---

## 13. The agent graph — `graph.py`, `graphs/`, `workflow_script.py`

When Agentaus calls `Workflow`, the bridge **substitutes its own graph script** for the
script the model wrote, before Claude Code sees it. Rationale: Agentaus's self-written
workflows were its weakest thing — one-line scripts, unbalanced brackets, `Bash()` called
inside scripts, subagent prompts with no rules, verifiers told to "repeat the calculations
as before", nothing passed between agents. The graph is built on four principles: **many
agents; one small simple prompt per agent; many steps; redundancy** — a backup for every
fallible step, every answer reached 3 independent ways.

- `MARK = "[agentaus-graph]"` is put at the start of every graph/subagent prompt and used
  throughout to detect graph-originated traffic (guarded writes, `time_up`, `overdue`,
  `exact_command`, import-guarding, plain-text writes).
- `graphs/solve.js` and `graphs/dynamic.js` are the two graph scripts; a `skills/` subfolder
  holds 8 markdown skills pre-read once (so agents don't read their own instructions off
  disk). `script_for()` returns the dynamic script if `AGENTAUS_GRAPH_KIND == "dynamic"`
  else the fixed one.
- `graph.expand(call, task, cwd, started_at)` replaces the script with the bridge's graph,
  computes `total_ms` (a hard end derived from the turn budget, floored at 60 s — the harness
  kills runs at 7200 s and graphs start 10–95 s into a session), and keys behaviour off the
  `[agentaus-graph]` mark. `graph.guarded_writes`, `focus`, `trim_results`, `overdue`,
  `time_up`, `plain_text_writes`, `exact_command`, `note_run`/`remember_run` for session
  state. `_without_graph_script` strips a graph script down to a `{description, script: agentaus-solve}` stand-in before it blows the window.
- `workflow_script.py` validates/separates the workflow scripts Agentaus writes;
  `separate_statements` is called from `repair.repair` for Workflow scripts (repair runs
  *before* `graph.expand` replaces the script entirely). `import_guard.py` snapshots the
  `.py` files an agent is about to change and can check they still import afterward — the
  guard that stopped Agentaus leaving broken Python in a file it edited.

---

## 14. Document reading — `documents.py`, `pdf.py`

**`documents.py` — Word/spreadsheets/PPT via LibreOffice, tables intact.** A `.docx` is a
zip archive — read as text it's binary noise, so the bridge used to skip office documents —
meaning `agentaus_search`/`zoom` couldn't see a tender response, specification or
requirements matrix, exactly the material people keep in them. A regex flatten is not good
enough: measured against LibreOffice on a real 43-row requirements table it lost 2,695
characters of answer text across 12 rows — 56% of one row — and couldn't see the compliance
column at all (it had no idea where one cell ended and the next began). LibreOffice converts
to HTML (keeping `<table>/<tr>/<td>`), which is rendered back to text with **cell
boundaries visible** — a table row becomes one line with ` | ` between cells, so line
numbers stay meaningful and `agentaus_zoom` can cite into a spreadsheet like source code.
Converted text is cached keyed by path+mtime+size (bounded LRU), each conversion run in a
private LibreOffice profile (concurrent soffice invocations fight over the shared profile
and one silently produces nothing). A PDF goes to `pdf.py` — LibreOffice's PDF importer
treats it as a drawing (one conversion produced 112 characters of CSS and 100 GIFs).
`repair_tool_results` also **re-reads office-document results that arrived as binary** from
Claude Code's own `Read` (which returns zip noise for a `.docx`), substituting real text.

**`pdf.py` — the PDF ladder.** A PDF may carry a clean/broken/absent text layer and nothing
announces which, so it runs an **ordered ladder** of extractors and escalates only failed
pages. Order was measured: across 48 PDFs, `pdftotext -layout` returned more text than
pdfminer/pypdf on *every* file (often 2–3×) and was 10–30× faster; `-layout` preserves
column/table geometry. Text tiers: pdftotext (renders via poppler), pdfminer, pypdf (per
page). A page is "readable" by a length and character-ordinance gate; unreadable pages
(thin → likely a scan) go to OCR — macOS Vision (Quartz rasterisation + OS neural OCR, no
dependency) or tesseract+pdftoppm. Every tier is optional; **nothing raises** — an
unreadable PDF looks like an empty document. `documents.install_hint()` names the one way to
read these files ("There is one way to read these files here" — a second pure-Python
implementation of "how does a table become text" has different format coverage and a
different rendering; two answers to that question is a bug nobody finds until it matters).

---

## 15. Text normalisation — `text.py`, `symbols.py`

**`text.py`** repairs "the typography a model applies to things that are not prose". Agentaus
rewrites ASCII hyphens as U+2011 (non-breaking hyphen) and wraps identifiers in `$$...$$`;
left alone, a file path in an answer can't be copied/clicked/searched, and a summary fed
back into the conversation carries a path that no longer resolves. Two functions:

- `normalise_identifiers` — the **aggressive** treatment for text a model will re-read
  (search results, summaries): NFKC-normalise, map the full set of dash/space/quote
  lookalikes to ASCII (`$$id$$` → `` `id` ``).
- `normalise_for_display` — the **narrow** treatment for text a person reads: only the
  characters that are *never* a deliberate choice and *always* break what they appear in
  (U+2010/2011 → hyphen, NBSP → space); en/em dashes and smart quotes are ordinary
  typography in prose and deliberately left alone — but inside backticks *everything is an
  identifier*, so code spans get the full treatment. This is single point every buffered
  answer passes through on its way to the client.

**`symbols.py`** drives `outline.py`'s tree-sitter integration (`symbols.outline_of`,
`symbols.language_for`) — the exact declaration index used to make search aim. `text.py`
does not itself hold `chunk_text` (that's in `translate.py`) or the token estimators
(`tokens.py`); the repo's `tests/test_identifiers.py` enforces that identifiers survive
summarisation byte-for-byte.

---

## 16. Further modules (concise)

- **`outline.py`** — a free structural map (declarations/functions/classes/sections with line
  numbers and per-section token sizes) so search can aim before it reads, via a fallback
  chain of extractors in order of exactness: tree-sitter symbol indexing (19 languages,
  distinguishes a declaration from the same words in a docstring), Python's `ast`, a
  declaration-line regex, and a markdown-heading/JSON-key fallback. Each rung runs only when
  the one above cannot.
- **`inventory.py`** — the listing `agentaus_inventory` builds (kinds counter, folders,
  per-file headlines from the file's own structure — no model calls).
- **`repair.py`** — repairs unambiguous tool-call slips free (misnamed fields, relative
  paths, casing), with a `pending` ledger so an Edit is matched against prior Edits' text,
  and `separate_statements` for Workflow scripts.
- **`schema.py`** — validates tool arguments against the client-published tool schema
  (arguments it can't interpret are forwarded unchecked rather than raising). `test_schema.py`
  (18 tests) covers it.
- **`syntax.py`** — `syntax.first_error(answer)`; finds the first failure in code an answer
  contains, hands it a `FIX_INSTRUCTION` helper call, and only takes the fix if it then
  parses ("does the code in the answer parse?" — `test_syntax.py`).
- **`distill.py` (ResultDistiller)** — distills genuinely enormous tool output before it
  enters context.
- **`ledger.py`** — **a per-turn record of what the model already did, *derived, never
  stored*** (its own docstring: "a pure function of the request, computed fresh every turn,
  which keeps the bridge stateless" — there is no on-disk log). `with_ledger` appends a
  derived list of tools already run / tasks outstanding to the prompt so the model stops
  re-running what it just did; `render_evidence` / `render_notifications` (consumed at
  server.py:1140-1141) digest the turn's tool calls and `<task-notification>` reports. Gated
  by `AGENTAUS_TOOL_LEDGER` (default on) and limited by `AGENTAUS_TOOL_LEDGER_LIMIT` (40).
  The append-only ledger keyed to the bridge's loaded-source hash belongs to
  `abbench/ultra/loop.py`'s run record, not this module.
- **`prompt_style.py`** — the single source of how the bridge lays out what it writes for
  Agentaus (Markdown default; `AGENTAUS_PROMPT_STYLE=xml` restores the tagged layout).
  Templates are written once in tagged form and rendered at call time, so the switch cannot
  drift into two copies.
- **`skills.py`** — loads the project's own skills (and serves the bridge's `skill_library/`),
  because this model will not load them itself; `skills.serve` + `frame_skill_text` answer
  `Skill` calls the client never heard of (see §17).
- **`persisted.py`** — **recovers tool output Claude Code truncated to a file.** When a tool
  produces more than the client will carry, Claude Code writes the whole thing to disk and
  passes a 2 KB preview ("Output too large (1.6MB). Full output saved to: …"). That's
  sensible for a client and terrible for a model — observed: asked to survey a repo, the
  model ran one `find`, was handed 2 KB of a 1.6 MB listing, and wrote confident comparisons
  between files it had never seen, including a policy that doesn't exist — it was told where
  the full output was and didn't read it. The bridge runs on the same machine, so it can: the
  preview is replaced with the real content, distilled if genuinely enormous, newest result
  first, capped, and it never leaves a preview replaced by nothing.
- **`harmony.py`** — detects when the model wrote tool calls into its **prose** as
  angle-bracket markup (`harmony.looks_like_harmony`/`harmony.extract`) and pulls them out
  into real tool calls — the prose form is still a call the model meant to make; showing
  angle brackets as an answer is the alternative.
- **`background.py`** — headless service entry point used by Windows Task Scheduler: re-points
  stdout/stderr at the log file (pythonw.exe has no console, so output would otherwise vanish)
  and runs the server; launchd/systemd capture output themselves and do not use it.
- **`components.py`** — the installable-pieces registry: records which components this install
  chose (`AGENTAUS_COMPONENTS` in `.env`) so the bridge and `diagnose.py` can tell a reader
  deliberately skipped from one that should be there and is missing.
- **`attribution.py`** — **commit co-author trailer rewriting** (not claim attribution).
  Claude Code tells the model how to sign a commit via a system-reminder; for agentaus the
  line is `Co-Authored-By: Claude Code <noreply@anthropic.com>`, so every Agentaus commit
  credited Claude. The bridge swaps that trailer for Agentaus' own on requests it sends
  Agentaus (only Claude Code's own words: reminders, the system prompt, and only a trailer
  standing alone on a line); a tool result or user-typed text is history and left as-is; a
  project setting `attribution.commit` overrides for every model.
- **`identity.py`** — **injects the "You are Agentaus" identity line** into every Agentaus
  system prompt (`with_identity` prepends `IDENTITY` — "You are Agentaus, made by Trellis
  Data" — first). Agentaus otherwise defaults to Claude-style self-description when asked who
  it is; injecting a short line costs ~60 tokens versus the ~3,500-token rewrite the model
  would otherwise produce, and `tests/test_identity.py` enforces it.
- **`status.py`** — **single source of truth for "what is wrong right now"** (its own
  docstring role, for Agentaus and the Anthropic passthrough on this branch). Every producer of a struggling upstream — retries and adaptive limits — reports
  one line here; the `compacting`/`compaction_fallback` incident kinds are defined
  (status.py:44-45) but no producer currently records them on this branch; every consumer
  (the `/status` page, the Windows toast, recent history) reads the assembled picture. The
  `Incident` dataclass carries `.kind`,
  `.detail`, `.started`, `.occurrences`; `.severity` comes from the `SEVERITY` table — soft
  `rate_limited`, `no_connection`, `slow_upstream`, `compacting`; hard `quota`,
  `key_rejected`, `turn_timeout`, `compaction_fallback`, `settings_drift` — and
  `.describe()` returns the kind's stored human phrase plus the occurrence detail. One live
  incident per kind: `record()` bumps an existing kind's `occurrences` counter rather than
  starting a fresh incident (a 429 storm is one climbing counter, not one per call), logs a
  warning on occurrences 1/5/20/50/×100, and notifies subscribers under the lock, so a
  consumer must never block. Functions: `subscribe(callback)` — called `(incident, is_resolve)` on every transition; `record(kind, detail)`; `resolve(kind)` — clears an
  incident (a retry succeeded / the upstream is back); `active()` — live incidents sorted
  worst-first by `_ORDER={"hard":0,"soft":1}` then start time; `describe()` — current-picture
  summary text; `recent(limit=50)` — history oldest-newest deque, cap 200;
  `reset()`; `snapshot()` — the `/status.json` payload. The drift trio:
  `set_env_file(path)` / `snapshot_settings()` / `check_settings_drift()` watch for settings
  changed on disk after the bridge started (`settings_drift`, hard). Throttle feeds it via
  `_note_status` (see §11): 429→`rate_limited`, 402→`quota`, 401/403→`key_rejected`, success
  resolves those, transport failures→`no_connection`/`slow_upstream`.
- **`notify.py`** — **Windows desktop toasts, "the consumer of agentaus_bridge.status"**.
  `install()` registers with status ("Register with status. No-op unless notifications are
  enabled", verbatim) and is a no-op unless `AGENTAUS_NOTIFY=toast` (default `"off"`).
  `_on_incident` runs under status's lock so it must not block — it hands off to a daemon
  thread. Hard kinds (per the table above) toast immediately on start; soft kinds toast only
  once they have lasted `AGENTAUS_NOTIFY_AFTER_S` (`_record_start`/`_mature`), and every
  toast is subject to a per-kind cooldown `AGENTAUS_NOTIFY_COOLDOWN_S`
  (`_cooldown_ok`/`_mark_toasted`). A recovery toasts `<kind>:recovered` only if that kind
  was actually toasted, with its own cooldown key so a quick down/up cannot spam. `_fire`
  launches PowerShell `pwsh` WinRT toast via `subprocess.Popen(..., creationflags=CREATE_NO_WINDOW, stdout/stderr=DEVNULL)` in a daemon thread — never the event loop — and
  a failure is logged and swallowed. Inherently Windows-only (relies on the `pwsh`
  WinRT toast + `CREATE_NO_WINDOW`); macOS/Linux get only the always-on heartbeat and
  `/status` page.

---

## 17. Skills — `.claude/skills/` (project skills) and `skill_library/` (bridge library)

**Project skills (`.claude/skills/`, 10):** `analyse-data`, `bridge-diagnose`, `find-in-code`,
`investigate`, `multi-step-work`, `read-documents`, `repo-survey`, `research`,
`search-exhaustively`, `tender-evidence`. These match the model's own bundled skills; the
bridge's skill-injection tests depend on them.

**Bridge library (`skill_library/`):** 9 bundled skills adapted from Anthropic's plugins
(with LICENSE and NOTICE), served by `skills.py`; the bridge answers `Skill` calls that name
them directly (the client never heard of them), via `skills.serve` + `frame_skill_text`. Only
bridge library names are served; a same-named project skill stays the client's.

---

## 18. Tests — `tests/`

**48 `.py` test files, 1070 test functions** (measured by AST parse), all offline unit tests
(stubbed fakes, mocked `agent()`), except a set marked network. Nine highest-density files:
`test_ultracode` (143), `test_thinking` (59), `test_tools` (52), `test_installer` (52),
`test_benchmark_tasks` (43), `test_bridge_robust` (40), `test_loop` (38), `test_augment` (37),
`test_compact` (36) — with `test_context_economy` (36) tying at ninth (the raw `grep -c "def test_"`
count is 1077: 7 matches are string literals inside data values rather than functions, while the
10 `async def test_` hits are real methods inside classes — so 1077 − 7 = 1070 genuine test
functions). The
two new files from this branch, `test_status` (31) and `test_notify` (14), do not crack the top
nine — they are gathered under "Status/notify (this branch)" in the by-area list below. See §8
for the compaction timeout (`AGENTAUS_COMPACT_TIMEOUT`) these exercise alongside `test_compact`.

By area:

- **Translation:** `test_translate` (25), `test_models` (5, network-light), `test_passthrough`
  (2, network), `test_harmony` (14), `test_thinking` (59), `test_schema` (18), `test_markdown_prompts` (23), `xml_style.py` (a helper pinning a test module to the XML prompt layout).
- **Compaction/context:** `test_compact` (36), `test_augment` (37), `test_context_economy` (36),
  `test_context_limit` (13), `test_auto_trim` (7), `test_identifiers` (9, identifiers must survive
  summarisation byte-for-byte), `test_tokens` (12), `test_live_tokens` (8, network).
- **Concurrency/stall:** `test_throttle` (16), `test_responsiveness` (14), `test_stall` (6),
  `test_attribution` (13, commits credit Agentaus not Claude).
- **Search/grounding:** `test_tools` (52), `test_grounding` (19), `test_dedup` (18, two phrasings
  of one question must not run search twice), `test_outline` (28), `test_inventory` (13),
  `test_secrets` (16, files the bridge must never read), `test_subagent_routing` (12, a subagent
  runs on its session's model, not Claude).
- **Documents:** `test_documents` (19), `test_pdf` (19), `test_ingestion_is_clean` (6),
  `test_requirements` (11, every reader required).
- **Status/notify (this branch):** `test_status` (31), `test_notify` (14) — the status
  incident tracker and the Windows toast consumer; `test_compact` also grew to cover the
  compaction-timeout fast path.
- **Skills:** `test_skills` (20), `test_skill_listing` (18), `test_skill_library` (7).
- **Robustness:** `test_retry` (24), `test_bridge_robust` (40), `test_crash_fixes` (8, from a day
  of benchmark logs), `test_repair_focus` (17, through the real Claude Code binary),
  `test_logging` (10, logging must suffice to diagnose a stall), `test_benchmark_tasks` (43,
  tests the harness itself — a scorer's bugs look like model failures), `test_loop` (38),
  `test_ultracode` (143), `test_pick` (15 — the graph's change picker), `test_text` (10), `test_syntax`
  (15).
- **Live scripts:** `integration_test.py` (end-to-end vs a running bridge + real Agentaus;
  mid-session model switching, window overflow, tool round-trips, streaming; costs real
  tokens), `smoke_test.py` (plain completion, streaming sequence, tool round trip).
- **Support helpers:** `tests/conftest.py` (pytest fixtures) and `tests/stub_server.py` (a stub
  HTTP endpoint for tests that do not need a real bridge) — not test modules, not counted in
  the 48 test files.
- **Graph dry-runs:** `graph_dry_run.mjs` (schema-shaped answers from a mock agent, a null
  every 17th call to exercise backups), `graph_dynamic_dry_run.mjs` (mock answering in each
  skill's Markdown, dropping every 13th call, failing one check so backups/redo/merge run).

---

## 19. Benchmarks — `benchmarks/` and `abbench/`

**`benchmarks/` (thin harness):** one client for every benchmark so results compare across
models; captures per-call telemetry (tokens, wall clock) because resolve rate alone hides
cost. Suites: **humaneval** (HumanEval pass@1 run locally, 164 problems, subprocess with
wall-clock timeout, not a sandbox — best measured **pass@1 = 0.9817** / 161/164); the
three failures were HumanEval/100 SyntaxError, /116 and /145 AssertionError (1004 s, 141k in
/ 84.7k out tokens, median 5.26 s/call). **retrieval** (localisation precision at what
cost; ground truth is this repo; questions worded to share no vocabulary with answers so
grep can't serve but semantic must — f1 0.5417). **injection** (indirect prompt-injection
planted in file content — an ordinary question must be answered without obeying it —
`resist_rate` 1.0). **documents** (answer from a PDF; needles verified to live ONLY inside
PDFs against all 263 non-PDF corpus files — motivated by `.pdf` being in the search
skip-list so all 48 PDFs were invisible). **coverage** (completeness vs first-match: how
much of the corpus an answer demonstrably touched).

**`abbench/` (the heavy A/B harness):** **Claude Code driving Opus 5 vs Agentaus** through
the real VS Code-shipped Claude CLI binary in fresh dirs outside git, graded with no model
in the loop, and no network lookups (web tools off, transcripts scanned). Three arms: `opus`
(claude-opus-5 via baseline bridge passthrough), `agentaus_base` (baseline bridge, port
8795), `agentaus_tuned` (this worktree, port 8796). Suites: `he` (HumanEval+ 164 problems,
dev/test split on even/odd ids so prompts never fitted to the scored half), `mmlu_pro`
(300 MMLU-Pro, 10/session), `mmlu` (300 MMLU, 10/session), `econ_r` (2 R-coded econ tasks:
an OLS model; a data.gov.au download + Pareto tail). `ab.py run/report`; `bridges.sh`
(re)starts A/B bridges with `start_new_session=True` so a job-kill can't take a bridge down;
per-round drivers also live in `abbench/`: `cdev.sh` (clean dev rounds), `cnext.sh` (v5
snapshot), `cref.sh` (clean Opus reference), `ctest_agentaus.sh` (final held-out test halves),
plus `stream_probe.py` (timestamps every stream event through the real Claude binary) and
`skill_use.py` (does the model reach for Skill and Agent); `ultra.py setup|run|report` runs the Ultracode multi-agent `Workflow` benchmark (econ/1913/
tender tasks, graded by comparing to originals); `loop.py` is the hardened run loop that
gates launches on bridge health + a streamed probe, aborts runs whose last 15 min are an
outage, voids and reruns spoiled windows, with an append-only ledger of every run keyed to
the bridge's loaded-source hash; `summarise.py` aggregates runs into JSON with 95% Wilson
intervals; `make_report.py` builds the HTML A/B report with "no number typed in by hand".

**A/B verdict (held-out test halves, tuned vs Opus):** HumanEval+ tuned **0.939** vs Opus
**0.939**; MMLU tuned **0.807** vs Opus **0.933** (tuned climbing from a base 0.047 — the
baseline produced no answers file in 14/15 batches); MMLU-Pro tuned **0.707** vs Opus
**0.867** (base 0.0). Econ tasks tuned **1.0**. The "tuned" bridge lifts Agentaus's
abysmal raw base test-half scores to within ~0.03–0.16 of Opus, at ~2–4× wall-clock.

---

## 20. Scripts & root files

- **`scripts/install.sh`** — macOS/Debian install of LibreOffice, poppler, tesseract,
  python deps. **`scripts/diagnose.py`** — reads the bridge log and prints throughput,
  tool-call and compaction latencies, every signal ranked by severity and count, each with
  what it means and what to do about it, **and** what *never* fired (a compensation that
  never triggered is either healthy or never been exercised — those need telling apart).
- **`start-bridge.cmd`** (Windows; `start-bridge.sh` macOS; PowerShell equivalents
  `start-bridge.ps1`, `claude-agentaus.ps1`, `install.ps1` in `scripts/`) — launcher; on Windows sets
  `PYTHONUTF8=1` (without it the repo's `read_text()` calls decode as cp1252 and the server
  crashes on import) and runs the venv `python -m agentaus_bridge` with output teed to
  `logs\agentaus-bridge.log`. **`claude-agentaus.cmd`** — health-checks the bridge, starts
  `start-bridge.cmd` if down (up to 30 tries), sets `ANTHROPIC_BASE_URL`/`ANTHROPIC_CUSTOM_MODEL_OPTION=agentaus`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1048576`, then runs
  `claude --model agentaus`. **`.1.cmd`** = the one-liner wrapper. **`agentaus_bridge/installer/`**
  (`cli.py`, `flow.py`, `actions.py`, `ui.py`) — the setup package `python -m agentaus_bridge
  setup` runs after `install.sh` fetches the repo.
- **`vs models:`** `requirements.txt` (fastapi, uvicorn, httpx, tiktoken, tree-sitter +
  language pack, pdfminer.six, pypdf, pyobjc-framework-Vision/Quartz on macOS).
- **`agentaus_api_doc.md`** — the Agentaus API reference (currently holds the API key in
  plaintext — see §21).
- **`.gitignore`** — `.venv/`, `.env`, `__pycache__/`, `.claude/settings_old.json`,
  `.claude/settings.local.json`.

---

## 21. Security notes

- The bridge binds `127.0.0.1` by default with **no client authentication**, fine on
  loopback; if `BRIDGE_HOST` is opened up, `BRIDGE_TOKEN` should be set — anything that can
  reach the port can otherwise spend the Agentaus quota.
- **The API key is currently stored in plaintext in `agentaus_api_doc.md`** as well as
  `.env` — the doc is **not** gitignored. Before this repo goes anywhere shared, the key
  should be removed and rotated.
- `BRIDGE_LOG_BODIES=true` writes full prompts (including source code) to the log — leave
  it off outside debugging.
- Everything sent to Agentaus leaves the machine for Trellis Data's Australian
  infrastructure; everything to a Claude model leaves for Anthropic — the routing table
  determines which.
- `CLAUDE.local.md` notes: `AGENTAUS_SEARCH_ROOTS` must stay **empty** on Windows (the
  bridge splits it on `:`, which breaks drive letters) and `BRIDGE_TOKEN` must stay **empty**
  (Claude Code sends the claude.ai login as the bearer token, so a bridge token would block
  the Claude passthrough).

---

## 22. Key measured numbers (the evidence behind the design)

- Streamed vs buffered 524s: 7/38 vs 76/109 (streams far better).
- Token relationship to Agentaus: additive ~2,415 tokens overhead, slope ≈ 1.
- Compaction without the stable-prefix cache: 173 s per turn (cache makes it once per
  boundary move) — still true, but now **bounded**: `AGENTAUS_COMPACT_TIMEOUT` (env key for
  `agentaus_compact_timeout_s`, default `180.0`) wraps the summarise run in
  `asyncio.wait_for(..., timeout_s)` (compact.py:674–682), so a cold compaction that would
  exceed it falls back to the trim/clip path via `_timeout_mem` (an immediate retry of a
  timed-out head trims directly) instead of hanging to `BRIDGE_TURN_TIMEOUT` (default `900.0`).
- Eight+ concurrent sessions → all-524 collapse (1,200 failed calls, two hours). These are
  surfaced by the status layer: `_note_status` in `throttle.py` feeds 429→`rate_limited`,
  402→`quota`, 401/403→`key_rejected` and resolves them on recovery, and the incidents appear
  on `/status` (via `status.describe()`/`active()`/`recent(15)`), `/status.json` (from
  `status.snapshot()`), and as Windows desktop toasts when `AGENTAUS_NOTIFY` is on. The
  in-chat streaming heartbeat is unchanged — a static `"Still waiting for Agentaus (Ns)"`
  beat that never consults status.
- `-layout` pdftotext beats pdfminer/pypdf on all 48 PDFs, 10–30× faster.
- HumanEval+ pass@1: 0.9817 (161/164). A/B: tuned ≈ Opus on HumanEval (0.939/0.939);
  MMLU 0.807 vs 0.933 (base 0.047); MMLU-Pro 0.707 vs 0.867 (base 0.0); econ 1.0/1.0.
- d14 outage evidence: 11 outage runs scored 0 and were counted; a run into outage burned
  up to 7200 s (~9.5 h total); 5 runs died on first request after 62 min; 96/100 buffered
  outcomes arrived after their client left.
- Bing: the bridge's per-request routing + calibration + compaction + grounding keep a
  long session alive that a base install cannot.

---

*This knowledgebase is assembled from reading the repo's own documentation and every source
module in the package. It is the reference the [summary](Summary.md) condenses.*
