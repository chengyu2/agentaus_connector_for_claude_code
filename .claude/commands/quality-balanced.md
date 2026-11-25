First, check the current mode:

```sh
curl -s http://127.0.0.1:8787/quality
```

If `mode` is already `medium`, reply in one line: "Already on Balanced mode (thinking: off, review: on). No change made." and stop — do not POST anything.

Otherwise, switch to Balanced mode:

```sh
curl -s -X POST http://127.0.0.1:8787/quality -H "Content-Type: application/json" -d "{\"mode\":\"medium\"}"
```

Report the result in one line, e.g. "Switched to Balanced mode (thinking: off, review: on)."
