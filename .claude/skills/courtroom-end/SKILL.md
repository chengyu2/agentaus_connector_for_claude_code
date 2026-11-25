---
name: courtroom-end
description: End Courtroom mode and deliver the verdict summary. Use when asked to end the trial or close the case, when the judge picks the close option on the ruling buttons (Approve round, close the case), or when the user types /courtroom-end. Reads courtroom/case.md, verifies a case is open, closes it, and serves the judge a verdict summary: every charge across all rounds, how each was answered, what was sustained and fixed, and what remains open - without ever declaring the code sound.
---

# Courtroom mode - close the trial and deliver the verdict

## Roles (this command only closes)

| Role | Who | What they do |
| --- | --- | --- |
| **Judge** | The user | Received the verdict summary and rules on it |
| **Defense / Prosecution** | Quiet by definition - the trial is over |

This command does not argue. It reads the record and serves it.

## Close the trial

1. **Read `courtroom/case.md`** in the working directory. If it is absent or
   `Status: CLOSED`, there is no open case: say so and stop - do not invent a verdict
   for a trial that is not on the record.
2. **Reconcile the record.** Confirm the file is coherent before you present it: `Rounds`
   matches the trial-record entries, every round lists its charges, and every charge has a
   plea (conceded / rebutted / challenged) and a ruling (sustained / dismissed).
   If a charge is recorded as sustained, it must also record a fix from the same round -
   a sustained charge with no fix is an open item, not a closed one. State any
   discrepancy on the record rather than smoothing it over.
3. **Write the verdict summary.** Keep it to the record; the code itself is no longer
   before the court. The summary is:
   - **Charges by round** - a table: round, charges brought, fatal / serious / weak.
   - **Sustained and fixed** - each, with `file:line` and what the fix was.
   - **Dismissed** - each with the one-line reason (rebutted with proof / charge's
     citation did not mean what was alleged / challenge sustained by the code).
   - **Open items** - sustained charges whose fix was not recorded, and any charge the
     judge reserved instead of ruling.
   - **The verdict line** - one line, more precise than "sound" or "guilty". The honest
     denominators: "N charges brought, M sustained and fixed, K dismissed, P open."
4. **Close the case on the record.** Set `Status: CLOSED` in `courtroom/case.md`,
   append the verdict summary, and record the close date. Leave the trial record intact -
   the verdict is judged against it.

## Rules

- **The verdict is the judge's.** This command reports what the record shows; it never
  declares the code sound. "The prosecution could not break it" is written as that, not
  as "no bugs" - an adversarial pass ending is not a proof of correctness.
- **The record is authoritative.** If the record contradicts what the summary would
  say, the record wins; say where they diverge.
- **Close is final for this case.** After `Status: CLOSED`, a new `/courtroom` opens a
  new case; it does not reopen this one. If the judge wants to keep going, the command
  said so before closing, and the judge overruled.
- **End on the summary.** Deliver the verdict, name where it is recorded, and stop.
