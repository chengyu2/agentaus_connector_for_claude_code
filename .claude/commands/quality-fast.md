First, check the current mode:

```sh
curl -s http://127.0.0.1:8787/quality
```

If `mode` is already `low`, reply in one line: "Already on Fast mode (thinking: off, review: off). No change made." and stop — do not POST anything.

Otherwise, switch to Fast mode:

```sh
curl -s -X POST http://127.0.0.1:8787/quality -H "Content-Type: application/json" -d "{\"mode\":\"low\"}"
```

Report the result in one line, e.g. "Switched to Fast mode (thinking: off, review: off)."
