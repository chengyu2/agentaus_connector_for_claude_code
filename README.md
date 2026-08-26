# Agentaus bridge for Claude Code

Run [Agentaus](https://agentaus.com.au) (Trellis Data's sovereign Australian model)
inside Claude Code **alongside** the built-in Claude models, and switch between them
mid-session with `/model`.

Claude Code only speaks the **Anthropic Messages API**. Agentaus speaks an
**OpenAI-style chat-completions API**. This bridge is a small local server that
translates between them — and, crucially, routes *per request* so both providers stay
available in the same session.

```
                    ┌──────────────────────────────────────────┐
                    │            Claude Code (CLI)             │
                    │   ANTHROPIC_BASE_URL=127.0.0.1:8787      │
                    └────────────────────┬─────────────────────┘
                                         │  Anthropic Messages API
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │       agentaus_bridge (this repo)        │
                    │   routes on the request's `model` field  │
                    └───────┬──────────────────────────┬───────┘
              model=agentaus│                          │model=claude-*
                            ▼                          ▼
              ┌──────────────────────────┐  ┌────────────────────────┐
              │  agentaus.com.au         │  │  api.anthropic.com     │
              │  /api/v1/chat/completions│  │  (forwarded untouched) │
              │  translated OpenAI format│  │  your normal Claude auth│
              └──────────────────────────┘  └────────────────────────┘
```

Pick `agentaus` in `/model` and the turn goes to Canberra. Pick Sonnet or Opus and the
turn goes to Anthropic exactly as it would without the bridge.

---

## Before you start

The bridge is the **last** piece, not the first. It does not install Claude Code and it
cannot make Claude Code work — it only adds a second model to a setup that already runs.
Get these in place, in this order:

| # | Prerequisite | How |
| --- | --- | --- |
| 1 | **Visual Studio Code** | Download and install from [code.visualstudio.com](https://code.visualstudio.com). |
| 2 | **The Claude Code extension** | Published by **Anthropic**, displayed as **"Claude Code for VS Code"**, marketplace id `Anthropic.claude-code`. In VS Code: Extensions (`⇧⌘X`) → search *Claude Code* → Install. |
| 3 | **A Claude Code session that already works** | Open the extension, sign in (claude.ai subscription or an `ANTHROPIC_API_KEY`), and confirm an ordinary prompt answers. Do this **before** touching the bridge — otherwise you cannot tell a bridge fault from a sign-in fault. |
| 4 | **Python 3.9 or newer** | `python3 -V`. The Python that ships with macOS is new enough. |
| 5 | **An Agentaus API key** | Sign up at [agentaus.com.au](https://agentaus.com.au), then create a key in the API dashboard. |

### The CLI and the extension are not the same thing

Claude Code comes in two forms, and this matters later:

- **The VS Code extension** — what most people install. There is **no `claude` command** on
  your PATH with an extension-only install.
- **The standalone `claude` CLI** — a separate install.

Check which you have with `which claude`. If it prints nothing you have the extension only,
which is fine — but `scripts/claude-agentaus.sh` cannot work for you, because it `exec`s
`claude`. Use a settings file instead (Option 1 or 2 under
[Pointing Claude Code at the bridge](#pointing-claude-code-at-the-bridge)).

---

## Quick start

```bash
git clone https://github.com/chengyu2/agentaus_connector_for_claude_code.git
cd agentaus_connector_for_claude_code
./scripts/install.sh                           # virtualenv, dependencies, .env scaffold
$EDITOR .env                                   # paste your AGENTAUS_API_KEY
./.venv/bin/python -m agentaus_bridge --check   # verify the key with a live call
./scripts/start-bridge.sh                      # start the bridge (leave this terminal open)
```

A successful `--check` prints `OK - Agentaus replied` with a token count. If it prints
`HTTP 401`, the key is wrong or expired — fix that before going further, because nothing
downstream can work without it.

Then **tell Claude Code the bridge exists.** This is a separate step and it is where almost
everyone gets stuck: the settings file has to go in the directory you actually *work* in,
which is usually **not** this repo. Read
[Pointing Claude Code at the bridge](#pointing-claude-code-at-the-bridge) before editing
anything, then restart Claude Code and run `/model` — **Agentaus (Trellis Data)** appears
below Opus, Sonnet and Haiku.

Optionally, install the [launchd agent](#running-it-in-the-background) so the bridge starts
at login and you never have to think about it again.

---

## The Agentaus API contract

Verified against the live API while building this bridge:

| Property | Value |
| --- | --- |
| Base URL | `https://agentaus.com.au` |
| Endpoint | `POST /api/v1/chat/completions` |
| Auth | `Authorization: Bearer <key>` |
| Format | OpenAI chat-completions (`messages`, `tools`, `tool_choice`, `stream`) |
| Models | one: `agentaus` (the `model` field is accepted but ignored) |
| Streaming | OpenAI SSE `data:` chunks terminated by `[DONE]` |
| Tool calling | full round trip: `tool_calls` out, `role:"tool"` + `tool_call_id` back in |
| Usage | `{"input_tokens": N, "output_tokens": N}` |
| Extension | `system_prompt_overwrite: true` replaces the built-in Agentaus persona |

Two behaviours drove design decisions in the bridge:

1. **The default persona costs ~2,200 input tokens on every request** and instructs the
   model to behave as a general assistant, which fights Claude Code's agent prompt.
   The bridge always sends `system_prompt_overwrite: true` so Claude Code's system
   prompt is the only one in play. (`AGENTAUS_SYSTEM_PROMPT_OVERWRITE=false` to revert.)
2. **Agentaus streams in large buffered chunks**, sometimes emitting nothing at all
   until the whole answer is ready. Claude Code aborts any stream that goes silent for
   300 seconds, so the bridge emits its own SSE `ping` events every 10 seconds while it
   waits. Without this, long generations would look like a dead connection.

One gotcha worth recording: Agentaus returns **HTTP 406** if you send
`Accept: text/event-stream`. The bridge sends `Accept: */*` and lets the `stream` body
flag select the response format.

---

## Pointing Claude Code at the bridge

Claude Code discovers the bridge through **environment variables**, and the thing that
trips people up is *scope*: **which directory the settings file lives in decides which
sessions can see Agentaus.**

> ### The single most common mistake
>
> This repo ships its own `.claude/settings.json`. That file is **project-scoped** — it
> applies only while the folder open in your editor **is this repo**.
>
> So if you clone the bridge to `~/agentaus_connector_for_claude_code` but do your real
> work in `~/my_project`, that file never loads, and Agentaus never appears in `/model`.
> The bridge is running perfectly; Claude Code was simply never told about it. Nothing in
> the log will look wrong, because no request ever arrives.
>
> Fix: put the variables at **user level** (Option 1), or in **the project you actually
> work in** (Option 2).

### Option 1 — every project on this machine (recommended)

Put the `env` block in your **user-level** settings at `~/.claude/settings.json`. It
applies in every directory you open, so this is the "set it once" answer.

**Merge** it into whatever that file already contains — do not overwrite the file, it
probably holds your model and permission preferences:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Agentaus (Trellis Data)",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Sovereign Australian model via the local Agentaus bridge"
  }
}
```

Because this routes **every** project through the bridge, pair it with the
[launchd agent](#running-it-in-the-background). Without that, any time the bridge is not
running, *all* of your Claude Code sessions fail — Claude models included, since they reach
Anthropic through the same base URL. See
[the trade-off this creates](#the-trade-off-this-creates).

### Option 2 — one specific project

The same `env` block, but in `<that project>/.claude/settings.json` — meaning the project
you work in, **not** this repo.

Use this when you want Agentaus in one place and untouched Claude everywhere else. It also
contains the blast radius: a stopped bridge breaks only that project.

If the project is a shared git repo, put it in `.claude/settings.local.json` instead —
same effect, but it is gitignored by convention, so you are not committing a `localhost`
endpoint that only resolves on your machine.

### Option 3 — the launcher script (standalone CLI only)

`./scripts/claude-agentaus.sh` sets every variable and execs `claude`, forwarding its
arguments, so `./scripts/claude-agentaus.sh --model agentaus` starts directly on Agentaus.

**This needs the standalone `claude` CLI on your PATH.** With a VS Code
extension-only install there is no `claude` binary and the script dies with
`command not found`. Check with `which claude` first.

### Option 4 — export by hand, for one shell

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_CUSTOM_MODEL_OPTION="agentaus"
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Agentaus (Trellis Data)"
export ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="Sovereign Australian model via the local bridge"
claude
```

Lasts as long as the shell. Useful for a one-off test, not for daily use.

### Whichever you choose: restart afterwards

`ANTHROPIC_CUSTOM_MODEL_OPTION` is the variable that adds the row to the `/model` picker,
and Claude Code reads environment variables **once at startup**. After editing settings,
restart the session — in VS Code, reload the window — or the row will not appear.

Then run `/model`: **Agentaus (Trellis Data)** sits below Opus, Sonnet and Haiku. Claude
Code skips validation on that model id, so any string the bridge understands works.

### Why the launcher does *not* set `ANTHROPIC_AUTH_TOKEN`

Setting a gateway credential replaces your claude.ai subscription login for the whole
session — the subscription stops being used and every Claude request bills per token to
that credential instead. Leaving it unset keeps your normal login active for Claude
models while Agentaus is billed to its own key inside the bridge.

The bridge forwards the `anthropic-beta` header verbatim, which is what carries the
OAuth capability a subscription login needs, so passthrough works unchanged.

If you normally authenticate with an `ANTHROPIC_API_KEY`, that keeps working too — the
bridge passes `Authorization` and `x-api-key` straight through.

---

## Can settings.json point different models at different endpoints?

No — and this is the reason the bridge exists.

Claude Code has exactly **one** `ANTHROPIC_BASE_URL` per session. It is read once at
startup and applies to every request the CLI makes: your turns, background title and
summary calls, everything. There is no per-model endpoint map in `settings.json`, and
the related variables do not provide one:

| Variable | What it actually changes |
| --- | --- |
| `ANTHROPIC_BASE_URL` | The single endpoint for **all** requests |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` / `SONNET` / `HAIKU` | Only which **model id** an alias maps to — the request still goes to `ANTHROPIC_BASE_URL` |
| `ANTHROPIC_CUSTOM_MODEL_OPTION` | Only adds a **row to the `/model` picker** — no endpoint of its own |

So per-model routing has to happen *behind* that one URL. That is precisely what this
bridge does: Claude Code sees a single endpoint, and the bridge fans out per request on
the `model` field in the body.

**The upshot is that you already get seamless switching.** Inside one session,
`/model agentaus` goes to Canberra and `/model opus` goes to Anthropic, with no restart.
The only thing that needs a restart is *adding* the picker entry, because environment
variables are read once at startup — not switching between entries afterwards.

### The trade-off this creates

Because everything flows through one process, **the bridge is a single point of failure
for both providers**. If the bridge is down — or, as happened here, the machine's DNS
resolver breaks — Claude models stop working too, so you cannot fall back to Claude to
debug the problem. Two things mitigate it:

- launchd (`KeepAlive`) restarts the bridge automatically if it dies.
- Transient upstream faults are retried with backoff rather than failing the turn.

If you need a guaranteed escape hatch, keep a second Claude Code profile with no
`ANTHROPIC_BASE_URL` set at all; it talks to Anthropic directly and bypasses the bridge
entirely.

The bridge deliberately does **not** fall back to Claude when Agentaus is unreachable.
Silently sending a prompt to a different provider than the one you selected would move
your code offshore without telling you.

---

## How routing works

Every request is routed on the `model` field in the request body:

| Model id | Destination |
| --- | --- |
| contains `agentaus` | translated → Agentaus |
| anything else | forwarded byte-for-byte → `api.anthropic.com` |

(When `BRIDGE_PASSTHROUGH=false` the second row also goes to Agentaus — see below.)

Passthrough is deliberately *transparent*: headers and error bodies are relayed
unmodified, because Claude Code matches on upstream error wording to decide when to
retry with a capability disabled. Wrapping those errors would break that recovery.

Two switches change the shape of this:

- `AGENTAUS_FORCE_ALL=true` — send **everything** to Agentaus regardless of model id.
  Useful to check what a pure-Agentaus session feels like, including the background
  Haiku calls Claude Code makes for titles and summaries.
- `BRIDGE_PASSTHROUGH=false` — never contact Anthropic at all. Non-Agentaus model ids
  are answered by Agentaus rather than forwarded, so an air-gapped session keeps working
  and no request leaves for Anthropic even if Claude Code asks for a Claude model.

To route only *some* work to Agentaus, set the alias variables. For example, keep Opus
for the main loop but send cheap background calls to Agentaus:

```bash
export ANTHROPIC_DEFAULT_HAIKU_MODEL="agentaus"
```

---

## What happens when a conversation outgrows Agentaus

Agentaus' window is **131,072 tokens** — prompt and reply together — against Claude's
much larger one. Two things make it bite sooner than you would expect: there is no
prompt caching, so every turn resends the whole conversation, and Claude Code sizes its
auto-compact against the window it *assumes* for a model, which it has no way to learn
for Agentaus.

Left alone the turn dies, and `/compact` cannot rescue it either — compaction summarises
by sending the conversation to the model, so once you are over the window the compaction
call is over it too
([claude-code#25867](https://github.com/anthropics/claude-code/issues/25867)).

So the bridge compacts by itself, **asking Agentaus to summarise** the older part of the
conversation and sending that summary in its place:

```
system prompt + "[earlier conversation summary] ..."   <- the compacted head
<most recent messages, verbatim>                       <- untouched tail
```

```
WARNING agentaus-bridge: compacted 46 oldest message(s) into a summary to fit
        the 131072-token window (~294955 -> ~54790 tokens)
```

### Why summarise rather than truncate

Dropping the oldest messages is cheap but loses exactly what later turns depend on — a
port number agreed an hour ago, why a approach was rejected, a constraint stated once.
The summariser is prompted to extract rather than paraphrase: file paths, identifiers,
commands, decisions *and their reasons*, bugs and their root causes, what is done versus
outstanding, and explicitly never to invent detail it cannot see.

In a live test, a 295,000-token conversation compacted to 54,790 tokens still answered
correctly about a port, a config path, a rate limit and the reasoning behind a backoff
ceiling — all of which lived in the very first message, long since compacted away.

### What it costs

The bridge is stateless: Claude Code resends the whole conversation every turn, so the
naive version re-summarises the entire history on *every request*. Two things stop that.

**A quantised boundary.** The split between summarised head and verbatim tail is
snapped to a multiple of `AGENTAUS_COMPACT_BLOCK` messages. Unquantised it advances by
about a turn every turn, so the summary is keyed on something that never repeats and
the cache never hits.

**Prefix reuse.** When the boundary does move, the previous summary already covers
most of the new head, so only the messages added since are read and merged in.

Measured live on a ~360,000-token conversation:

| Turn | First byte | Total | What happened |
| --- | --- | --- | --- |
| 1 (cold) | 10.2s | 190.5s | full summarisation of the history |
| 2 | 0.4s | 1.6s | cached summary reused |
| 3 | 0.4s | 3.2s | cached summary reused |

The first turn after a session outgrows the window is genuinely slow — there is no way
around reading that much text once. Every turn after it is normal speed.

**The response starts before compaction does.** Compaction runs inside the streaming
generator, not before it, so keepalive pings flow while it works. Getting this wrong is
what made a plain "hello" on a large session look like a hang: the response had not
begun, so there was nothing to ping with, and Claude Code sat on an open socket
receiving zero bytes for over 90 seconds.

### The guarantees

- **The question being answered is never touched.** Only the older head is compacted.
- **Tool pairs are never split.** A `tool_result` refers back to a `tool_use` in the
  preceding assistant turn, so the kept tail always begins on a clean user turn —
  cutting between them produces a request Agentaus rejects.
- **The model is told.** The summary is introduced as a compacted record with the
  number of messages it replaces, so it reports missing context instead of inventing it.
- **Failure degrades, it does not cascade.** If summarising fails, the bridge falls back
  to dropping the head; only if that cannot fit either is the turn refused, using the
  `prompt is too long` wording Claude Code can act on.

### The window is learned, not assumed

Agentaus states its own limit when a prompt overflows:

```
The engine prompt length 224662 exceeds the max_model_len 131072
```

The bridge parses that and uses it from then on, so the number stays right if Trellis
Data changes the model. `AGENTAUS_MAX_INPUT_TOKENS` still wins when set explicitly —
learning must never quietly override what you configured. `AGENTAUS_AUTO_TRIM=false`
turns the whole mechanism off and restores the hard error, which is the better outcome
on work where losing early context would be worse than failing.

---

## Changing the port

The bridge listens on `127.0.0.1:8787` by default. If something else already owns that
port — or you want to run two bridges side by side — the port appears in **two** places
that must agree, and they are read by different processes:

**1. Tell the bridge where to listen** (`.env`):

```dotenv
BRIDGE_PORT=9100
```

**2. Tell Claude Code where to find it** (`.claude/settings.json`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9100",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Agentaus (Trellis Data)",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Sovereign Australian model via the local Agentaus bridge"
  }
}
```

Then restart both:

```bash
./scripts/start-bridge.sh                  # or: launchctl kickstart -k gui/$(id -u)/com.trellisdata.agentaus-bridge
```

and restart Claude Code — `ANTHROPIC_BASE_URL` is read once at startup, so a running
session keeps talking to the old port.

Verify the two ends agree before anything else:

```bash
curl -s http://127.0.0.1:9100/healthz
```

A JSON reply means the bridge is listening. `Connection refused` means it is not on
that port — check `.env` and that the bridge actually restarted.

**If you miss step 2**, Claude Code keeps calling the old port and every request fails
with a connection error, including Claude models, because all traffic flows through the
bridge. That symptom looks like the model is down when it is really a port mismatch.

### The other places a port can appear

| Where | How to change it | When it matters |
| --- | --- | --- |
| `scripts/claude-agentaus.sh` | `export BRIDGE_URL="http://127.0.0.1:9100"` before running it | Only if you use the launcher; it reads `BRIDGE_URL` and needs no edit |
| `tests/smoke_test.py` | `--url http://127.0.0.1:9100` | Running the end-to-end checks |
| Command line | `python -m agentaus_bridge --port 9100` | Overrides `.env` for one run, handy for testing |
| launchd plist | nothing — it reads `.env` | No edit needed |

### Picking a free port

```bash
lsof -ti tcp:9100        # prints a pid if something already owns it, silence if free
```

Stay above 1024 (lower ports need root) and avoid anything already in use — the bridge
exits with `Address already in use` rather than starting on a different port, which is
deliberate: a silent fallback would leave Claude Code pointed at nothing.

---

## Tools the bridge runs itself

Every other tool in Claude Code belongs to the client: Claude Code sends the schema, the
bridge translates the call, Claude Code executes it. `agentaus_search` is different. The
bridge adds it to the tool list Agentaus sees, and when Agentaus calls it the bridge runs
it and feeds the result straight back. The `tool_use` never reaches Claude Code, so you
never see a permission prompt for it and never see it in the transcript — only the answer
it produced.

### Why not just use Grep

Because `Grep` is a regex, and a smaller model writes a regex against a *guess* about
what the code looks like. It gets a plausible match and answers from it, or gets nothing
and concludes the code does not exist. Ask *"where do we cap concurrent calls?"* and the
answer is `asyncio.Semaphore(6)` — which shares not one word with the question.

`agentaus_search` reads by meaning instead:

```
query: "where do we cap concurrent calls?"
  │
  ├─ expand ......... one Agentaus call → semaphore, concurrency, limit, pool
  ├─ shortlist ...... substring scan over the tree, ranked by distinct-term hits
  ├─ fallback ....... fewer than 3 candidates? the words are absent, not the answer
  │                   → read every file instead
  ├─ chunk .......... 4k tokens each, tagged with path and line range
  ├─ fan out ........ one Agentaus call per chunk, 6 at a time
  └─ merge .......... drop the NONEs, keep the quotes, cite file:line
```

`Grep` is still there and Claude Code still executes it — only its *description* changes
on Agentaus turns, to say it is for exact literal matches you can already spell. The
schema is untouched.

**What it will not read.** `.env` files, `*.pem`, `*.key`, SSH keys and `.git/` are
excluded no matter what matches. Set `AGENTAUS_SEARCH_ROOTS` to confine the bridge to
specific trees — worth doing, because these reads never pass through Claude Code's
permission prompts.

**What it costs.** One expansion call plus one call per chunk, capped at
`AGENTAUS_SEARCH_MAX_CHUNKS`. When the cap truncates a search, the result says so — a
silent cap reads as full coverage, which is worse than a stated partial one.

---

## Web search

Claude Code's `WebSearch` is an Anthropic **server-side** tool: it arrives as
`{"type": "web_search_20250305", "name": "web_search"}` with no `input_schema`, and the
translator drops it along with every other server-side stub. So an Agentaus turn has no
web search of its own — it can `WebFetch` a URL it already has, but it cannot discover one.

Agentaus does have web search. It is triggered by the **prompt**, not by a parameter:
the phrase *"web search this"* is what turns it on. That makes it invisible to an agent
loop — the model either happens to say it or does not, and nothing can observe the result
or cite it.

`agentaus_web_search` turns that convention into a tool. The bridge runs it by making one
Agentaus call whose prompt begins `web search this: <query>`, and asks for source URLs
rather than an answer from memory:

```
agentaus_web_search("latest httpx release")
        │
        └─ Agentaus  ← "web search this: latest httpx release
                         Answer from what the search returns, not from memory.
                         For every fact you state, give the source URL..."
```

The search runs **inside Agentaus**, so nothing leaves the sovereign path to answer it —
unlike `WebFetch`, whose page summarisation is performed by Claude Code and routes to
Anthropic even on an Agentaus turn.

**Expect it to be slower than an ordinary reply**, because a real search runs behind it.
The keepalive pings are what stop that looking like a stall. Turn it off with
`AGENTAUS_WEB_SEARCH=false`.

---

## Thinking, synthesised

Claude plans inside a thinking block before it acts. Agentaus has no such mode, so left
alone it answers from the first thing that comes to mind — the "started editing before
working out what the change required" failure.

So the bridge asks for the plan as its own turn, shows it as a thinking block, and hands
it back as context for the real call:

```
your message
     │
     ├─ plan call ...... "what does this turn require? what must you find out?"
     │                    → rendered as ✻ Thinking… in the UI
     ├─ answer call .... same request, with the plan in the system prompt
     └─ review call .... "what is wrong with this answer?" → revise if needed
```

It runs when the turn carries tools, or when you have extended thinking switched on in
Claude Code — the client's own toggle drives it. A plain prose question gets neither,
because a planning round trip on *"what does this function do"* costs latency and buys
nothing.

This is the same trade the review pass makes: **two cheap passes beat one expensive one
on a smaller model.** It also costs a round trip, so `AGENTAUS_THINKING=false` turns it
off, and `AGENTAUS_THINKING_VISIBLE=false` keeps the plan without displaying it.

Nothing replays these blocks: when Claude Code sends them back on the next turn, the
translator drops them, the same as it does for Claude's own.

---

## One cap on everything the bridge does on its own

The bridge makes Agentaus calls you never asked for — summarising a long history,
checking that summary for gaps, reviewing an answer, planning a turn, reading every chunk
of a haystack. `AGENTAUS_MAX_CONCURRENCY` (default **6**) bounds all of them together.

Global rather than per-feature, deliberately: two features each capped at 6 permit 12, and
the number that matters is how hard the bridge hits one upstream. **Your own turn is never
gated** — queueing the request you actually made behind the bridge's background work would
turn a busy cap into a visible stall.

The consequence worth knowing: a cold compaction is roughly 47 calls and will hold the cap
for its duration, so a search starting at that moment waits. Compaction runs before a turn
and search during it, so they rarely overlap. When a call does wait more than a second, the
log says so.

---

## Configuration reference

All settings are environment variables, readable from `.env`. Shell exports win over
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
| `AGENTAUS_MAX_CONCURRENCY` | `6` | One global cap on every Agentaus call the bridge makes on its own initiative — summarising, reviewing, planning, searching. Your own turn is never queued behind it. Replaces `AGENTAUS_SUMMARY_CONCURRENCY`, which is still read when set |
| `AGENTAUS_SEARCH` | `true` | Offer `agentaus_search`, the bridge-executed semantic search, and steer `Grep` towards literal lookups |
| `AGENTAUS_WEB_SEARCH` | `true` | Offer `agentaus_web_search`, which drives Agentaus' own web search. Claude Code's `WebSearch` is dropped in translation, so without this an Agentaus turn cannot search the web at all |
| `AGENTAUS_SEARCH_CHUNK_TOKENS` | `4000` | File content per search call |
| `AGENTAUS_SEARCH_MAX_CHUNKS` | `120` | Ceiling on calls for one search. Truncation is reported in the result, never silent |
| `AGENTAUS_SEARCH_MIN_CANDIDATES` | `3` | Below this many shortlisted files, distrust the shortlist and read everything |
| `AGENTAUS_SEARCH_MAX_FILE_BYTES` | `1048576` | Skip files larger than this |
| `AGENTAUS_SEARCH_ROOTS` | *(empty)* | Colon-separated directories search may read. Empty allows any absolute path, matching Claude Code's own `Read` |
| `AGENTAUS_TOOL_ROUNDS` | `3` | How many rounds of bridge-executed tool calls one turn may run before the answer has to stand |
| `AGENTAUS_THINKING` | `true` | Plan the turn in a separate call before answering it |
| `AGENTAUS_THINKING_VISIBLE` | `true` | Show that plan as a thinking block. `false` still uses it, but does not display it |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8787` | Listen address |
| `BRIDGE_PASSTHROUGH` | `true` | Forward non-Agentaus models to Anthropic; `false` answers them with Agentaus instead |
| `ANTHROPIC_UPSTREAM_BASE_URL` | `https://api.anthropic.com` | Passthrough target |
| `BRIDGE_PING_INTERVAL` | `10` | Seconds between keep-alive pings while Agentaus is silent |
| `BRIDGE_CHUNK_CHARS` | `60` | Re-chunk buffered replies into smaller deltas; `0` disables |
| `BRIDGE_CONNECT_TIMEOUT` | `15` | Connect timeout (seconds) |
| `BRIDGE_READ_TIMEOUT` | `600` | Read timeout (seconds) |
| `BRIDGE_MAX_RETRIES` | `2` | Extra attempts after a transient upstream failure |
| `BRIDGE_RETRY_BACKOFF` | `0.5` | Base backoff in seconds; doubles per attempt, plus jitter |
| `BRIDGE_RETRY_MAX_DELAY` | `8` | Ceiling on a single backoff wait |
| `BRIDGE_TOKEN` | *(empty)* | Require this token from clients; empty means no client auth |
| `BRIDGE_LOG_LEVEL` | `info` | `debug` for per-request detail |
| `BRIDGE_LOG_BODIES` | `false` | Log full request bodies — **includes your source code** |

Command-line flags override the file: `--host`, `--port`, `--env-file`, `--log-level`,
and `--check` (verify the credential and exit).

---

## What translates, and what cannot

**Handled**

| Anthropic concept | Becomes |
| --- | --- |
| `system` blocks | a leading `system` message + `system_prompt_overwrite` |
| `tool_use` block | OpenAI `tool_calls` entry with JSON-encoded arguments |
| `tool_result` block | a `role:"tool"` message carrying `tool_call_id` |
| `is_error` on a result | the body is prefixed `[tool error]` |
| `tools` / `input_schema` | OpenAI function definitions |
| `tool_choice` `auto`/`any`/`tool`/`none` | `auto` / `required` / named function / `none` |
| streaming | a fully-formed Anthropic SSE sequence with matched block open/close |

**Degraded, by necessity**

| Feature | Behaviour |
| --- | --- |
| Images and PDFs | Agentaus is text-only, so attachments become a bracketed note rather than being silently dropped. Screenshot-based workflows will not work. |
| Extended thinking | Agentaus exposes no reasoning channel, so `thinking` blocks from earlier Claude turns are stripped before sending. The bridge substitutes its own: it asks Agentaus to plan the turn as a separate call and shows that plan as a thinking block. See [Thinking, synthesised](#thinking-synthesised). |
| Context window | Agentaus accepts **131,072 tokens** total, prompt plus reply — far less than Claude. Claude Code sizes auto-compact against the window it *assumes* for a model and cannot learn Agentaus' real one, so it will not compact in time by itself. The bridge enforces the limit and phrases the refusal as `prompt is too long: N tokens > M maximum`, the wording Claude Code matches on to compact and retry. **If you are already well over the limit, `/compact` fails too** — compaction summarises by sending the conversation to the model, so it needs to fit in the window as well ([claude-code#25867](https://github.com/anthropics/claude-code/issues/25867)). Switch to a Claude model, `/compact` there, then switch back — an escape you only have because the bridge keeps both providers live in one session. |
| Prompt caching | Not supported upstream, so `cache_control` markers do nothing. Every turn resends the whole conversation — the main cost driver in long sessions. |
| Token counting | Agentaus has no tokenizer endpoint, so `/v1/messages/count_tokens` returns a chars÷4 estimate. It feeds the context meter and auto-compact trigger only, never billing. |
| `max_tokens`, `temperature` | Accepted by Agentaus but ignored, so they are passed along without effect. |
| Anthropic server-side tools | Web search and code execution stubs have no `input_schema` and are dropped, so Claude Code's own `WebSearch` never reaches Agentaus. The bridge replaces it with `agentaus_web_search`, which drives Agentaus' own search — see [Web search](#web-search). `WebFetch` is a client-side tool and survives translation unchanged. |

**Worth knowing before you rely on it**

Agentaus is a general-purpose assistant, not a model tuned for long agentic tool loops.
In testing it handled single tool calls and tool results correctly, but it is
noticeably more eager to re-call a tool it has already run than Claude is. It suits
questions, analysis, drafting and review inside a Claude Code session better than
long autonomous multi-step edits. Because there is no prompt caching, long sessions on
Agentaus also cost more per turn than the token counts alone suggest.

Two Claude Code features always call `api.anthropic.com` directly and never traverse the
bridge: the fast-mode availability check and the WebFetch domain safety check. On a
network that blocks Anthropic entirely those will report errors while inference keeps
working.

---

## Running it in the background

To avoid keeping a terminal open, install a launchd agent.

**Generate it — do not copy a literal path out of this README**, because the paths must
point at *your* clone. Run this from the repo root:

```bash
CONN="$(pwd)"

cat > ~/Library/LaunchAgents/com.trellisdata.agentaus-bridge.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.trellisdata.agentaus-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>${CONN}/.venv/bin/python</string>
    <string>-m</string>
    <string>agentaus_bridge</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${CONN}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/agentaus-bridge.log</string>
  <key>StandardErrorPath</key><string>/tmp/agentaus-bridge.log</string>
</dict>
</plist>
EOF

plutil -lint ~/Library/LaunchAgents/com.trellisdata.agentaus-bridge.plist
launchctl load ~/Library/LaunchAgents/com.trellisdata.agentaus-bridge.plist
```

The bridge reads its own `.env` from `WorkingDirectory`, so the key does not go in the
plist. A wrong path here fails **silently** — `launchctl list` shows the label with no pid.
That is why `plutil -lint` and the `launchctl list` check below are worth running.

`RunAtLoad` plus `KeepAlive` means **nothing needs running by hand after a reboot** —
launchd starts the bridge at login and restarts it if it dies. `.claude/settings.json`
lives on disk, so Claude Code picks the Agentaus option back up on its own. Confirm with:

```bash
launchctl list | grep agentaus      # a pid means it is running
curl -s http://127.0.0.1:8787/healthz
```

If you run the bridge with `./scripts/start-bridge.sh` in a terminal instead of under
launchd, that one *does* have to be started again after every reboot.

---

## Testing

```bash
./.venv/bin/python -m unittest discover -s tests -v   # unit tests, no network needed
./.venv/bin/python tests/smoke_test.py                # quick end-to-end, needs a running bridge
./.venv/bin/python tests/integration_test.py          # full live flow, costs real tokens
```

`integration_test.py` exercises what a real session does, nothing stubbed: per-model
routing to both providers, switching between them, streaming, tool-call round trips,
an oversized conversation recovering via auto-trim, and an untrimmable one being
refused with wording Claude Code can act on.

The unit tests cover the translation edge cases that break agent loops: tool-result
ordering, fragmented tool-call deltas, block open/close pairing in the SSE stream, and
malformed tool arguments.

---

## Reading the log

`/tmp/agentaus-bridge.log`. Every line for one turn carries the same short request id,
so a single turn can be followed end to end:

```
req f5782343 recv model=agentaus route=agentaus stream=True msgs=81 est=241482 bytes=963487
req f5782343 compaction start (est 241,482 tok, target 131,072)
req f5782343 compaction done in 189.4s
req f5782343 compaction result: 241482 -> 2593 est tokens
req f5782343 upstream start (est 2946 tok)
req f5782343 POST /v1/messages model=agentaus route=agentaus stream=true -> 200 in 190.5s
```

| Line | Tells you |
| --- | --- |
| `recv` | A request arrived, and how big it is. Logged **on arrival**, not on completion — a hung turn never completes, so a completion-only log never mentions it at all. |
| `compaction start` / `done` | The slowest phase, timed. A `start` with no matching `done` is the signature of a hang. |
| `compaction result` | What compaction achieved, in tokens. |
| `upstream start` | Handed off to Agentaus; anything after this is the model's time, not the bridge's. |
| `passthrough -> ` | A Claude-model turn was forwarded to Anthropic, with the status. |
| `client disconnected after` | The caller gave up. Without this line an abandoned turn looks exactly like one still running. |
| `token calibration:` | The fitted relationship between our token count and the one Agentaus charges. |

To follow one turn: `grep 'req f5782343' /tmp/agentaus-bridge.log`.
To watch live: `tail -f /tmp/agentaus-bridge.log`.

`BRIDGE_LOG_LEVEL=debug` adds a line per summarisation call. `BRIDGE_LOG_BODIES=true`
logs full request bodies — **including your source code** — so leave it off routinely.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Agentaus error: Unauthorized` | Bad or expired `AGENTAUS_API_KEY`. Confirm with `--check`. |
| `HTTP 406` from Agentaus | Something sent `Accept: text/event-stream`. The bridge sends `Accept: */*`; check any proxy in front of it. |
| Claude Code hangs, then reports a stream error | The bridge was not running, or pings were stripped by an intermediate proxy. Raise `BRIDGE_LOG_LEVEL=debug` and watch. |
| Agentaus missing from `/model` | `ANTHROPIC_CUSTOM_MODEL_OPTION` was not set before Claude Code started. Variables are read once at startup, so restart the session (reload the VS Code window). |
| Agentaus missing from `/model`, but the bridge is healthy | **Settings scope.** Your `.claude/settings.json` is in the bridge repo, not in the project you have open. Move the `env` block to `~/.claude/settings.json` (all projects) or to that project's own `.claude/settings.json`, then restart. See [Pointing Claude Code at the bridge](#pointing-claude-code-at-the-bridge). |
| `./scripts/claude-agentaus.sh: claude: command not found` | You have the VS Code extension but not the standalone `claude` CLI, so the launcher cannot work. Use a settings file instead (Option 1 or 2). |
| Every project suddenly fails, not just Agentaus | You set `ANTHROPIC_BASE_URL` at user level and the bridge is not running. Start it, or install the launchd agent. `curl -sf localhost:8787/healthz` tells you in one second. |
| Claude models fail while Agentaus works | Passthrough auth. Do not set `ANTHROPIC_AUTH_TOKEN` unless you intend to stop using your subscription. |
| Replies read like a generic assistant, not an agent | `AGENTAUS_SYSTEM_PROMPT_OVERWRITE` got turned off, so the Agentaus persona is back in front of Claude Code's prompt. |
| `API Error: Failed to parse JSON` | Fixed in this repo. The passthrough was returning the upstream body still gzip-compressed while stripping `content-encoding`. Only larger responses are compressed, so it looked random. Covered by `tests/test_passthrough.py`. |
| `API Error` / auto mode fails on every action | Do not set `CLAUDE_CODE_ATTRIBUTION_HEADER=0`. Behind a custom base URL it also strips the block from auto-mode permission-classifier requests, which the API declines with 401. |
| Both Agentaus **and** Claude models fail at once | Almost always DNS or the bridge being down, not either provider. Everything flows through the bridge, so one broken resolver takes out both. See the row below. |
| `nodename nor servname provided, or not known` | The macOS system resolver (`mDNSResponder`) is wedged. Diagnose by comparing `dig agentaus.com.au` (queries the DNS server directly) with `python3 -c "import socket;socket.getaddrinfo('agentaus.com.au',443)"` (uses the system resolver). If `dig` works and `getaddrinfo` fails, every app on the machine is affected, not just the bridge. Fix: `sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder`, or reboot. |
| Agentaus vanished from the `/model` list | The settings file that carried `ANTHROPIC_CUSTOM_MODEL_OPTION` is gone or was edited — check both `~/.claude/settings.json` and the open project's `.claude/settings.json`. That variable creates the row and is read **once at startup**, so restore it and restart Claude Code. |
| Agentaus replies with nothing, or the turn fails with no explanation | The conversation exceeded Agentaus' **131,072-token** window. Agentaus reports this as HTTP 200 with the error inside the SSE body, which older builds turned into a silent empty reply. The bridge now rejects it up front with an actionable message. Run `/compact` or `/clear`, or switch to a Claude model. |
| `This conversation is too long for Agentaus` | Working as intended. Agentaus' window is much smaller than Claude's, and there is no prompt caching, so long sessions hit it quickly. `/compact` usually recovers the session. |
| `/compact` also fails on Agentaus | Expected once you are past 131k: compaction sends the conversation to the model to be summarised, so it overflows too. Switch to a Claude model, compact there, then switch back. `/clear` also works but discards history. |
| A short message on a long session takes minutes | The first compaction after a session outgrows the window reads the whole history. Later turns reuse it and are fast. `grep 'compaction done' /tmp/agentaus-bridge.log` shows the split. |
| Want to know where a slow turn went | Every line for one turn shares a request id: `grep 'req <id>' /tmp/agentaus-bridge.log`. `recv` gives the size on arrival, `compaction start`/`done` the phase timing, `upstream start` the handover, and `client disconnected` if the caller gave up. A start with no matching done is a hang. |
| `Address already in use` on start | An older bridge is still bound: `lsof -ti tcp:8787 \| xargs kill`. |

---

## Security notes

- The bridge binds `127.0.0.1` by default. It has **no client authentication** in that
  mode, which is fine on loopback. If you change `BRIDGE_HOST`, set `BRIDGE_TOKEN` as
  well — anything that can reach the port can otherwise spend your Agentaus quota.
- `.env` is gitignored; `BRIDGE_LOG_BODIES=true` writes full prompts (including your
  source code) to the log, so leave it off outside debugging.
- **The API key is currently stored in plaintext in `agentaus_api_doc.md`** as well as
  `.env`. That file is not gitignored. Before this repo goes anywhere shared, remove the
  key from the document and rotate it — it has been sitting in a file that is easy to
  commit by accident.
- Everything sent to Agentaus leaves your machine for Trellis Data's Australian
  infrastructure, and everything sent to a Claude model leaves for Anthropic. The
  routing table above determines which — check it before putting sensitive code through
  either path.
