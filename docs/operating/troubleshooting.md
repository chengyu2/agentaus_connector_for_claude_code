# Troubleshooting and the log

The full version of the README's [Troubleshooting](../../README.md#troubleshooting) and
[Reading the log](../../README.md#reading-the-log) sections.

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

## Reading the log

The bridge logs to stderr, so where the log ends up depends on how you started it:

| Started with | Log |
| --- | --- |
| `./scripts/start-bridge.sh` | The terminal it runs in. Add `2>&1 \| tee /tmp/agentaus-bridge.log` to keep a copy |
| launchd (macOS) | `/tmp/agentaus-bridge.log`, set by the plist |
| systemd user service (Linux) | The journal: `journalctl --user -u agentaus-bridge`. For `diagnose.py`, save it first: `journalctl --user -u agentaus-bridge -o cat > /tmp/agentaus-bridge.log` |

The examples below use `/tmp/agentaus-bridge.log`. Every line for one turn carries the same short request id,
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

## Symptoms

| Symptom | Cause and fix |
| --- | --- |
| `Agentaus error: Unauthorized` | Bad or expired `AGENTAUS_API_KEY`. Confirm with `--check`. |
| `HTTP 406` from Agentaus | Something sent `Accept: text/event-stream`. The bridge sends `Accept: */*`; check any proxy in front of it. |
| Claude Code hangs, then reports a stream error | The bridge was not running, or pings were stripped by an intermediate proxy. Raise `BRIDGE_LOG_LEVEL=debug` and watch. |
| Agentaus missing from `/model` | `ANTHROPIC_CUSTOM_MODEL_OPTION` was not set before Claude Code started. Variables are read once at startup, so restart the session (reload the VS Code window). |
| Agentaus missing from `/model`, but the bridge is healthy | **Settings scope.** The `env` block was never added to a settings file that covers the project you have open. Copy `.claude/settings.example.json` from the bridge repo into `~/.claude/settings.json` (all projects) or into that project's own `.claude/settings.json`, then restart. See [Pointing Claude Code at the bridge](../../README.md#pointing-claude-code-at-the-bridge). |
| `./scripts/claude-agentaus.sh: claude: command not found` | You have the VS Code extension but not the standalone `claude` CLI, so the launcher cannot work. Use a settings file instead (Option 1 or 2). |
| Every project suddenly fails, not just Agentaus | You set `ANTHROPIC_BASE_URL` at user level and the bridge is not running. Start it, or run it [in the background](../../README.md#running-it-in-the-background). `curl -sf localhost:8787/healthz` tells you in one second. |
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
