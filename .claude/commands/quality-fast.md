First, check the current mode:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/quality" -Method GET
```

If `mode` is already `low`, reply in one line: "Already on Fast mode (thinking: off, review: off). No change made." and stop — do not POST anything.

Otherwise, switch to Fast mode:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/quality" -Method POST -ContentType "application/json" -Body '{"mode":"low"}'
```

Report the result in one line, e.g. "Switched to Fast mode (thinking: off, review: off)."
