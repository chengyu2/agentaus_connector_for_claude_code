First, check the current mode:

```sh
curl -s http://127.0.0.1:8787/quality
```

If `mode` is already `high`, reply in one line: "Already on High quality mode (thinking: on, review: on). No change made." and stop — do not POST anything.

Otherwise, switch to High quality mode:

```sh
curl -s -X POST http://127.0.0.1:8787/quality -H "Content-Type: application/json" -d "{\"mode\":\"high\"}"
```

Report the result in one line, e.g. "Switched to High quality mode (thinking: on, review: on)."
