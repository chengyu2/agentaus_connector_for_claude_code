First, check the current mode:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/quality" -Method GET
```

If `mode` is already `high`, reply in one line: "Already on High quality mode (thinking: on, review: on). No change made." and stop — do not POST anything.

Otherwise, switch to High quality mode:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/quality" -Method POST -ContentType "application/json" -Body '{"mode":"high"}'
```

Report the result in one line, e.g. "Switched to High quality mode (thinking: on, review: on)."
