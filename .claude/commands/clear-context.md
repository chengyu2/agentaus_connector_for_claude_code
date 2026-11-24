Run this PowerShell command to clear all conversation history from the bridge on the next request:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/clear-context" -Method POST
```

Report the result in one line, e.g. "Context cleared — history will be wiped on the next message."
