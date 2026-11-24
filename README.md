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
| 4 | **Python 3.11 or newer** | `python3 -V`. The Python that ships with macOS is new enough. |
| 5 | **An Agentaus API key** | Sign up at [agentaus.com.au](https://agentaus.com.au), then create a key in the API dashboard. |

### The CLI and the extension are not the same thing

Claude Code comes in two forms, and this matters later:

- **The VS Code extension** — what most people install. There is **no `claude` command** on
  your PATH with an extension-only install.
- **The standalone `claude` CLI** — a separate install.

Check which you have with `which claude`. If it prints nothing you have the extension only,
which is fine — but `scripts/claude-agentaus.sh` (and `.ps1` on Windows) cannot work for
you, because they `exec` `claude`. Use a settings file instead (Option 1 or 2 under
[Pointing Claude Code at the bridge](#pointing-claude-code-at-the-bridge)).

---

## Quick start

### Requirements

Only Python and the packages in `requirements.txt` are required; the install scripts put
those in a virtualenv and need nothing else on the machine. LibreOffice, poppler and
tesseract are optional document readers: without them the bridge starts, logs a warning
naming each missing reader, and walks past the documents it cannot read.
`INSTALL_DOCUMENT_READERS=1` (or `$env:INSTALL_DOCUMENT_READERS = "1"` in PowerShell)
installs them as well.

| Requirement | Reads | macOS | Debian / Ubuntu | Fedora (dnf) | Windows (winget) |
| --- | --- | --- | --- | --- | --- |
| Python 3.11+ | - | `brew install python` | `sudo apt install python3 python3-venv` | `sudo dnf install python3` | python.org install, or the Microsoft Store |
| **LibreOffice** | `.docx`, `.xlsx`, `.pptx`, tables intact | `brew install --cask libreoffice` | `sudo apt install libreoffice-writer libreoffice-calc libreoffice-impress` | `sudo dnf install libreoffice` | `winget install -e --id TheDocumentFoundation.LibreOffice` |
| **poppler** (`pdftotext`, `pdftoppm`) | PDF text with its layout; scanned pages for OCR | `brew install poppler` | `sudo apt install poppler-utils` | `sudo dnf install poppler-utils` | `winget install -e --id oschwartz10612.Poppler` |
| **tesseract** | Scanned PDF pages | `brew install tesseract` | `sudo apt install tesseract-ocr` | `sudo dnf install tesseract` | `winget install -e --id UB-Mannheim.TesseractOCR` |
| Python packages in `requirements.txt` | PDF fallbacks (`pdfminer.six`, `pypdf`), the code outline (`tree-sitter`), macOS Vision OCR (`pyobjc`) | `pip install -r requirements.txt` | same | same | same (via install script) |

### macOS, Debian / Ubuntu, Fedora (bash)

```bash
git clone https://github.com/chengyu2/agentaus_connector_for_claude_code.git
cd agentaus_connector_for_claude_code
./scripts/install.sh                           # virtualenv, dependencies, .env scaffold
# Optionally install the document readers too:
#   INSTALL_DOCUMENT_READERS=1 ./scripts/install.sh
$EDITOR .env                                   # paste your AGENTAUS_API_KEY
./.venv/bin/python -m agentaus_bridge --check   # verify the key with a live call
./scripts/start-bridge.sh                      # start the bridge (leave this terminal open)
```

A successful `--check` prints `OK - Agentaus replied` with a token count. If it prints
`HTTP 401`, the key is wrong or expired — fix that before going further, because nothing
downstream can work without it.

The install script detects your package manager automatically: `brew` on macOS, `apt-get`
on Debian/Ubuntu, and `dnf` on Fedora/RHEL/Alma/Rocky.

### Windows (PowerShell)

Open PowerShell in the repo folder (or `cd` into it). If the script is blocked by the
execution policy, prefix the command with `powershell -ExecutionPolicy Bypass -File`.

```powershell
git clone https://github.com/chengyu2/agentaus_connector_for_claude_code.git
cd agentaus_connector_for_claude_code
.\scripts\install.ps1                          # virtualenv, dependencies, .env scaffold
# Optionally install the document readers too:
#   $env:INSTALL_DOCUMENT_READERS = "1"; .\scripts\install.ps1
notepad .env                                   # paste your AGENTAUS_API_KEY
.\\.venv\\Scripts\\python.exe -m agentaus_bridge --check
.\scripts\start-bridge.ps1                      # start the bridge (leave this window open)
```

The PowerShell install locates Python automatically (`python`, or the `py -3` launcher if
only the Microsoft Store install is present), and installs the document readers through
`winget` when enabled.

Then **tell Claude Code the bridge exists.** This is a separate step and it is where almost
everyone gets stuck: the settings file has to go in the directory you actually *work* in,
which is usually **not** this repo. Read
[Pointing Claude Code at the bridge](#pointing-claude-code-at-the-bridge) before editing
anything, then restart Claude Code and run `/model` — **Agentaus (Trellis Data)** appears
below Opus, Sonnet and Haiku.

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
| Extension | `system_prompt_overwrite: true` replaces the built-in Agentaus persona - **with the request's own system message, so it needs one** |
| Ignored | `reasoning_effort`, `reasoning.effort`: accepted, no effect (~120 hidden reasoning tokens either way) |

Two behaviours drove design decisions in the bridge:

1. **The default persona costs ~2,200 input tokens on every request** and instructs the
   model to behave as a general assistant, which fights Claude Code's agent prompt.
   The bridge always sends `system_prompt_overwrite: true` so Claude Code's system
   prompt is the only one in play. (`AGENTAUS_SYSTEM_PROMPT_OVERWRITE=false` to revert.)
   The flag overwrites the persona *with the request's system message*, so a request
   without one keeps the persona: "Say hi" measured 2,442 input tokens bare and 207 with
   a one-line system message, and offered a tool while streaming, the bare form refused
   to use it. Every bridge helper call now carries a short system message for this reason.
2. **Agentaus streams live** - measured, ~13-token pieces every ~100 ms, first token in
   about a second. (An earlier version of the API buffered whole answers, which is why
   the bridge emits SSE `ping` events every 10 seconds while it waits: Claude Code aborts
   a stream that goes silent for 300 seconds.)

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
[background agent](#running-it-in-the-background). Without that, any time the bridge is
not running, *all* of your Claude Code sessions fail — Claude models included, since they
reach Anthropic through the same base URL. See [the trade-off this creates](#the-trade-off-this-creates).

### Option 2 — one specific project

The same `env` block, but in `<that project>/.claude/settings.json` — meaning the project
you work in, **not** this repo.

Use this when you want Agentaus in one place and untouched Claude everywhere else. It also
contains the blast radius: a stopped bridge breaks only that project.

If the project is a shared git repo, put it in `.claude/settings.local.json` instead —
same effect, but it is gitignored by convention, so you are not committing a `localhost`
endpoint that only resolves on your machine.

### Option 3 — the launcher script (standalone CLI only)

- Linux / macOS: `./scripts/claude-agentaus.sh --model agentaus`
- Windows: `.\scripts\claude-agentaus.ps1 --model agentaus`

Each sets every variable and execs `claude`, forwarding its arguments, so
`--model agentaus` starts directly on Agentaus.

**This needs the standalone `claude` CLI on your PATH.** With a VS Code extension-only
install there is no `claude` binary and the script dies with `command not found`. Check
with `which claude` first.

### Option 4 — export by hand, for one shell

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_CUSTOM_MODEL_OPTION="agentaus"
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Agentaus (Trellis Data)"
export ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="Sovereign Australian model via the local bridge"
claude
```

For the equivalent one-off in PowerShell:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"
$env:ANTHROPIC_CUSTOM_MODEL_OPTION = "agentaus"
claude
```

Lasts as long as the shell. Useful for a one-off test, not for daily use.

### Whichever you choose: restart afterwards

`ANTHROPIC_CUSTOM_MODEL_OPTION` is the variable that adds the row to the `/model` picker,
and Claude Code reads environment variables **once at startup**. After editing settings,
restart the session — in VS Code, reload the window — or the row will not appear.

Then run `/model`: **Agentaus (Trellis Data)** sits below Opus, Sonnet and Haiku. Claude
Code skips validation on that model id, so any string the bridge understands works.

### Keeping the picker current as Anthropic adds and retires models

Nothing here names a Claude model, so nothing goes stale. The picker's Opus, Sonnet, Haiku
and Fable rows are aliases that always mean Anthropic's newest model of each family, and
the bridge forwards every Claude request to Anthropic unchanged.

For explicit, versioned rows as well, add `"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"`
to the same `env` block (this repo's own `.claude/settings.json` does). At startup Claude
Code then asks the bridge for `GET /v1/models`, and the bridge answers with **Anthropic's own
list, fetched live with your credentials**, plus Agentaus - so the picker follows Anthropic
with no change to the bridge. Its details:

- Claude Code keeps only ids containing `claude` or `anthropic`, so Agentaus still comes from
  `ANTHROPIC_CUSTOM_MODEL_OPTION`; keep that variable.
- Claude Code sends credentials on that request only from `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN` or an `apiKeyHelper`. With a claude.ai subscription login and none
  of those, the bridge has nothing to ask Anthropic with and serves only Agentaus, so the
  picker shows its usual alias rows - nothing breaks.
- Claude Code waits 3 s; the bridge gives Anthropic 2.5 s and otherwise serves the last list
  it fetched, and Claude Code falls back to its own cache or built-in list on any failure.

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

### Subagents follow their session

A subagent started from an Agentaus session did **not** run on Agentaus, out of the box.
Claude Code cannot "inherit" a custom model id for a subagent, so it asks for its default
Claude model instead. Measured on 2.1.281: the main loop on `agentaus`, the Explore
subagent it started on `claude-opus-5-5`, forwarded to Anthropic. The subagent's work
and your Claude quota went to the model you had switched away from, and nothing said so.

Claude Code marks a subagent's requests - an `x-claude-code-agent-id` header, and
`cc_is_subagent=true` in the billing line of its system prompt - and every request carries
`x-claude-code-session-id`, shared by a session and its subagents. So the bridge
remembers which model each session's main loop used last and sends that session's
subagents the same way:

| Session's main loop | Its subagents go to |
| --- | --- |
| `/model agentaus` | Agentaus |
| `/model opus` (or any Claude model) | Anthropic, exactly as before |

Switching mid-session moves the subagents with you, so the toggle is untouched. Only a
main-loop turn (tools offered, no agent id) records the session's model: a title or summary
call cannot flip it. The log says `route=agentaus (subagent of an Agentaus session)` when
it applies. Turn it off with `AGENTAUS_SUBAGENTS_FOLLOW_SESSION=false`.

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
port number agreed an hour ago, why an approach was rejected, a constraint stated once.
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
| `scripts/claude-agentaus.sh` / `.ps1` | `export BRIDGE_URL="http://127.0.0.1:9100"` before running it | Only if you use the launcher; it reads `BRIDGE_URL` and needs no edit |
| `tests/smoke_test.py` | `--url http://127.0.0.1:9100` | Running the end-to-end checks |
| Command line | `python -m agentaus_bridge --port 9100` | Overrides `.env` for one run, handy for testing |
| launchd / systemd plist or unit | nothing — they read `.env` | No edit needed |

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

## Nothing one turn does may freeze the others

The bridge is one process on one event loop, serving every Claude Code request — both
providers, every open window. So anything that blocks that loop blocks all of it: the
pings for the turn doing the work, every other Agentaus turn, and every Claude turn going
through passthrough. From VS Code that looks exactly like Claude Code hanging.

It happened. One `agentaus_search` was pointed at a tree that reached into application
bundles, and converted everything inside them — LibreOffice's own sample spreadsheets,
installer RTFs, two licence agreements through OCR. Every conversion ran synchronously on
the loop. For **90 minutes the bridge received nothing**; a `claude-opus-5` request that
arrived at the start of it was never forwarded.

Four things stop that now:

| Guard | What it does |
| --- | --- |
| **Worker threads** | Every walk, file read, document conversion and OCR pass runs in a thread. Measured with real LibreOffice on seven files: the loop was frozen for 10.2s before, and never more than 0.1s after |
| **A tool deadline** | `AGENTAUS_TOOL_TIMEOUT` (480s; the slowest real tool took 256s). The model gets a result telling it to narrow the path. The deadline also reaches the worker thread, which cancellation cannot, so an abandoned search stops converting files |
| **A bounded walk** | `.app`, `.framework` and other macOS bundles are never entered, nor `~/Library`, and one walk returns at most `AGENTAUS_SEARCH_MAX_FILES` files — said so in the result |
| **A turn deadline** | `BRIDGE_TURN_TIMEOUT` (900s; the slowest real turn took 277s). Pings exist to defeat Claude Code's idle timeout, which means nothing else could end a wedged turn. Past the deadline the stream ends with an error the user can retry |

Claude passthrough also has its own connection pool now, so a burst of Agentaus helper
calls cannot take every connection while a Claude turn waits for one. And a Claude stream
that dies mid-response ends with an SSE error event instead of an ASGI traceback and a
dropped socket.

---

## What Claude Code actually sends, and what the bridge does with it

Captured from Claude Code 2.1.278 - the native binary the VS Code extension ships - and
all of it invisible from the settings file:

| What arrives | Why it hurt Agentaus | What the bridge does |
| --- | --- | --- |
| **26 tools, 120 KB of schemas** (~30,000 tokens a call). `Artifact` alone is 34 KB | A quarter of the window gone before the conversation starts, and 26 names to choose from - where the invented and misspelt calls come from | **Tool focus**: Claude Code's non-coding harness tools are held back unless the user names them or the conversation already used them - 13 KB. A deny-list, so custom and MCP tools are never hidden. `AGENTAUS_TOOL_FOCUS=false` sends everything |
| The environment block - **including the working directory** - as a `system`-role entry *inside `messages`*, and connector instructions the same way | Forwarded as-is it became the *last* message, after the user's request, and Agentaus answered it: a finished R task replied to with "I have not read the Claude Docs workflow you shared" | Every `system`-role entry joins the system prompt, so the turn ends on what the user said. The working-directory lookup reads them too - it had silently been finding nothing |
| `<system-reminder>` blocks beside what the user typed: account email, commit attribution, which tools loaded | Handed to the planner, reviewer and turn judge as part of "the request" | Stripped wherever the bridge reads the request |
| The commit attribution asks an unknown model - `agentaus` included - for `Co-Authored-By: Claude Code <noreply@anthropic.com>` (2.1.293) | Every commit an Agentaus session made credited Claude on GitHub | Swapped for `AGENTAUS_COAUTHOR` on Agentaus turns. See [Commit credit](#commit-credit) |
| After every turn, **utility calls** with no tools: the end-of-turn state classifier (done / working / blocked / failed, which drives the "come back" notification) and session titles | The self-review pass rewrote the classifier's one-word verdict into a 4,322-character essay, twice - **39 seconds added to every Agentaus turn**, and a garbled notification state | Review and grounding run on agent turns only - requests that offer tools. `AGENTAUS_REVIEW_TOOLLESS=true` restores the old behaviour for a bare-prompt harness |

## Live tokens

Agentaus streams, but the bridge held every answer back until its checks had run -
review, grounding, the syntax check, tool-call repair - because an answer already on the
screen cannot be revised. So the client's token counter sat at zero and the answer landed
at once. Measured through the real Claude Code binary on one 400-word answer: no visible
characters for 57 seconds, then 4,672 at once.

Now the response starts immediately, and everything Agentaus writes streams into a
thinking block as it arrives: the plan, then the draft answer, with a line whenever the
bridge is doing something between upstream calls (`running agentaus_search`,
`compacting the earlier conversation`, `checking the answer`). The checked answer follows
as ordinary text. Display only - nothing replays thinking blocks. Same prompt afterwards:
characters visible from 5.7 seconds, growing ~450 every 2 seconds.
`AGENTAUS_LIVE_DRAFT=false` goes back to holding everything.

## Markdown or tags

Every prompt the bridge writes for Agentaus - its operating notes, the plan, review,
grounding and turn-judge passes, search and compaction calls, and the framing around each
tool result - is Markdown by default: headings, lists, and fenced blocks.
`AGENTAUS_PROMPT_STYLE=xml` restores the tagged layout byte for byte.

The two are one source. Templates are written once, in the tagged form, and rendered at
`.format()` time by `prompt_style.py`, so the switch cannot drift into two diverging copies
of each prompt. Only the bridge's own words are restyled: anything substituted into a
prompt - a request, an answer under review, a file excerpt, a tool result - arrives
verbatim inside a fence one tilde longer than any run of tildes in it, so nothing inside
can close its block or read as structure.

## Tool calls Agentaus spells wrong

The commonest Agentaus failures on the benchmark were not wrong decisions but wrong
spellings of right ones, and each cost a correction round - where a smaller model loses
the thread. They are repaired now when the meaning is unambiguous (`repair.py`):

- names: `agentaus_read`, `read_file`, `functions.Write`, `run_command`; `Python` becomes
  `Bash` running the same code through `python3`
- fields: `cmd` / `script` / `code` fill a missing `command`, `path` / `filename` a
  missing `file_path`, `text` / `contents` a missing `content`
- a relative `file_path` becomes absolute, which Claude Code's file tools require

A repair only ever targets a tool that was offered, and never overwrites a field the call
did set. Everything else still goes back to the model as a correction.

---

## Word documents and spreadsheets

A `.docx` is a zip archive, so the bridge used to walk past office documents as binary.
That meant `agentaus_search` and `agentaus_zoom` could not see a tender response, a
requirements matrix or a specification — which is exactly where that material lives.

They are read by default now, converted with **LibreOffice**, which knows the format.
`soffice` is found in the usual places or on `$PATH`, `AGENTAUS_SOFFICE_PATH` overrides
it, and `AGENTAUS_OFFICE_EXTRACT=false` goes back to skipping them.

**Tables survive, and that is the point.** A row becomes one line with ` | ` between
cells, so a line number still identifies a row and `agentaus_zoom` cites into a
spreadsheet exactly as it cites into source code:

```
   79  5.3
   80  Security and Governance
   81  Reference | Requirement | Compliance
   85  5.3.4 | Essential: Model governance documentation... | Yes
```

### One reader, on purpose

If LibreOffice is absent, the bridge refuses to start and tells you how to install it. It
does not fall back to a lighter library. `python-docx` and `openpyxl` would handle
`.docx` and `.xlsx` in 496 KB against LibreOffice's 795 MB, but they are a **second
implementation of "how does a table become text"** — different format coverage and a
different rendering of the same document. A row that reads one way on a laptop and
another way in CI is a bug nobody finds until a number is wrong in a tender response.

```
macOS   brew install --cask libreoffice
Debian  sudo apt install libreoffice-writer libreoffice-calc
Fedora  sudo dnf install libreoffice
Windows winget install -e --id TheDocumentFoundation.LibreOffice
```

Conversions are cached per file, keyed on path, mtime and size — so a search reading many
chunks converts once, and a document edited under the bridge is re-read rather than
served stale. Each conversion gets a private LibreOffice profile, because concurrent
`soffice` invocations otherwise fight over the shared one and a run silently produces
nothing.

---

## PDFs

Every PDF used to be skipped outright. `.pdf` sat in the search tool's binary-suffix
list, so `enumerate_files` returned nothing for one and `read_text` returned megabytes
of noise. In the corpus this was built against that hid **48 documents** — which is a
large part of why a question about "the whole repo" came back answered from two files.

They do **not** go through LibreOffice, whose PDF filter opens a PDF as a drawing to be
edited. PDFs get a ladder instead. Extractors are tried in order, each result is checked
**per page**, and only the pages that came back blank or garbled are escalated to OCR — a
twenty-page report with two scanned pages costs two OCR calls, not twenty.

| Tier | Needs | Why it is where it is |
| --- | --- | --- |
| `pdftotext -layout` | poppler | Won on **all 48** files, often by 2–3×, and ran 10–30× faster. `-layout` keeps the column and table geometry the others flatten |
| `pdfminer.six` | pip | Pure Python fallback. Slower, loses columns |
| `pypdf` | pip | Last resort. Fastest of the two, extracts least |
| **macOS Vision** | pyobjc | Neural OCR built into the OS. No model download, ~0.5s/page |
| **tesseract** | tesseract | Cross-platform OCR tier |

All 48 now extract: **1,120,798 characters in 11 seconds.** Extracted text carries
`[page N]` markers, because a page number is the only coordinate a PDF has and a quote
from a 93-page tender cannot be checked without one.

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
| `search-exhaustively` | Asked to go through hundreds of documents, answered from **one or two** — because the stopping condition was "I have something to say" rather than "there is nothing left to find" |
| `investigate` | A single pass produces one story about the evidence, and a fluent story is indistinguishable from a correct one once written down |
| `research` | Current facts answered from memory with no source, and web claims blended into repository claims with no way back |

Each names the failure it prevents, with the measurement, because a rule whose reason is
stated is followed more reliably than one asserted. They are deliberately short — every
token of instruction is one less for the conversation.

A loaded skill needed one fix worth knowing. Claude Code's tool result says only
*"Launching skill: read-documents"* and the procedure follows as a plain message.
Agentaus passed a `.docx` path as the skill's argument, expected the document back, got
instructions, did not follow them, and loaded the same skill three more times. The bridge
now heads a loaded skill *"Procedure to follow now"* and says it is instructions, not a
result; a repeat load is told it is already in the conversation.

---

## Commit credit

GitHub credits a co-author by the email in the commit's `Co-Authored-By:` trailer, matched
to the account that email is verified on. That is how Claude shows up as a contributor:
`noreply@anthropic.com` is verified on Anthropic's `claude` account. Agentaus has its own
account, [Agentaus-by-Trellis-Data](https://github.com/Agentaus-by-Trellis-Data), with
`theteam@trellisdata.com`.

Claude Code writes the trailer it wants into a `<system-reminder>`, and for a model it does
not know it asks for `Co-Authored-By: Claude Code <noreply@anthropic.com>`. Two ways to
change that:

| Where | Credits Agentaus on | Leaves Claude's commits to Claude |
| --- | --- | --- |
| The bridge (`AGENTAUS_COAUTHOR`, on by default) | Agentaus turns, in every project | Yes - Claude turns pass through untouched |
| `"attribution": {"commit": "Co-Authored-By: Agentaus <theteam@trellisdata.com>"}` in `.claude/settings.json` | Every turn in that project | No - Claude Code asks every model for that line |

This repository sets both, so every commit made here credits Agentaus. The pull request
footer (`Generated with Claude Code`) is left alone, since the tool is still Claude Code.

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
| `AGENTAUS_MODEL_MARKERS` | `agentaus` | Comma-separated substrings that route to Agentaus |
| `AGENTAUS_FORCE_ALL` | `false` | Ignore the model id; send everything to Agentaus |
| `AGENTAUS_MAX_INPUT_TOKENS` | `131072` | Agentaus' context window. Defaults in code and self-corrects from Agentaus' own error messages; `0` disables the check |
| `AGENTAUS_AUTO_TRIM` | `true` | Compact the older conversation into a summary rather than failing the turn |
| `AGENTAUS_MAX_CONCURRENCY` | `8` | One global cap on every Agentaus call the bridge makes on its own initiative. Your own turn is never queued behind it |
| `AGENTAUS_OFFICE_EXTRACT` | `true` | Read `.docx`, `.xlsx`, `.pptx` via LibreOffice, tables intact |
| `AGENTAUS_SOFFICE_PATH` | *(auto)* | Path to `soffice`. Empty searches the usual locations, then `$PATH` |
| `AGENTAUS_PDF_EXTRACT` | `true` | Read PDFs via the extractor ladder |
| `AGENTAUS_PDF_OCR` | `true` | OCR the pages that came back blank or garbled. macOS Vision first, then tesseract |
| `AGENTAUS_SEARCH` | `true` | Offer `agentaus_search`, the bridge-executed semantic search |
| `AGENTAUS_WEB_SEARCH` | `true` | Offer `agentaus_web_search`, which drives Agentaus' own web search |
| `AGENTAUS_SEARCH_ROOTS` | *(empty)* | Colon-separated directories search may read. Empty allows any absolute path |
| `AGENTAUS_SEARCH_MAX_FILES` | `5000` | Ceiling on files one search or inventory walk returns |
| `AGENTAUS_TOOL_TIMEOUT` | `480` | Wall-clock ceiling on one bridge tool call. `0` disables |
| `AGENTAUS_TOOL_ROUNDS` | `12` | Rounds of bridge-executed tool calls one turn may run before the answer has to stand |
| `AGENTAUS_INVESTIGATE` | `true` | Three independent searches; a fact must appear in two to be reported as established |
| `AGENTAUS_ZOOM` | `true` | Open a citation from a search result and read it in its section |
| `AGENTAUS_DISTILL_RESULTS` | `false` | Condense oversized tool results before sending them. **Off by default** |
| `AGENTAUS_TOOL_LEDGER` | `true` | Append a list of tools already run to the system prompt |
| `AGENTAUS_GROUNDING_CHECK` | `true` | Check a tool-derived answer against the tools actually run |
| `AGENTAUS_THINKING` | `true` | Plan the turn in a separate call before answering it |
| `AGENTAUS_THINKING_VISIBLE` | `true` | Show that plan as a thinking block |
| `AGENTAUS_SKILL_ROUTING` | `true` | Restore project skill descriptions and route skills/subagents by "if this, use that" |
| `AGENTAUS_SKILL_LIBRARY` | `true` | Offer the bridge's own skill library to Agentaus in every project |
| `AGENTAUS_SUBAGENTS_FOLLOW_SESSION` | `true` | A subagent runs on the model its session's main loop is on |
| `AGENTAUS_COAUTHOR` | `Agentaus <theteam@trellisdata.com>` | The `Co-Authored-By:` line commits on Agentaus turns end with. See [Commit credit](#commit-credit) |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8787` | Listen address |
| `BRIDGE_PASSTHROUGH` | `true` | Forward non-Agentaus models to Anthropic |
| `ANTHROPIC_UPSTREAM_BASE_URL` | `https://api.anthropic.com` | Passthrough target |
| `BRIDGE_PING_INTERVAL` | `10` | Seconds between keep-alive pings while Agentaus is silent |
| `BRIDGE_CHUNK_CHARS` | `60` | Re-chunk buffered replies into smaller deltas; `0` disables |
| `BRIDGE_TURN_TIMEOUT` | `900` | Wall-clock ceiling on one streamed Agentaus turn. `0` disables |
| `BRIDGE_TOKEN` | *(empty)* | Require this token from clients; empty means no client auth |
| `BRIDGE_LOG_LEVEL` | `info` | `debug` for per-request detail |
| `BRIDGE_LOG_BODIES` | `false` | Log full request bodies — **includes your source code** |
| `AGENTAUS_PROMPT_STYLE` | `markdown` | How the bridge lays out what it writes for Agentaus: `markdown` or `xml` |
| `AGENTAUS_LIVE_DRAFT` | `true` | Stream tokens into a thinking block while the answer is held for checks |
| `AGENTAUS_TOOL_FOCUS` | `true` | Hold back Claude Code's non-coding harness tools unless named or used. Custom and MCP tools are never hidden |

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
| Extended thinking | Agentaus exposes no reasoning channel, so `thinking` blocks from earlier Claude turns are stripped before sending. The bridge substitutes its own: it asks Agentaus to plan the turn as a separate call and shows that plan as a thinking block. |
| Context window | Agentaus accepts **131,072 tokens** total, prompt plus reply. The bridge enforces the limit and compacts for you — see [What happens when a conversation outgrows Agentaus](#what-happens-when-a-conversation-outgrows-agentaus). |
| Prompt caching | Not supported upstream, so `cache_control` markers do nothing. Every turn resends the whole conversation — the main cost driver in long sessions. |
| `max_tokens`, `temperature` | Accepted by Agentaus but ignored, so they are passed along without effect. |
| Anthropic server-side tools | Web search and code execution stubs are dropped; the bridge replaces `WebSearch` with `agentaus_web_search`. `WebFetch` survives translation unchanged. |

**Worth knowing before you rely on it**

Agentaus is a general-purpose assistant, not a model tuned for long agentic tool loops.
In testing it handled single tool calls and tool results correctly, but it is noticeably
more eager to re-call a tool it has already run than Claude is. It suits questions,
analysis, drafting and review inside a Claude Code session better than long autonomous
multi-step edits. Because there is no prompt caching, long sessions on Agentaus also cost
more per turn than the token counts alone suggest.

Two Claude Code features always call `api.anthropic.com` directly and never traverse the
bridge: the fast-mode availability check and the WebFetch domain safety check. On a network
that blocks Anthropic entirely those will report errors while inference keeps working.

---

## Running it in the background

To avoid keeping a terminal open, install a background agent. The bridge reads its own
`.env` from its working directory, so the API key stays out of the service definition.

### macOS — launchd (starts at login, restarts if it dies)

Generate the plist — do not copy a literal path out of this README, because the paths must
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

A wrong path here fails **silently** — `launchctl list` shows the label with no pid. That
is why `plutil -lint` and the `launchctl list` check below are worth running.

`RunAtLoad` plus `KeepAlive` means **nothing needs running by hand after a reboot**.
Confirm with:

```bash
launchctl list | grep agentaus      # a pid means it is running
curl -s http://127.0.0.1:8787/healthz
```

If you run the bridge with `./scripts/start-bridge.sh` in a terminal instead of under
launchd, that one *does* have to be started again after every reboot.

### Fedora / Debian — systemd user service

From the repo root:

```bash
CONN="$(pwd)"
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/agentaus-bridge.service <<EOF
[Unit]
Description=Agentaus bridge for Claude Code
After=network-online.target

[Service]
WorkingDirectory=${CONN}
ExecStart=${CONN}/.venv/bin/python -m agentaus_bridge
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user enable --now agentaus-bridge
```

Enable lingering so the service starts at login without you signed in:

```bash
loginctl enable-linger $(id -u)
```

### Windows — Task Scheduler

1. Open **Task Scheduler** → **Create Task**.
2. **General**: name `agentaus-bridge`.
3. **Triggers**: *At logon* (and optionally *At startup*).
4. **Actions**: *Start a program* → Program/Script: the full path to
   `<repo>\.venv\Scripts\python.exe`, Arguments: `-m agentaus_bridge`, Start in: the repo
   folder, so it reads `.env`.
5. Save and *Run* it once to confirm. Stop it, or use Task Manager, when you want it down.

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

Four suites, so the scoring authority sits outside this repository — a benchmark whose
verdicts are its own opinion cannot tell you anything:

| Suite | Measures | Verdict comes from |
| --- | --- | --- |
| `humaneval` | pass@1 over 164 problems | **the dataset's own unit tests** |
| `retrieval` | localisation precision, recall, F1 | ground truth over this repository |
| `documents` | can it answer from a PDF at all — found and cited rates | six facts checked to exist **only** inside PDFs, against the 263 non-PDF files in the corpus |
| `coverage` | does it survey the corpus or stop at the first hit | filenames counted against the real listing, so invented ones score nothing |

For a genuine head-to-head, `--model claude-opus-5` works but needs `ANTHROPIC_API_KEY`
exported: the bridge forwards those requests to api.anthropic.com and a harness cannot
borrow the OAuth token Claude Code holds.

Generated code runs in a subprocess with a wall-clock timeout. That bounds a runaway loop;
it is **not a sandbox**. Run it somewhere that is acceptable, or in a container.

---

## Reading the log

`/tmp/agentaus-bridge.log` (macOS/Linux). Every line for one turn carries the same short
request id, so a single turn can be followed end to end:

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
| `upstream start` | Handed off to Agentaus; anything after this is the model's time, not the bridge's. |
| `passthrough -> ` | A Claude-model turn was forwarded to Anthropic, with the status. |

To follow one turn: `grep 'req f5782343' /tmp/agentaus-bridge.log`. To watch live:
`tail -f /tmp/agentaus-bridge.log`.

`BRIDGE_LOG_LEVEL=debug` adds a line per summarisation call. `BRIDGE_LOG_BODIES=true`
logs full request bodies — **including your source code** — so leave it off routinely.
On Windows, log output goes to the console where you started the bridge (or Task
Scheduler's log viewer).

`./scripts/diagnose.py` reads the log and reports what is actually going wrong, ranked
by severity, each with what it means and what to do about it. It also lists what **never**
fired, which is the half people forget.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Agentaus error: Unauthorized` | Bad or expired `AGENTAUS_API_KEY`. Confirm with `--check`. |
| `HTTP 406` from Agentaus | Something sent `Accept: text/event-stream`. The bridge sends `Accept: */*`; check any proxy in front of it. |
| Agentaus missing from `/model` | `ANTHROPIC_CUSTOM_MODEL_OPTION` was not set before Claude Code started. Variables are read once at startup, so restart the session (reload the VS Code window). |
| Agentaus missing, but the bridge is healthy | **Settings scope.** Your `.claude/settings.json` is in the bridge repo, not the project you have open. Move the `env` block to `~/.claude/settings.json` (all projects) or that project's own `.claude/settings.json`, then restart. |
| `./scripts/claude-agentaus.sh: claude: command not found` | You have the VS Code extension but not the standalone `claude` CLI, so the launcher cannot work. Use a settings file instead (Option 1 or 2). |
| Both Agentaus **and** Claude fail at once | Almost always DNS or the bridge being down, not either provider. Everything flows through the bridge, so one broken resolver takes out both. `curl -sf localhost:8787/healthz` tells you in one second. |
| `This conversation is too long for Agentaus` | Working as intended. Agentaus' window is much smaller than Claude's, and there is no prompt caching, so long sessions hit it quickly. `/compact` usually recovers the session. |
| `/compact` also fails on Agentaus | Expected past 131k: compaction sends the conversation to the model to be summarised, so it overflows too. Switch to a Claude model, compact there, then switch back. `/clear` also works but discards history. |
| A short message on a long session takes minutes | The first compaction after a session outgrows the window reads the whole history. Later turns reuse it and are fast. `grep 'compaction done' /tmp/agentaus-bridge.log` shows the split. |
| `Address already in use` on start | An older bridge is still bound: `lsof -ti tcp:8787 \| xargs kill`. |

---

## Security notes

- The bridge binds `127.0.0.1` by default. It has **no client authentication** in that
  mode, which is fine on loopback. If you change `BRIDGE_HOST`, set `BRIDGE_TOKEN` as
  well — anything that can reach the port can otherwise spend your Agentaus quota.
- `.env` is gitignored; `BRIDGE_LOG_BODIES=true` writes full prompts (including your
  source code) to the log, so leave it off outside debugging.
- Everything sent to Agentaus leaves your machine for Trellis Data's Australian
  infrastructure, and everything sent to a Claude model leaves for Anthropic. The
  routing table above determines which — check it before putting sensitive code through
  either path.
