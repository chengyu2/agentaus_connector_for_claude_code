---
name: bridge-diagnose
description: Work out what is wrong with the Agentaus bridge from its own log, and fix the top item. Use when turns are slow, stalling, returning empty answers, timing out, or behaving inconsistently, or when asked what needs fixing.
---

# Diagnosing the bridge from what it actually logged

## Start with the report, not the code

```bash
./scripts/diagnose.py                 # reads /tmp/agentaus-bridge.log
```

Every compensation in the bridge logs when it fires, so the log records which are earning
their keep, which fire often enough to indicate a defect, and which have never fired.
Reading the code and guessing is slower and worse.

The report ranks by severity. **Work the top HIGH item.** Do not fix three things at once -
you will not know which one worked.

## Read it as one finding, not a list

On a real run of 223 turns:

```
[HIGH]    63 x  upstream 5xx / gateway timeout
[HIGH]     1 x  summarisation fell back to trimming
[LOW]   1236 x  waited for a concurrency slot
```

63 timeouts and 1236 slot waits are **the same fact from two angles** - the bridge generated
far more upstream calls than Agentaus absorbs at its cap - and the client disconnects
followed from that rather than being separate faults.

The line worth acting on was the quiet one. *Summarisation fell back to trimming*, once,
means compaction dropped messages instead of summarising them: **real conversation lost**,
and the only entry costing correctness rather than time.

## Then trace one request end to end

```bash
grep "req <id>" /tmp/agentaus-bridge.log
```

A turn's whole life is on those lines: planning, compaction, each bridge tool call, retries,
the final status. Two things to check before theorising:

- **Timestamps are strings.** `awk '$1 >= "01:30:00"'` also matches `23:20:35`. Filter with
  `grep -E "^01:"` instead. This has sent a diagnosis to the wrong session.
- **The payload size is in the log.** A 524 on `est=3642` tokens against a 131,072-token
  window is a sick upstream, not a long conversation. Do not compact in response to it.

## What never to conclude too fast

- **An empty log is not a quiet system.** Python block-buffers through a pipe. Run with
  `PYTHONUNBUFFERED=1` and write straight to a file.
- **A label can hide a fact.** One line read `42 candidate(s) (brute force) -> 1 of 1
  chunk(s)` and sent a diagnosis chasing a bug that did not exist - the read had in fact
  been aimed, and the format string showed one of two independent flags.
- **A slow call is not a failed call.** Six abandoned helper calls in one run were all
  still-coming work, killed by a timeout that was too short.

## Rules

- Check the running process is the code you just changed: compare its start time against
  the newest source mtime. `launchctl kickstart -k gui/$(id -u)/com.trellisdata.agentaus-bridge`.
- Restarting mid-run kills in-flight requests. Let a batch finish first.
- Fix one thing, restart, re-measure. Two changes at once and the measurement is worthless.
