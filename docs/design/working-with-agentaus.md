# Working with Agentaus

What driving a smaller model through Claude Code's harness taught, and the rules the bridge's prompting follows as a result.

## What the live model taught us

Everything in the bridge was written against unit tests first. Driving real turns at
Agentaus found failures no test would have caught, and the fixes are worth knowing about
if you extend this.

**Structure everything.** Agentaus follows explicit structure far more reliably than
loose prose. Every prompt the bridge builds is sectioned - `request`, `excerpt`,
`tools_available`, `task`, `output_format` - and so is every tool result it forwards.
This was first done with XML tags; the layout is now Markdown headings and fenced blocks
by default (see [Markdown or tags](../../README.md#markdown-or-tags)), with the tags one setting away.
The benchmark measured the Markdown layout together with the other changes of that round,
not on its own, so what is established is that sectioning matters - not yet which syntax
does it better.

**A helper pass must never judge what it cannot see.** The bridge runs three extra calls
around a turn: plan before, review after, and distil in between. Each is a *fresh*
conversation. Two of them were actively harmful mid-tool-loop:

- The **planner** does not inherit the conversation, so on the second step of a tool loop
  it re-planned the step already taken — telling the model to read a file it was holding.
- The **reviewer** sees only the request and the answer. Given "list the Section 5
  headings" and a correct list of headings, with the source document invisible to it, it
  ruled the answer unverified and the revise pass replaced it with *"Please provide the
  DOCX file."* Measured on a real tender document: **0 of 6 turns correct through the
  bridge, 3 of 3 posting the identical payload straight to Agentaus.** The model was
  never the problem.

Both now sit out whenever the last message is a tool result. Same document afterwards:
**5 of 6**.

**The model invents tool names, and misspells real ones.** It answered a search by calling
`open_file`, which nobody offered it — passed through, that fails a `tool_use` in Claude
Code for a tool it has never heard of, and the turn dies looking like a bridge fault.
Invented names are caught and corrected upstream; `read` for `Read` is resolved silently
rather than spending a round.

**Descriptions decide tool choice, and position within them matters.** Given a bare list
of names the planner picks `Grep`, the one it knows from training. It sees each tool's
description now — and the restriction had to move to the *front* of Grep's description,
because a caveat appended after a paragraph loses to a strong prior.

## The guidance describes only what is on the wire

The `<tool_selection>` block in the system prompt is **generated per request** from the
tool list actually being sent, never hardcoded. A static list drifts the moment a feature
is switched off: with `AGENTAUS_ZOOM=false` the model would still be told to call
`agentaus_zoom`, invent it, and spend a correction round being told it does not exist —
the bridge teaching its own upstream a tool the bridge had removed.

Verified on the live bridge with both flags off:

```
AGENTAUS_ZOOM=false AGENTAUS_INVESTIGATE=false
  wire     : ['Glob', 'Read', 'agentaus_search', 'agentaus_web_search']
  guidance : ['Glob', 'agentaus_search', 'agentaus_web_search']
  drift    : none
```

The planning pass gets the same treatment: it is handed each tool's name *and* the opening
of its description, because a planner shown a bare list picks the name it recognises from
training — which is `Grep`.

## Pattern matching versus judgement

The bridge exists because Agentaus needs help, so it should ask Agentaus rather than
guess whenever the question is a judgement. Every remaining regex has been checked
against that, and the split is not "less is better" — it is **what happens when this
code runs**.

**Replaced with a model call.** Whether a reply is the model refusing to use tools it
has. Three phrase lists did this — about forty entries — and they were wrong in both
directions: they missed phrasings nobody had thought of, and they needed a companion list
of "filesystem words" beside them so that *"I don't have access to next year's budget"*
was not read as a refusal to read a file. Agentaus decides now, behind a **structural**
gate that costs nothing: a turn that was offered tools, called none, and produced a short
answer. Only then is there anything to judge.

Where a soft check can fail, it fails toward doing nothing: an unreadable verdict is
treated as a real answer, because re-asking a good answer is worse than passing a weak one
through — the user can just repeat a turn.

**Kept, deliberately.** Two of them, for the same reason:

| Pattern | Why it stays |
| --- | --- |
| `max_model_len\s+(\d+)` | Reads the real context window out of Agentaus' own error text. It runs **when the upstream is already failing** — asking that upstream to parse its own error message is asking the broken thing to explain itself. |
| `$$…$$` and the typographic table | Turns `EU‑WEST‑2` back into `EU-WEST-2` and `$$retry_budget_ms$$` back into a backticked identifier. A deterministic character mapping. A model asked to fix punctuation would introduce new corruption in the one place the bridge cannot tolerate it — identifiers a coding agent is about to act on. |

Two more are borderline and stay for now: the working-directory patterns in `augment.py`
(reading a path out of Claude Code's own stable system prompt) and `_SECTION_START` in
`tools.py` (finding a section boundary for `agentaus_zoom`). Both are candidates for soft
analysis. The second would cost a model call on every zoom, and zoom being free is worth
more than the accuracy it would buy — a wrong boundary makes a passage slightly larger,
which the token budget then trims anyway.

The search prefilter is worth a word because it looks like the same question and is not.
It uses literal matching to *rank* files and chunks — but the terms it ranks on come from
**Agentaus expanding the query**, which is the soft half. Replacing the ranking with
judgement too would mean reading everything to decide what to read, which is what the
prefilter exists to avoid.
