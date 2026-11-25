# Configuration reference

The full table; the README's [Configuration reference](../../README.md#configuration-reference)
lists only the common settings. A few internal tuning knobs are documented only in
`agentaus_bridge/config.py`. All settings are environment variables, readable from `.env`. Shell exports win over
`.env` values.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTAUS_API_KEY` | *(required)* | Your Agentaus key |
| `AGENTAUS_BASE_URL` | `https://agentaus.com.au` | Agentaus host |
| `AGENTAUS_PATH` | `/api/v1/chat/completions` | Endpoint path |
| `AGENTAUS_SYSTEM_PROMPT_OVERWRITE` | `true` | Replace the Agentaus persona with Claude Code's prompt |
| `AGENTAUS_UPSTREAM_STREAM` | `true` | Request SSE from Agentaus rather than polling for a whole reply |
| `AGENTAUS_MODEL_MARKERS` | `agentaus` | Comma-separated substrings that route to Agentaus |
| `AGENTAUS_FORCE_ALL` | `false` | Ignore the model id; send everything to Agentaus |
| `AGENTAUS_MAX_INPUT_TOKENS` | `131072` | Agentaus' context window. Defaults in code and self-corrects from Agentaus' own error messages; setting it explicitly overrides both. `0` disables the check |
| `AGENTAUS_AUTO_TRIM` | `true` | Compact the older conversation into a summary rather than failing the turn |
| `AGENTAUS_MAX_CONCURRENCY` | `8` | One global cap on every Agentaus call the bridge makes on its own initiative — summarising, reviewing, planning, searching. Your own turn is never queued behind it. Replaces `AGENTAUS_SUMMARY_CONCURRENCY`, which is still read when set |
| `AGENTAUS_OFFICE_EXTRACT` | `true` | Read `.docx`, `.xlsx`, `.pptx` and friends via LibreOffice, tables intact. `false` skips them as binary |
| `AGENTAUS_SOFFICE_PATH` | *(auto)* | Path to `soffice`. Empty searches the usual locations, then `$PATH` |
| `AGENTAUS_OFFICE_TIMEOUT` | `120` | Ceiling on one conversion |
| `AGENTAUS_PDF_EXTRACT` | `true` | Read PDFs via the extractor ladder. `false` skips them as binary |
| `AGENTAUS_PDF_OCR` | `true` | OCR the pages that came back blank or garbled. macOS Vision first, then tesseract |
| `AGENTAUS_PDF_MIN_CHARS_PER_PAGE` | `80` | Below this a page counts as having no text layer. Real text pages run 500+; cover pages run tens |
| `AGENTAUS_PDF_OCR_MAX_PAGES` | `40` | Cap on OCR pages per document. Exceeding it is logged, never silent |
| `AGENTAUS_PDF_DPI` | `200` | Rasterisation resolution for OCR |
| `AGENTAUS_PDF_TIMEOUT` | `120` | Ceiling on one extraction or one OCR page |
| `AGENTAUS_SEARCH_MAX_DOCUMENT_BYTES` | `25 MiB` | Size ceiling for PDFs and office documents, separate from `AGENTAUS_SEARCH_MAX_FILE_BYTES`. Their size on disk is embedded images, not content — the 1 MiB text cap excluded 27 documents in one corpus, including a 93-page tender response |
| `AGENTAUS_INVENTORY` | `true` | Offer `agentaus_inventory`, which lists what is in a tree — counts, kinds, folders, one line per file. No model calls |
| `AGENTAUS_SEARCH` | `true` | Offer `agentaus_search`, the bridge-executed semantic search, and steer `Grep` towards literal lookups |
| `AGENTAUS_WEB_SEARCH` | `true` | Offer `agentaus_web_search`, which drives Agentaus' own web search. Claude Code's `WebSearch` is dropped in translation, so without this an Agentaus turn cannot search the web at all |
| `AGENTAUS_SEARCH_CHUNK_TOKENS` | `8000` | File content per search call. Measured: 4000 costs 48s/16 calls, 8000 costs 29s/10, 16000 costs 22s/4 — all find the same facts, but bigger chunks quote less back |
| `AGENTAUS_SEARCH_MAX_CHUNKS` | `120` | Ceiling on calls for one search. Truncation is reported in the result, never silent |
| `AGENTAUS_SEARCH_MIN_CANDIDATES` | `3` | Below this many shortlisted files, distrust the shortlist and read everything |
| `AGENTAUS_SEARCH_MAX_FILE_BYTES` | `1048576` | Skip files larger than this |
| `AGENTAUS_SEARCH_ROOTS` | *(empty)* | Colon-separated directories search may read. Empty confines reads to the request's working directory (the bridge process's cwd when none was named), plus the bridge's skill library |
| `AGENTAUS_SEARCH_MAX_FILES` | `5000` | Ceiling on files one search or inventory walk returns. Hitting it is stated in the result |
| `AGENTAUS_TOOL_TIMEOUT` | `480` | Wall-clock ceiling on one bridge tool call. The model gets a result saying it was stopped, and the worker thread stops too. `0` disables |
| `AGENTAUS_DOCUMENT_CACHE_CHARS` | `67108864` | Converted document text kept in memory, least recently used evicted first |
| `AGENTAUS_TOOL_ROUNDS` | `12` | How many rounds of bridge-executed tool calls one turn may run before the answer has to stand |
| `AGENTAUS_CORRECTION_ROUNDS` | `3` | Rounds spent telling the model a tool it named does not exist. Separate from tool rounds, so being corrected does not consume the budget for real work |
| `AGENTAUS_SEARCH_OUTLINE_FIRST` | `true` | Build a free structural outline and spend one call choosing sections, instead of a call per chunk |
| `AGENTAUS_SEARCH_MAX_SECTIONS` | `8` | Sections one aimed search reads before it is cheaper to read everything |
| `AGENTAUS_SEARCH_MAX_CANDIDATES` | `12` | Ceiling on files one search reads. The shortlist is ranked, so this keeps the best matches |
| `AGENTAUS_INVESTIGATE` | `true` (`false` under `AGENTAUS_PROMPT_DIET`) | Offer `agentaus_investigate`: three independent searches, and a fact must appear in two before it is reported as established |
| `AGENTAUS_ZOOM` | `true` | Offer `agentaus_zoom`: open a citation from a search result and read it in its section. Without it the model cites evidence it cannot quote from |
| `AGENTAUS_ZOOM_RADIUS_LINES` | `120` | How far either side of the cited lines to look for a section boundary |
| `AGENTAUS_ZOOM_MAX_LINES` | `400` | Hard stop, so a heading-less file cannot return itself whole |
| `AGENTAUS_ZOOM_MIN_LINES` | `40` | Floor on the window. Tender documents use bold single lines as sub-headings, so boundary detection alone returned 3-line "sections" |
| `AGENTAUS_ZOOM_MAX_TOKENS` | `6000` | Below this the passage comes back verbatim; above it, Agentaus keeps what serves the stated purpose |
| `BRIDGE_LOG_BODY_CHARS` | `20000` | How much of a body `BRIDGE_LOG_BODIES` logs |
| `AGENTAUS_DISTILL_RESULTS` | `false` | Condense oversized tool results before sending them. **Off by default** — see [Distillation](behaviours.md#distillation) |
| `AGENTAUS_DISTILL_THRESHOLD_TOKENS` | `12000` | Results smaller than this are never touched |
| `AGENTAUS_TOOL_LEDGER` | `true` | Append a derived list of tools already run to the system prompt. Costs no Agentaus calls |
| `AGENTAUS_GROUNDING_CHECK` | `true` | Check a tool-derived answer against the tools actually run, and strip claims nothing supports |
| `AGENTAUS_RESTORE_PERSISTED` | `true` | When the client truncates a tool result to a preview, read the file it saved instead |
| `AGENTAUS_RESTORE_MAX_BYTES` | `400000` | Ceiling on what is read back |
| `AGENTAUS_THINKING` | `true` | Plan the turn in a separate call before answering it |
| `AGENTAUS_THINKING_VISIBLE` | `true` | Show that plan as a thinking block. `false` still uses it, but does not display it |
| `AGENTAUS_PLAN_ASYNC` | `true` | Run the planner beside the turn's first call instead of before it; the plan joins every later request of the turn |
| `AGENTAUS_PLAN_WAIT_SECONDS` | `30` | How long the first request after a tool result waits for a plan still being written. Later requests never wait |
| `AGENTAUS_GROUNDING_ONE_PASS` | `true` | The grounding check returns `GROUNDED` or the corrected answer in one call, instead of a verdict call and a rewrite call |
| `AGENTAUS_OVERLAP_CHECKS` | `true` | Start the grounding check beside the stall check; it is cancelled if the turn is re-asked |
| `AGENTAUS_BATCH_TOOLS` | `true` | Offer `run_tools`, whose argument is a list of tool calls, beside the real tools. Agentaus' API returns one tool call per reply and drops the rest; this lets one reply carry several. See `agentaus_bridge/batching.py` |
| `AGENTAUS_PROMPT_DIET` | `true` | A shorter, sharper prompt: a short operating block in place of the long notes, capped descriptions for non-core tools (core tools such as Bash, Edit and Read keep their full manuals), and no tool-selection or routing sections. Sets the defaults of `AGENTAUS_SKILL_LIBRARY`, `AGENTAUS_SKILL_ROUTING` and `AGENTAUS_INVESTIGATE` to `false`; each still wins when set. See `agentaus_bridge/diet.py` |
| `AGENTAUS_TEMPERATURE` | `0.2` | Sampling temperature for main-loop calls (helper calls are not affected). Empty sends none, so Agentaus uses its default; `client` forwards Claude Code's value, which it omits while adaptive thinking is on. Must be a finite number from 0 to 2; anything else sends none and logs a warning |
| `AGENTAUS_TOP_P` | *(empty)* | Nucleus sampling for main-loop calls. Empty sends none; a value outside above 0 up to 1 (or not a finite number) sends none and logs a warning |
| `AGENTAUS_FINISH_GATE` | `true` | Before a turn ends on prose, re-ask once when a file the request named does not exist, code was edited and nothing ran since, or the last test run still failed |
| `AGENTAUS_FINISH_CHECKLIST` | `false` | Also re-ask once to check the work against the request's numbered items and named spec. Off: on solved tasks it cost 1.6-3.7 min and changed no score |
| `AGENTAUS_DELIVERABLE_NUDGE_CALLS` | `0` | After this many tool calls without the requested file written, say so in the system prompt. Off by default, because the write-now note already does |
| `AGENTAUS_REWRITE_NUDGE` | `true` | Once a turn has written a large file, ask for `Edit`s to it rather than whole-file rewrites |
| `AGENTAUS_NAME_CHECK` | `true` | After an edit to a `.py` file, say in its result which names it left undefined in the functions it touched, until they are defined |
| `AGENTAUS_READ_FIRST` | `true` | Put a `Read` in front of an `Edit`/`Write` that Claude Code would refuse because the file was not read, or changed since |
| `AGENTAUS_SKILL_ROUTING` | `true` (`false` under `AGENTAUS_PROMPT_DIET`) | Restore project skill descriptions, add the "if this, use that" section for skills and agent types, and have the planner name its choice. See [Routing](../design/skills.md#routing-if-this-use-that) |
| `AGENTAUS_SKILL_LIBRARY` | `true` (`false` under `AGENTAUS_PROMPT_DIET`) | Offer the bridge's own skill library to Agentaus in every project and answer `Skill` calls for it |
| `AGENTAUS_SUBAGENTS_FOLLOW_SESSION` | `true` | A subagent runs on the model its session's main loop is on. Off, an Agentaus session's subagents go to Claude |
| `AGENTAUS_WORKFLOW_GRAPH` | `true` | Run a bridge agent graph in place of the workflow scripts Agentaus writes. See [the dynamic graph](../design/ultracode.md#the-dynamic-graph-planners-decide-the-shape-in-markdown) |
| `AGENTAUS_GRAPH_KIND` | `dynamic` | `dynamic` (planners decide the shape, in Markdown, from skill files) or `fixed` (the ten-stage graph) |
| `AGENTAUS_GRAPH_WIDTH` | `8` | Most graph agents running at once |
| `AGENTAUS_GRAPH_FOR_CODE` | `false` | Use the fixed graph for tasks that change code too. The dynamic graph always takes them |
| `AGENTAUS_COAUTHOR` | `Agentaus <theteam@trellisdata.com>` | The `Co-Authored-By:` line commits on Agentaus turns end with, in place of Claude Code's Claude one. Empty leaves Claude Code's line. See [Commit credit](../../README.md#commit-credit) |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8787` | Listen address |
| `BRIDGE_PASSTHROUGH` | `true` | Forward non-Agentaus models to Anthropic; `false` answers them with Agentaus instead |
| `ANTHROPIC_UPSTREAM_BASE_URL` | `https://api.anthropic.com` | Passthrough target |
| `BRIDGE_PING_INTERVAL` | `10` | Seconds between keep-alive pings while Agentaus is silent |
| `BRIDGE_CHUNK_CHARS` | `60` | Re-chunk buffered replies into smaller deltas; `0` disables |
| `BRIDGE_CONNECT_TIMEOUT` | `15` | Connect timeout (seconds) |
| `BRIDGE_READ_TIMEOUT` | `300` | Per-read budget upstream. Streaming resets it on every token, so it only bounds waiting on *nothing*. Was 1800, which turned a dead connection into 30 minutes of silence |
| `BRIDGE_STALL_WARNING` | `45` | Log that an upstream call is still waiting, and for how long. `0` disables |
| `AGENTAUS_PROMPT_STYLE` | `markdown` | How the bridge lays out what it writes for Agentaus: `markdown` or `xml` |
| `AGENTAUS_LIVE_DRAFT` | `true` | Stream tokens into a thinking block while the answer is held for checks |
| `AGENTAUS_TOOL_FOCUS` | `true` | Hold back Claude Code's non-coding harness tools (Artifact, Cron, DesignSync...) unless named or used. Custom tools are never hidden; MCP tools are held back only over the schema budget - see [Behaviours](behaviours.md#mcp-tools-over-the-schema-budget) |
| `AGENTAUS_REVIEW_TOOLLESS` | `false` | Also review tool-less requests - Claude Code's own utility calls. For bare-prompt harnesses only |
| `BRIDGE_TURN_TIMEOUT` | `900` | Wall-clock ceiling on one streamed Agentaus turn. Pings defeat Claude Code's idle timeout, so without this a wedged turn spins forever. `0` disables |
| `BRIDGE_MAX_RETRIES` | `1000000` | Extra attempts after a transient upstream failure: in effect, keep retrying through an outage |
| `BRIDGE_RETRY_BACKOFF` | `1` | Base backoff in seconds; doubles per attempt, plus jitter |
| `BRIDGE_RETRY_MAX_DELAY` | `300` | Ceiling on a single backoff wait |
| `BRIDGE_RATE_LIMIT_MAX_DELAY` | `20` | Ceiling on the wait after an HTTP 429, from Agentaus or the Anthropic passthrough: each wait is jittered between half the ceiling and the ceiling, and never exceeds it (values under 1 count as 1). A 429 is Agentaus' per-tenant rate window, which reopens in seconds; the outage backoff above is for outages |
| `BRIDGE_TOKEN` | *(empty)* | Require this token from clients; empty means no client auth |
| `BRIDGE_LOG_LEVEL` | `info` | `debug` for per-request detail |
| `BRIDGE_LOG_BODIES` | `false` | Log full request bodies — **includes your source code** |

Command-line flags override the file: `--host`, `--port`, `--env-file`, `--log-level`,
and `--check` (verify the credential and exit).
