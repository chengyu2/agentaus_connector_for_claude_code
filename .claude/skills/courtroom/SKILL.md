---
name: courtroom
description: Begin Courtroom mode - the adversarial review loop. When asked to start courtroom mode, to open or begin a trial, or when the user types /courtroom. With no active case it opens one (names the code under review, records the defense's position, writes courtroom/case.md) and opens the trial; with an active case it runs the next round: the Agentaus-backed prosecutor attacks the code, the defense (this session) answers each charge, and sustained charges get fixed. The judge rules from on-screen buttons, not a typed command. Bugs surface through argument, not a single review.
---

# Courtroom mode - begin a trial, or run the next round

## Roles

| Role | Who | What they do |
| --- | --- | --- |
| **Defense** | This main session (Claude) | Wrote or holds the code; answers every charge; fixes what is conceded |
| **Prosecution** | The `prosecutor` subagent (pinned to `model: agentaus`) | Attacks the code and returns charges with file:line, proof, severity |
| **Judge** | The user | Rules from the on-screen buttons: approves, dismisses, asks for more, or closes |

Agentaus is the prosecutor because the `prosecutor` agent type pins `model: agentaus`, and
the bridge routes any model id containing `agentaus` to Agentaus while the main loop
stays on Claude. Confirm it landed there by checking the bridge log during the round:
the prosecution's turn shows `model=agentaus ... route=agentaus`. (A plain `route=agentaus`,
not the `(subagent of an Agentaus session)` suffix — that suffix only appears when the
*session's main loop* is on Agentaus, and here it is not.)

## How the debate is shown - the two voices never blend in

The prosecution and the defense are two halves of one visible conversation. Every turn is
formatted so a reader can tell, at a glance and without reading a word, who is speaking:

- **Speaker banner.** The side speaking opens with a bold header alone on its own line:
  `### ⚖️ PROSECUTOR (Agentaus)` for the prosecution's side, `### 🛡️ DEFENSE (Claude)`
  for this session. Never put both voices under one banner.
- **One turn per block.** Set each side's whole turn apart with `---` rules and blank
  lines above and below, so a speech reads as a speech, not a paragraph that runs on
  into the background.
- **Quote whoever spoke first.** When the defense answers a charge, quote that charge as
  a blockquote (`> **CHARGE 1 —** ...`) and put the plea directly beneath it. The
  call-and-response is the point: charge quoted, plea answered, always adjacent.
- **Verdict-first plea.** Start every plea with the bold ruling word on its own:
  `**SUSTAINED · CONCEDED**`, `**DISMISSED · REBUTTED**`, or `**CHALLENGED**`.

The contrast comes from the banner, the blockquote, and the separators working together.
Ship those with every turn; never rely on wording alone to show who is talking.

## Begin a new trial (no active case)

1. **Find the code under review.** If the user named files or a branch, those. Otherwise
   the working diff: run `git diff` (unstaged) and, if the branch has one, the diff of the
   open pull request. If there is no git diff and no files are named, say so and ask the
   judge which code is on trial before opening a case - do not invent a subject.
2. **Read the code** you are going to defend. Do not open a case over code you have not
   looked at.
3. **Write `courtroom/case.md`** in the working directory, with exactly this shape:

   ```markdown
   # Case - <what is being worked on>

   Status: OPEN
   Rounds: 0
   Code under review: <diff scope or file list>
   User's request: <the task the code answers>
   Defense position: <the opening statement - why this code is correct>
   Trial record: <appended, one entry per round>
   ```

   Keep the request verbatim and keep every later amendment on the record; the record is
   what the verdict is judged against.
4. **Open the trial.** Report to the judge: the code on trial, that the prosecution sits
   with Agentaus, and that the judge rules from on-screen buttons after each round. Run
   the first round now unless the judge wants to set ground rules first.

## Run the next round (active case open)

1. **Read `courtroom/case.md`.** If it is absent or `Status: CLOSED`, treat this as a new
   trial and open one.
2. **Prosecution.** Spawn the prosecution with `Agent(subagent_type: "prosecutor")`. Give
   it the request verbatim, the code under review, and the defense's position, and tell it
   to return its charges under the `### ⚖️ PROSECUTOR (Agentaus)` banner. It runs on
   Agentaus.
3. **Defense.** Answer every charge one by one on the record: **conceded** (and fix it
   now), **rebutted** (with the proof - a test, a code path, a read of the file), or
   **challenged** (the charge cites a line that does not mean what the prosecution says).
   Quote each charge in a blockquote under the prosecution banner, then answer it under
   the defense banner. A charge the defense rebuts is **dismissed**; a charge the defense
   concedes is **sustained** and must be fixed in the same round.
4. **Append the round to the record** in `courtroom/case.md`: each charge, the plea, the
   ruling, and the fix if any. Bump `Rounds`.
5. **Rule by buttons, never by a typed command.** Do not tell the judge to type
   `/courtroom-end`. After the round summary, call `AskUserQuestion` so the judge rules
   from on-screen options (below), then act on the answer and stop.

## Ruling buttons - the judge rules here, not by typing

After every round summary, the defense calls `AskUserQuestion` once with the judge's four
rulings, so the case is driven by buttons instead of the typed `/courtroom-end`:

- **Approve round, run another** - the sustained fixes are accepted; start the next round.
- **Approve round, close the case** - accept the fixes and close: run the `courtroom-end`
  procedure (it serves the verdict and sets `Status: CLOSED`).
- **Ask for more on a charge** - a charge was not answered to the judge's satisfaction;
  stay in the round and answer it further.
- **Dismiss a charge** - the judge overrules a ruling; record the judge's ruling on the
  record and follow it.

The judge always has "Other" as well - they can use it to set ground rules, name new code,
or any free-form ruling.

## Rules that keep it a trial and not a single review

- **One side at a time.** The prosecution argues a round; the defense answers it; the judge
  rules. No side answers for the other.
- **Charges must cite code.** A charge without a `file:line` is an argument, and the judge
  is told it is unsubstantiated rather than treated as a defect.
- **Sustained means fixed, in the same round.** A conceded bug is not a finding yet; it is
  a finding when the fix is applied and the round records it.
- **The verdict is the judge's.** This command never declares the code sound; it closes
  with whatever the record shows, and the close (via the buttons or `/courtroom-end`)
  serves that record. A code the prosecution could not break is reported as
  "no charge sustained", never "no bugs".
