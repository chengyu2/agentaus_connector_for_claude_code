The user wants to change the Agentaus bridge concurrency limit (AGENTAUS_MAX_CONCURRENCY).

**Step 1 — Get the requested value**

The user should have provided a number as an argument (e.g. `/set-concurrency 4`). If no number was given, ask: "What concurrency limit would you like to set?"

**Step 2 — Check the current value**

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/healthz" -Method GET
```

Report the current concurrency limit from the response (field: `max_concurrency`), or note if it isn't shown.

**Step 3 — Warn if over 6**

If the requested value is **greater than 6**, say exactly this before doing anything:

> ⚠️ **Warning:** Setting concurrency above 6 may cause rate-limiting issues on the Agentaus shared tenant. When too many calls run at once, requests queue up and responses slow down significantly (sometimes 60–180 seconds). The recommended maximum is 6.
>
> Do you want to continue and set it to [VALUE] anyway?

Stop and wait for the user to confirm ("yes", "go ahead", etc.) before proceeding. If they say no or don't confirm, do nothing.

**Step 4 — Apply the change**

Update the value in the .env file (run from the repo root):

```powershell
(Get-Content .env) -replace '^AGENTAUS_MAX_CONCURRENCY=\d+', "AGENTAUS_MAX_CONCURRENCY=$VALUE" | Set-Content .env
```

Then verify it was written:

```powershell
Select-String -Path .env -Pattern "AGENTAUS_MAX_CONCURRENCY"
```

**Step 5 — Remind the user**

The bridge must be restarted for the new value to take effect. Report in one line:
"Concurrency set to [VALUE] in .env. Restart the bridge for it to take effect."
