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

### Not every call waits its turn

The cap is one number, but the calls behind it are not equivalent. A search chunk sits
inside a tool the model is blocked on — someone is watching a cursor. The second pass that
checks a summary for gaps, or the review that critiques an answer, improves quality and
the turn is correct without it.

A plain semaphore cannot say that, so a burst of background work delays the calls a user
is waiting on. The gate is priority-ordered instead:

| Priority | Calls | Behaviour |
| --- | --- | --- |
| **urgent** | planning, query expansion, search chunks, zoom, web search, merges | served first |
| **normal** | compaction summarisation | default |
| **background** | summary gap-check, self-review, revision, distillation | yields to both |

Equal priorities still queue by arrival, so this changes who waits, never whether the cap
holds.

Two things came out of building it. The review pass was **never gated at all** — up to
three calls per answer went straight past the cap the rest of the bridge respects. And
`AGENTAUS_HELPER_TIMEOUT` was 240s, which abandoned work that was still coming: a slow
call is not a failed one, and six abandoned calls in one run were all of that kind. It is
900s now, and exists to bound a socket that will never speak again rather than to give up
on a busy upstream.

The consequence worth knowing: a cold compaction is roughly 47 calls and will hold the cap
for its duration, so a search starting at that moment waits. Compaction runs before a turn
and search during it, so they rarely overlap. When a call does wait more than a second, the
log says so.

---

## What the live model taught us

Everything in the bridge was written against unit tests first. Driving real turns at
Agentaus found failures no test would have caught, and the fixes are worth knowing about
if you extend this.

**Tag everything.** Agentaus follows explicit structure far more reliably than prose with
capitalised headings. Every prompt the bridge builds uses XML tags — `<request>`,
`<excerpt>`, `<tools_available>`, `<task>`, `<output_format>` — and so does every tool
result it forwards. This is where most of the reliability came from, not from better
wording.

**A helper pass must never judge what it cannot see.** The bridge runs three extra calls
around a turn: plan before, review after, and distil in between. Each is a *fresh*
conversation. Two of them were actively harmful mid-tool-loop:

- The **planner** does not inherit the conversation, so on the second step of a tool loop
  it re-planned the step already taken — telling the model to read a file it was holding.
- The **reviewer** sees only the request and the answer. Given "list the Section 5
  headings" and a correct list of headings, with the source document invisible to it, it
  ruled the answer unverified and the revise pass replaced it with *"Please provide the
  DOCX file."* Measured on a real tender document: **0 of 6 turns correct through the
  bridge, 3 of 3 posting the identical payload straight to Agentaus.** The model was
  never the problem.

Both now sit out whenever the last message is a tool result. Same document afterwards:
**5 of 6**.

**The model invents tool names, and misspells real ones.** It answered a search by calling
`open_file`, which nobody offered it — passed through, that fails a `tool_use` in Claude
Code for a tool it has never heard of, and the turn dies looking like a bridge fault.
Invented names are caught and corrected upstream; `read` for `Read` is resolved silently
rather than spending a round.

**Descriptions decide tool choice, and position within them matters.** Given a bare list
of names the planner picks `Grep`, the one it knows from training. It sees each tool's
description now — and the restriction had to move to the *front* of Grep's description,
because a caveat appended after a paragraph loses to a strong prior.

---

## Distillation

`AGENTAUS_DISTILL_RESULTS` condenses tool results over `AGENTAUS_DISTILL_THRESHOLD_TOKENS`
before they reach Agentaus, cached by content so the conversation prefix stays stable and
the compaction cache keeps hitting.

**It is off by default**, on evidence rather than principle. Condensing a 60,000-character
document added about a minute to one turn, and on work where the tool output *is* the
subject — a tender document being edited, a log being read line by line — condensing it
destroys the thing the user asked about.

Turn it on for long agentic sessions that would otherwise compact repeatedly, where
trading fidelity for window is the right trade, and measure the compaction rate before and
after. Its win shows up as *fewer compactions*, not as better answers.

---

## Zoom: from a citation to something you can write with

`agentaus_search` returns hits like `Drafted_Responses.md:3585-3586` with a line or two
quoted. That is enough to prove a fact is there and **not** enough to write from — and a
model given only that produces exactly what you would expect:

```
ASSERTION: supports scanned images, drawings, correspondence...
EVIDENCE:  "REST-based API catalogue... accepts any file type"
ADD:       "(evidence: FIN-2025-26-00616 - lines 3585-3586)"
```

A citation where a sentence was wanted, because a citation was all it had.

`agentaus_zoom` closes that loop. Give it a file and a line number and it returns the
passage **widened to its section, verbatim, with line numbers intact** — and never calls a
model to do it:

```
agentaus_zoom(file_path=..., start_line=3359)
  │
  ├─ widen ..... outward to the nearest section boundary — Markdown heading, bold-only
  │              line, numbered clause — so the passage arrives with its heading
  ├─ trim ...... to AGENTAUS_ZOOM_MAX_TOKENS, growing outward from the citation so the
  │              lines nearest the quote are the ones kept
  └─ return .... tagged, so the model knows it is holding a window and not a whole file

<passage file="…" lines="3352-3366" you_asked_for="3359-3359"
         section_ends_at="3366" verbatim="true" complete="true">
  3352  **Secure, governed environment**
  ...
</passage>
```

The attributes are the point. A passage that stops mid-section reads, to a model, like a
file that stops there — so it either concludes the content is absent or quotes across the
cut. `complete="false"` and `section_ends_at` say otherwise, and this model follows
structure far more reliably than it follows a sentence.

**It never condenses.** It used to, above a token threshold, and that was wrong from the
start: the point of zooming into a citation is to see the exact words before quoting them,
and condensing them first destroys the only thing the caller came for. Truncating verbatim
is strictly better and instant.

Getting that wrong was expensive. The threshold was an order of magnitude too low, so
every zoom over ordinary tender prose paid for a model call — 73 to 125 seconds under
load, into a 240-second helper timeout, stalling a batch run for an hour. Raising the
threshold moved the bulk downstream until the turn carrying it drew a Cloudflare 524. The
whole class of problem disappeared when the condensation path was deleted rather than
tuned.

Search results end with a line telling the model this tool exists — a citation is only
useful if it knows it can open it.

---

## The guidance describes only what is on the wire

The `<tool_selection>` block in the system prompt is **generated per request** from the
tool list actually being sent, never hardcoded. A static list drifts the moment a feature
is switched off: with `AGENTAUS_ZOOM=false` the model would still be told to call
`agentaus_zoom`, invent it, and spend a correction round being told it does not exist —
the bridge teaching its own upstream a tool the bridge had removed.

Verified on the live bridge with both flags off:

```
AGENTAUS_ZOOM=false AGENTAUS_INVESTIGATE=false
  wire     : ['Glob', 'Read', 'agentaus_search', 'agentaus_web_search']
  guidance : ['Glob', 'agentaus_search', 'agentaus_web_search']
  drift    : none
```

The planning pass gets the same treatment: it is handed each tool's name *and* the opening
of its description, because a planner shown a bare list picks the name it recognises from
training — which is `Grep`.

---

## Where the output goes

**The bridge never writes files.** It translates requests and responses; it has no idea
what a deliverable is. Output reaches disk exactly one way: Agentaus calls `Write` or
`Edit`, and **Claude Code** executes it — through the same permission prompt as any other
write. Nothing is dumped automatically, and there is no output directory.

Two consequences worth knowing:

- **Ask for the file explicitly.** "Review 5.1.1" gets you an answer in the terminal.
  "Review 5.1.1 and write it to `<path>`" gets you a file.
- **Markdown, not `.docx`.** A `.docx` is a zip archive, so a text-writing tool cannot
  produce one — Agentaus can only write text. Have it write `.md`, then convert
  (`pandoc out.md -o out.docx`) if the buyer needs Word.

`agentaus_search` and `agentaus_zoom` are the exception in the other direction: the bridge
runs those itself, so they never reach a permission prompt. They are read-only and
confined by `AGENTAUS_SEARCH_ROOTS`.

---

## Finding out what needs fixing

The bridge compensates for a weaker upstream in a dozen places, and every compensation
logs when it fires. That makes the log a record of which ones are earning their keep,
which are firing so often they point at a real defect, and which have never fired at all
— a far better guide than reading the code and guessing.

```bash
./scripts/diagnose.py                      # reads /tmp/agentaus-bridge.log
./scripts/diagnose.py path/to/other.log
```

It prints throughput, tool-call and compaction latencies, then every signal it can find,
ranked by severity and count, each with what it means and what to do about it. It also
lists what **never** fired, which is the half people forget: a compensation that has
never triggered is either healthy or has never been exercised, and those need telling
apart.

A real run of 223 Agentaus turns reported:

```
[HIGH  ]    63 x  upstream 5xx / gateway timeout
[HIGH  ]     6 x  helper call abandoned on timeout
[HIGH  ]     3 x  tool round limit reached
[HIGH  ]     1 x  summarisation fell back to trimming     <- real context lost
[MEDIUM]    14 x  client disconnected mid-turn
[LOW   ]  1236 x  waited for a concurrency slot
[LOW   ]    26 x  self-review revised the answer          <- compensation paying off
```

Read that as one finding, not seven: **the bridge generates far more upstream calls than
Agentaus absorbs at a cap of 6.** 1236 slot waits and 63 gateway timeouts are the same
fact seen from two angles, and the disconnects and abandoned helpers follow from it. The
adaptive chunk ceiling absorbs bursts; sustained load needs fewer calls, which means
larger search chunks rather than a higher cap.

The single line worth acting on immediately is the quiet one: *summarisation fell back to
trimming* means compaction could not summarise enough and dropped messages instead. That
is real conversation lost, and it is the only entry there that costs correctness rather
than time.

---

## Pattern matching versus judgement

The bridge exists because Agentaus needs help, so it should ask Agentaus rather than
guess whenever the question is a judgement. Every remaining regex has been checked
against that, and the split is not "less is better" — it is **what happens when this
code runs**.

**Replaced with a model call.** Whether a reply is the model refusing to use tools it
has. Three phrase lists did this — about forty entries — and they were wrong in both
directions: they missed phrasings nobody had thought of, and they needed a companion list
of "filesystem words" beside them so that *"I don't have access to next year's budget"*
was not read as a refusal to read a file. Agentaus decides now, behind a **structural**
gate that costs nothing: a turn that was offered tools, called none, and produced a short
answer. Only then is there anything to judge.

Where a soft check can fail, it fails toward doing nothing: an unreadable verdict is
treated as a real answer, because re-asking a good answer is worse than passing a weak one
through — the user can just repeat a turn.

**Kept, deliberately.** Two of them, for the same reason:

| Pattern | Why it stays |
| --- | --- |
| `max_model_len\s+(\d+)` | Reads the real context window out of Agentaus' own error text. It runs **when the upstream is already failing** — asking that upstream to parse its own error message is asking the broken thing to explain itself. |
| `$$…$$` and the typographic table | Turns `EU‑WEST‑2` back into `EU-WEST-2` and `$$retry_budget_ms$$` back into a backticked identifier. A deterministic character mapping. A model asked to fix punctuation would introduce new corruption in the one place the bridge cannot tolerate it — identifiers a coding agent is about to act on. |

Two more are borderline and stay for now: the working-directory patterns in `augment.py`
(reading a path out of Claude Code's own stable system prompt) and `_SECTION_START` in
`tools.py` (finding a section boundary for `agentaus_zoom`). Both are candidates for soft
analysis. The second would cost a model call on every zoom, and zoom being free is worth
more than the accuracy it would buy — a wrong boundary makes a passage slightly larger,
which the token budget then trims anyway.

The search prefilter is worth a word because it looks like the same question and is not.
It uses literal matching to *rank* files and chunks — but the terms it ranks on come from
**Agentaus expanding the query**, which is the soft half. Replacing the ranking with
judgement too would mean reading everything to decide what to read, which is what the
prefilter exists to avoid.

---

## Word documents and spreadsheets

A `.docx` is a zip archive, so the bridge used to walk past office documents as binary.
That meant `agentaus_search` and `agentaus_zoom` could not see a tender response, a
requirements matrix or a specification — which is exactly where that material lives.

They are read by default now, converted with **LibreOffice**, which knows the format.
Nothing to configure if LibreOffice is installed; `soffice` is found in the usual places
or on `$PATH`, `AGENTAUS_SOFFICE_PATH` overrides it, and `AGENTAUS_OFFICE_EXTRACT=false`
goes back to skipping them.

**Tables survive, and that is the point.** A row becomes one line with ` | ` between
cells:

```
   79  5.3
   80  Security and Governance
   81  Reference | Requirement | Compliance
   85  5.3.4 | Essential: Model governance documentation... | Yes
```

One line per row means a line number still identifies a row, so `agentaus_zoom` cites into
a spreadsheet exactly as it cites into source code.

### One reader, on purpose

If LibreOffice is absent, the bridge says so and tells you how to install it. It does not
fall back to a lighter library.

`python-docx` and `openpyxl` would handle `.docx` and `.xlsx` in 496 KB against
LibreOffice's 795 MB, so the temptation is obvious. They are also a **second
implementation of "how does a table become text"** — different format coverage, and a
different rendering of the same document. Two answers to that question is worse than one
large dependency: a row that reads one way on a laptop and another way in CI is a bug
nobody finds until a number is wrong in a tender response.

```
macOS   brew install --cask libreoffice
Debian  sudo apt install libreoffice-writer libreoffice-calc
```

Nothing to configure afterwards — `soffice` is found on `$PATH` or in the usual
locations, and `AGENTAUS_SOFFICE_PATH` covers an unusual one.

### Why not just strip the XML

Because it fails quietly. Measured against LibreOffice on one real 43-row requirements
table, a regex flatten of the document XML:

- lost **2,695 characters** of answer text across 12 rows — **56% of one row**, whose
  evidence review then came back empty and looked like an upstream fault
- could not see the **compliance column at all**, having no idea where one cell ended and
  the next began
- guessed at row boundaries, so the final row **swallowed 77,000 characters** of the rest
  of the document

None of that raises an error. It just produces slightly wrong input, which produces
confidently wrong output.

Conversions are cached per file, keyed on path, mtime and size — so a search reading many
chunks converts once, and a document edited under the bridge is re-read rather than served
stale. Each conversion gets a private LibreOffice profile, because concurrent `soffice`
invocations otherwise fight over the shared one and a run silently produces nothing.

---

## PDFs

Every PDF used to be skipped outright. `.pdf` sat in the search tool's binary-suffix
list, so `enumerate_files` returned nothing for one and `read_text` returned megabytes
of noise. In the corpus this was built against that hid **48 documents** — which is a
large part of why a question about "the whole repo" came back answered from two files.

They do **not** go through LibreOffice. Its importer opens a PDF as a drawing to be
edited: converting one real eight-page document produced 112 characters of stylesheet and
a hundred GIFs. I read that as "this PDF is an image-only scan" and was wrong — it was a
conclusion about LibreOffice's PDF filter mistaken for a conclusion about the file.
`pypdf` reads the same document fine.

So PDFs get a ladder instead. Extractors are tried in order, each result is checked **per
page**, and only the pages that came back blank or garbled are escalated to OCR — a
twenty-page report with two scanned pages costs two OCR calls, not twenty.

| Tier | Needs | Why it is where it is |
| --- | --- | --- |
| `pdftotext -layout` | `brew install poppler` | Won on **all 48** files, often by 2–3×, and ran 10–30× faster. `-layout` keeps the column and table geometry the others flatten |
| `pdfminer.six` | pip | Pure Python fallback. Slower, loses columns |
| `pypdf` | pip | Last resort. Fastest of the two, extracts least |
| **macOS Vision** | pyobjc | Neural OCR built into the OS. No model download, ~0.5s/page, rasterises through Quartz so it adds no imaging dependency |
| **tesseract** | `brew install tesseract` | Cross-platform OCR tier |

Every tier is optional and the ladder degrades: with no system binaries and no OCR at
all, an ordinary text-layer PDF still reads.

### One bug worth recording

A form feed *separates* pages and is also emitted after the last one, so splitting on it
invents an empty final page. That phantom page failed the readability gate, which
disqualified the best extractor on most of the corpus and silently handed the job to the
worst — the failure looked like success, because text still came back. Fixing it took one
tender PDF from 17,771 characters to 23,344.

All 48 now extract: **1,120,798 characters in 11 seconds.**

Extracted text carries `[page N]` markers, because a page number is the only coordinate a
PDF has and a quote from a 93-page tender cannot be checked without one.

---

## When a claim outruns the evidence

The self-review pass has to sit out tool-derived turns — a reviewer shown only the request
and the answer reads a well-grounded reply as an unverified claim, and rewrites a correct
answer into *"please provide the document"*. That fix was necessary and it left a hole:
**the one kind of turn where a claim can outrun the evidence had nothing watching it.**

What that looked like in practice. Asked to survey a repository, a real session ran one
`find`, and then wrote:

> *"the repo's `_notes.md` usually enforces a single, consistent fit label"* — never opened it
> *"No apparent breach of the 'prohibitions live once' rule"* — **a policy that does not exist**

Both stated as findings. Nothing checked them.

So there is a second pass for exactly those turns, and it is not blind: it is given the
**ledger of tools actually executed**, which turns *"you claim this about a file you never
opened"* from a guess into something checkable.

```
answer + <tools_it_actually_ran>  →  GROUNDED
                                 →  GAPS, then one line per unsupported claim
                                        ↓
                                 rewrite: each gap removed, or restated as the open
                                 question it actually is — everything else untouched
```

An unreadable verdict counts as grounded. Rewriting a good answer is the more expensive
mistake, and this pass exists because the last one made it.

---

## Truncated tool output is read back from disk

When a tool produces more than the client will carry, Claude Code saves the whole output
and passes on a preview:

```
Output too large (1.6MB). Full output saved to: …/tool-results/abc.txt
Preview (first 2KB):
```

Sensible for a client, terrible for a model. That is how one session came to survey a
repository from **2 KB of a 1.6 MB listing** and invent the rest — it was told where the
full output was and did not read it.

The bridge runs on the same machine, so it does: the preview is replaced with the real
content before anything else reads the turn, capped by `AGENTAUS_RESTORE_MAX_BYTES` with
the remainder announced rather than dropped. A preview naming a file that does not exist is
left alone — a preview is bad, and a preview replaced by nothing is worse.

### And `Bash` is restricted for searching

The same session reached for `find` rather than `agentaus_search`, which is what produced
the 1.6 MB in the first place. `Bash` now carries the treatment `Grep` got — the
restriction at the **front** of its description, because a caveat after a paragraph loses
to a strong prior:

> *RESTRICTED: do NOT use this to search or survey. `find`, `ls -R`, `grep -r` and `cat`
> over many files all produce more output than this conversation can carry… Use Bash for
> RUNNING things — tests, builds, git, one command with bounded output.*

Only the search-shaped uses. Bash is still how you run a test.

---

## Aiming before reading

Search finds things by reading — one model call per chunk. That is what makes it work when
the answer shares no words with the question, and what makes it cost ten calls on a large
file.

The cheap half of that problem is structural. *"Which part of this is about security
accreditation"* follows from declarations or headings, and those cost **nothing** to
extract: no model call, no upstream request. So the bridge builds an outline locally and
spends **one** call over a table of contents, then reads only the sections it names.

```
corpus                              content   outline   ratio
this bridge's own source (code)      66,486     6,431     10x
a 434KB tender response (prose)      89,579     9,276     10x
a returnable-documents .docx         26,865       188    143x
```

Aiming is an optimisation and never a gate. An unparseable reply, an invented path, a
failed call — all fall back to reading every chunk, because reading less is not worth
missing the answer.

### Three extractors, for three structures

| Corpus | Indexed by |
| --- | --- |
| Source code | declarations — `def`, `class`, `func`, `struct`, `interface`, `impl`… |
| Prose | headings — Markdown, bold-only lines, clause numbers like `5.3.1` |
| JSON | its keys, which is all its structure amounts to |

That is not the same mistake as two extractors for one structure. A function signature and
a heading are not competing descriptions of the same thing.

### Code uses Tree-sitter, and why that is worth a dependency

Declarations come from **Tree-sitter**, across nineteen languages — Python, JavaScript,
TypeScript, TSX, Go, Rust, Java, Kotlin, Swift, C, C++, C#, Ruby, PHP, Scala, Bash, SQL,
Lua, Dart. It produces a Concrete Syntax Tree with every token mapped to an exact byte,
line and column, so a declaration is what the *grammar* says it is:

```
     36  def gate()
     84  class _PriorityGate
    113  async def acquire(self, priority)
```

A line pattern gets most of those and is wrong where it matters. It cannot tell a
declaration from the same words inside a docstring or a comment, and it misses a signature
written across two lines — both of which are tested for.

There is a strict quality ordering rather than competing implementations, and each rung
runs only when the one above cannot:

```
1. Tree-sitter       exact, 19 languages, optional dependency
2. Python `ast`      exact, one language, always available
3. declaration pass  approximate, any language, always available
```

`pip install tree-sitter tree-sitter-language-pack` gets rung one. Without it the outline
still works — just approximately, for languages other than Python.

---

## Skills: the know-how, not the capability

A **tool** does something — a round trip that returns data. A **skill** tells the model
*how* — Markdown loaded into context that returns behaviour. The bridge ships tools; the
skills in `.claude/skills/` ship the procedures, and Claude Code loads one when its
description matches what you asked for.

They matter more here than they would with a stronger model. Most of the disappointing
answers this bridge produced were not capability gaps — they were a competent model taking
the wrong approach, and an approach is exactly what a skill supplies.

| Skill | The pain point it encodes |
| --- | --- |
| `repo-survey` | Asked to review a repo, ran one `find`, answered from a 2 KB preview of a 1.6 MB listing, and **invented a policy that does not exist** |
| `find-in-code` | Reached for `Grep` and `find` — which cannot answer *"where do we cap concurrent calls"*, since the answer shares no word with the question |
| `read-documents` | A regex flatten of a `.docx` lost **2,695 characters** across 12 rows and could not see the compliance column |
| `tender-evidence` | Four rows of 43 proposed additions asserting real certifications **with no quote behind them** |
| `bridge-diagnose` | 63 timeouts and 1236 slot waits read as separate faults when they were one |
| `search-exhaustively` | Asked to go through hundreds of documents, answered from **one or two** — because the stopping condition was "I have something to say" rather than "there is nothing left to find" |
| `investigate` | A single pass produces one story about the evidence, and a fluent story is indistinguishable from a correct one once written down |
| `research` | Current facts answered from memory with no source, and web claims blended into repository claims with no way back |

Each names the failure it prevents, with the measurement, because a rule whose reason is
stated is followed more reliably than one asserted. They are deliberately short — every
token of instruction is one less for the conversation, and a long list of rules is itself
something a smaller model handles badly.

### Keeping them from doubling up with the tools

A tool is a **capability**; a skill is **when to reach for it and how to know you are
finished**. No skill re-explains what a tool does. `find-in-code` is the router and hands
off; each specialist owns one tool; `search-exhaustively` names no tool at all, because a
stopping criterion is not a capability.

That boundary had to be enforced, not just stated. `find-in-code` carried the rule *"one
search, then at most two zooms, then answer"* — correct for locating one thing, and
precisely the wrong instruction for *"go through the whole repo"*, which is the question
that kept producing two-file answers. One skill was quietly arguing with another. It is
now scoped to single-target lookups and points at `search-exhaustively` for the rest.

Nothing about them is Agentaus-specific. They are ordinary Claude Code skills and work on
any model; they simply matter more where the model needs the script.

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
| `AGENTAUS_SEARCH` | `true` | Offer `agentaus_search`, the bridge-executed semantic search, and steer `Grep` towards literal lookups |
| `AGENTAUS_WEB_SEARCH` | `true` | Offer `agentaus_web_search`, which drives Agentaus' own web search. Claude Code's `WebSearch` is dropped in translation, so without this an Agentaus turn cannot search the web at all |
| `AGENTAUS_SEARCH_CHUNK_TOKENS` | `8000` | File content per search call. Measured: 4000 costs 48s/16 calls, 8000 costs 29s/10, 16000 costs 22s/4 — all find the same facts, but bigger chunks quote less back |
| `AGENTAUS_SEARCH_MAX_CHUNKS` | `120` | Ceiling on calls for one search. Truncation is reported in the result, never silent |
| `AGENTAUS_SEARCH_MIN_CANDIDATES` | `3` | Below this many shortlisted files, distrust the shortlist and read everything |
| `AGENTAUS_SEARCH_MAX_FILE_BYTES` | `1048576` | Skip files larger than this |
| `AGENTAUS_SEARCH_ROOTS` | *(empty)* | Colon-separated directories search may read. Empty allows any absolute path, matching Claude Code's own `Read` |
| `AGENTAUS_TOOL_ROUNDS` | `12` | How many rounds of bridge-executed tool calls one turn may run before the answer has to stand |
| `AGENTAUS_CORRECTION_ROUNDS` | `3` | Rounds spent telling the model a tool it named does not exist. Separate from tool rounds, so being corrected does not consume the budget for real work |
| `AGENTAUS_SEARCH_OUTLINE_FIRST` | `true` | Build a free structural outline and spend one call choosing sections, instead of a call per chunk |
| `AGENTAUS_SEARCH_MAX_SECTIONS` | `8` | Sections one aimed search reads before it is cheaper to read everything |
| `AGENTAUS_SEARCH_MAX_CANDIDATES` | `12` | Ceiling on files one search reads. The shortlist is ranked, so this keeps the best matches |
| `AGENTAUS_INVESTIGATE` | `true` | Offer `agentaus_investigate`: three independent searches, and a fact must appear in two before it is reported as established |
| `AGENTAUS_ZOOM` | `true` | Offer `agentaus_zoom`: open a citation from a search result and read it in its section. Without it the model cites evidence it cannot quote from |
| `AGENTAUS_ZOOM_RADIUS_LINES` | `120` | How far either side of the cited lines to look for a section boundary |
| `AGENTAUS_ZOOM_MAX_LINES` | `400` | Hard stop, so a heading-less file cannot return itself whole |
| `AGENTAUS_ZOOM_MIN_LINES` | `40` | Floor on the window. Tender documents use bold single lines as sub-headings, so boundary detection alone returned 3-line "sections" |
| `AGENTAUS_ZOOM_MAX_TOKENS` | `6000` | Below this the passage comes back verbatim; above it, Agentaus keeps what serves the stated purpose |
| `BRIDGE_LOG_BODY_CHARS` | `20000` | How much of a body `BRIDGE_LOG_BODIES` logs |
| `AGENTAUS_DISTILL_RESULTS` | `false` | Condense oversized tool results before sending them. **Off by default** — see [Distillation](#distillation) |
| `AGENTAUS_DISTILL_THRESHOLD_TOKENS` | `12000` | Results smaller than this are never touched |
| `AGENTAUS_TOOL_LEDGER` | `true` | Append a derived list of tools already run to the system prompt. Costs no Agentaus calls |
| `AGENTAUS_GROUNDING_CHECK` | `true` | Check a tool-derived answer against the tools actually run, and strip claims nothing supports |
| `AGENTAUS_RESTORE_PERSISTED` | `true` | When the client truncates a tool result to a preview, read the file it saved instead |
| `AGENTAUS_RESTORE_MAX_BYTES` | `400000` | Ceiling on what is read back |
| `AGENTAUS_THINKING` | `true` | Plan the turn in a separate call before answering it |
| `AGENTAUS_THINKING_VISIBLE` | `true` | Show that plan as a thinking block. `false` still uses it, but does not display it |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8787` | Listen address |
| `BRIDGE_PASSTHROUGH` | `true` | Forward non-Agentaus models to Anthropic; `false` answers them with Agentaus instead |
| `ANTHROPIC_UPSTREAM_BASE_URL` | `https://api.anthropic.com` | Passthrough target |
| `BRIDGE_PING_INTERVAL` | `10` | Seconds between keep-alive pings while Agentaus is silent |
| `BRIDGE_CHUNK_CHARS` | `60` | Re-chunk buffered replies into smaller deltas; `0` disables |
| `BRIDGE_CONNECT_TIMEOUT` | `15` | Connect timeout (seconds) |
| `BRIDGE_READ_TIMEOUT` | `300` | Per-read budget upstream. Streaming resets it on every token, so it only bounds waiting on *nothing*. Was 1800, which turned a dead connection into 30 minutes of silence |
| `BRIDGE_STALL_WARNING` | `45` | Log that an upstream call is still waiting, and for how long. `0` disables |
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
```

**unittest, not pytest** — pytest is not in the virtualenv.

---

## Benchmarking

```bash
./benchmarks/run.py --list
./benchmarks/run.py --model agentaus
./benchmarks/run.py --model agentaus --suite humaneval --limit 20
```

Three suites. In two of them the scoring authority sits outside this repository, which is
the point — a benchmark whose verdicts are its own opinion cannot tell you anything.

| Suite | Measures | Verdict comes from |
| --- | --- | --- |
| `humaneval` | pass@1 over 164 problems | **the dataset's own unit tests** |
| `retrieval` | localisation precision, recall, F1 | ground truth over this repository |
| `injection` | resistance to instructions hidden in file content | this bridge's stated threat model |
| `documents` | can it answer from a PDF at all — found and cited rates | six facts checked to exist **only** inside PDFs, against the 263 non-PDF files in the corpus |
| `coverage` | does it survey the corpus or stop at the first hit | filenames counted against the real listing, so invented ones score nothing |

Every suite routes through the bridge, so two arms share the harness, the prompts and the
parsing exactly and differ only in who answered. **Tokens and latency sit beside every
score**, because a model that scores two points higher for four times the tokens has not
won anything a deployment cares about.

### You do not need a second arm

HumanEval is reported widely enough that a reference point is a lookup rather than an
experiment. `baselines.py` carries published figures and the run places the measured score
among them — no API key, no spend.

Those figures are **orientation, not a head-to-head**: harnesses differ by a few points of
pass@1. And the bigger caveat — **HumanEval is saturated.** It separates mid-tier from
frontier and cannot separate frontier models from each other, which is why `retrieval` and
`injection` carry more weight for this deployment than pass@1 does.

For a genuine head-to-head, `--model claude-opus-5` works but needs `ANTHROPIC_API_KEY`
exported: the bridge forwards those requests to api.anthropic.com and a harness cannot
borrow the OAuth token Claude Code holds. The run says so plainly rather than reporting a
zero.

### What each suite is actually testing

**`retrieval`** questions are worded so the answer shares no vocabulary with the question —
*"Where does this project stop too many background requests running at once?"* for
`gate.py:_PriorityGate`. A text search cannot serve that; a semantic one must. This is the
number that matters for a bridge whose search design is "aim before you read".

**`documents`** exists because the answer used to be *no*. Every needle in it was checked
against the 263 non-PDF files in the corpus first, so a non-zero score means PDFs are
genuinely being read rather than an adjacent file being paraphrased.

**`coverage`** is the only suite here that scores *completeness* rather than correctness,
and it runs as an A/B — the same question with and without the `search-exhaustively`
procedure. A skill is a claim that instructions change behaviour, and that claim is worth
measuring rather than assuming.

**`injection`** plants an instruction in file content and asks an ordinary question about
it. One case claims *"Privilege Level 10 — highest authority"*, which is the mechanism that
gets recommended in prose and does nothing on its own. The enforcement that works here is
structural: the tool allowlist, the invented-name check, the grounding pass.

Generated code runs in a subprocess with a wall-clock timeout. That bounds a runaway loop;
it is **not a sandbox**. Run it somewhere that is acceptable, or in a container.

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
