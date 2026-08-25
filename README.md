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

## Quick start

```bash
./scripts/install.sh                          # create virtual env, install deps, copy .env.example
$EDITOR .env                                 # paste your AGENTAUS_API_KEY
./.venv/bin/python -m agentaus_bridge --check  # verify the key and a live call to Agentaus
./scripts/start-bridge.sh                     # start the bridge (leave this terminal open)
```

### Step by step guide

You can sign up for an Agentaus account and obtain an API key at **[agentaus.com.au](https://agentaus.com.au)**. After registering, navigate to the API dashboard to create a new key.

**Goal:** Clone this repository, set it up, and run the bridge so you can use the Agentaus model from Claude Code.

1. **Create a GitHub repository**
   - Go to https://github.com and click **New** → **Create a new repository**.
   - Name it something like `agentaus-bridge` and make it **public** (or private if you prefer).
   - Do **not** initialize with a README, .gitignore, or license – we’ll push our own.

2. **Clone the empty repo to your machine**
   ```bash
   git clone https://github.com/<YOUR_USERNAME>/<REPO_NAME>.git
   cd <REPO_NAME>
   ```

3. **Copy the project files into the clone**
   ```bash
   # Assuming you are still in the original project directory
   cp -R . ..   # copy everything into the parent directory (the clone)
   # Or use rsync to avoid copying the .git directory of the original (if any)
   rsync -av --exclude='.git' ./ ../<REPO_NAME>/
   cd ../<REPO_NAME>
   ```

4. **Remove any secret keys before committing**
   ```bash
   # The API key is in agentaus_api_doc.md – delete the line or the whole file if you prefer
   sed -i '' '/AGENTAUS_API_KEY/d' agentaus_api_doc.md
   # Also make sure .env is not tracked (it’s already in .gitignore)
   ```

5. **Initialize git, commit, and push**
   ```bash
   git add .
   git commit -m "Initial commit – Agentaus bridge for Claude Code"
   git push origin main
   ```
   You will be prompted for your GitHub username/password or a personal access token.

6. **Run the project** (you can do this directly in the cloned repo)
   ```bash
   ./scripts/install.sh
   $EDITOR .env      # paste your AGENTAUS_API_KEY here
   ./.venv/bin/python -m agentaus_bridge --check
   ./scripts/start-bridge.sh
   ```
   Then open a second terminal and start Claude Code with the launcher script:
   ```bash
   ./scripts/claude-agentaus.sh
   ```
   Inside Claude Code, select the *Agentaus* model via `/model agentaus`.

7. **Optional: set up the launchd service** (macOS only) – see the "Running it in the background" section later in this README.

Once the bridge is running, open a second terminal and start Claude Code:

```bash
./scripts/claude-agentaus.sh
```

Inside Claude Code, run `/model` — **Agentaus (Trellis Data)** now sits in the list
below Opus, Sonnet and Haiku. Select it, or type `/model agentaus`.

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

Three ways, in order of convenience.

### Option A — the launcher script (recommended)

`./scripts/claude-agentaus.sh` sets everything and execs `claude`. It forwards any
arguments, so `./scripts/claude-agentaus.sh --model agentaus` starts directly on Agentaus.

### Option B — export the variables yourself

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_CUSTOM_MODEL_OPTION="agentaus"
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Agentaus (Trellis Data)"
export ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="Sovereign Australian model via the local bridge"
claude
```

`ANTHROPIC_CUSTOM_MODEL_OPTION` is what adds the extra row to the `/model` picker;
Claude Code skips validation on that id, so any string the bridge understands works.

### Option C — settings.json

To make it permanent for one project, create `.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Agentaus (Trellis Data)",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Sovereign Australian model via the local bridge"
  }
}
```

Use `~/.claude/settings.json` instead to apply it everywhere. Claude Code reads
environment variables once at startup, so restart the session after changing them.

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
| Extended thinking | Agentaus exposes no reasoning channel. `thinking` blocks from earlier Claude turns are stripped before sending. |
| Prompt caching | Not supported upstream, so `cache_control` markers do nothing. Every turn resends the whole conversation — the main cost driver in long sessions. |
| Token counting | Agentaus has no tokenizer endpoint, so `/v1/messages/count_tokens` returns a chars÷4 estimate. It feeds the context meter and auto-compact trigger only, never billing. |
| `max_tokens`, `temperature` | Accepted by Agentaus but ignored, so they are passed along without effect. |
| Anthropic server-side tools | Web search and code execution tool stubs have no `input_schema` and are dropped; Agentaus has its own internal web search which it triggers on its own judgement. |

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

To avoid keeping a terminal open, install a launchd agent at
`~/Library/LaunchAgents/com.trellisdata.agentaus-bridge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.trellisdata.agentaus-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/chengyu/PycharmProjects/agentaus_api_into_claude_code/.venv/bin/python</string>
    <string>-m</string>
    <string>agentaus_bridge</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/chengyu/PycharmProjects/agentaus_api_into_claude_code</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/agentaus-bridge.log</string>
  <key>StandardErrorPath</key><string>/tmp/agentaus-bridge.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.trellisdata.agentaus-bridge.plist
```

---

## Testing

```bash
./.venv/bin/python -m unittest discover -s tests -v   # translation unit tests
./.venv/bin/python tests/smoke_test.py                # end-to-end, needs a running bridge
```

The unit tests cover the translation edge cases that break agent loops: tool-result
ordering, fragmented tool-call deltas, block open/close pairing in the SSE stream, and
malformed tool arguments.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Agentaus error: Unauthorized` | Bad or expired `AGENTAUS_API_KEY`. Confirm with `--check`. |
| `HTTP 406` from Agentaus | Something sent `Accept: text/event-stream`. The bridge sends `Accept: */*`; check any proxy in front of it. |
| Claude Code hangs, then reports a stream error | The bridge was not running, or pings were stripped by an intermediate proxy. Raise `BRIDGE_LOG_LEVEL=debug` and watch. |
| Agentaus missing from `/model` | `ANTHROPIC_CUSTOM_MODEL_OPTION` was not exported before `claude` started. Variables are read once at startup. |
| Claude models fail while Agentaus works | Passthrough auth. Do not set `ANTHROPIC_AUTH_TOKEN` unless you intend to stop using your subscription. |
| Replies read like a generic assistant, not an agent | `AGENTAUS_SYSTEM_PROMPT_OVERWRITE` got turned off, so the Agentaus persona is back in front of Claude Code's prompt. |
| `API Error: Failed to parse JSON` | Fixed in this repo. The passthrough was returning the upstream body still gzip-compressed while stripping `content-encoding`. Only larger responses are compressed, so it looked random. Covered by `tests/test_passthrough.py`. |
| `API Error` / auto mode fails on every action | Do not set `CLAUDE_CODE_ATTRIBUTION_HEADER=0`. Behind a custom base URL it also strips the block from auto-mode permission-classifier requests, which the API declines with 401. |
| Both Agentaus **and** Claude models fail at once | Almost always DNS or the bridge being down, not either provider. Everything flows through the bridge, so one broken resolver takes out both. See the row below. |
| `nodename nor servname provided, or not known` | The macOS system resolver (`mDNSResponder`) is wedged. Diagnose by comparing `dig agentaus.com.au` (queries the DNS server directly) with `python3 -c "import socket;socket.getaddrinfo('agentaus.com.au',443)"` (uses the system resolver). If `dig` works and `getaddrinfo` fails, every app on the machine is affected, not just the bridge. Fix: `sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder`, or reboot. |
| Agentaus vanished from the `/model` list | `.claude/settings.json` is missing or lost `ANTHROPIC_CUSTOM_MODEL_OPTION`. That variable creates the row and is read **once at startup**, so restore the file and restart Claude Code. |
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
