First, check the current mode:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/quality" -Method GET
```

If `mode` is already `medium`, reply in one line: "Already on Balanced mode (thinking: off, review: on). No change made." and stop — do not POST anything.

Otherwise, switch to Balanced mode:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/quality" -Method POST -ContentType "application/json" -Body '{"mode":"medium"}'
```

Report the result in one line, e.g. "Switched to Balanced mode (thinking: off, review: on)."
