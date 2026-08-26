---
name: find-in-code
description: Locate something in a codebase or document tree - where a behaviour lives, what handles a case, why a value is set. Use when asked "where is", "how does this work", "which file does X", or before answering any question whose answer is a location. Chooses the right search tool and reads the passage before quoting it.
---

# Finding something, and being able to quote it afterwards

## Pick the tool by the question, not by habit

| Question shape | Tool |
| --- | --- |
| How something works, where a behaviour lives, why a value is set | **`agentaus_search`** — reads by meaning |
| Being wrong would be expensive (before a refactor, tracing a cause) | **`agentaus_investigate`** — see the **`investigate`** skill |
| An exact string you can already spell — a known function name, an error message | `Grep` |
| A filename you know | `Glob` |
| Anything outside this repository | `agentaus_web_search` — see the **`research`** skill |

**If the question says "all", "every", or "the whole repo", this is the wrong skill.** This one stops early by design. Use **`search-exhaustively`**, which keeps going until two rounds find nothing new.

**`Bash` is for running things** — tests, builds, git. Not for searching. `find`, `ls -R`,
`grep -r` and `cat` over many files produce more output than the conversation can carry, so
you are handed a truncated preview and end up answering from a fragment.

The habit to resist: reaching for `Grep` or `find` because they are familiar. A regex only
finds text you can already spell. *"Where do we cap concurrent calls?"* is answered by
`asyncio.Semaphore(6)` — which shares no word with the question.

## The script

1. **Search once**, in plain language, with the absolute repository path.
2. **Zoom before you quote.** `agentaus_zoom` on the citation returns the passage verbatim
   with its heading and line numbers. A search hit proves a fact is there; it does not give
   you enough to paraphrase accurately or to see what it depends on.
3. **Answer with locations.** `path:line` or `path:symbol`. A location can be checked; a
   description cannot.
4. **If you found nothing, say so.** "No evidence found in X" is a real answer. An invented
   location is worse than an absent one.

## Rules

- Do not run the same search twice. If you have already searched for something, re-read the
  result you got.
- Do not call a tool that was not offered to you. If you need something there is no tool
  for, say what is missing.
- Do not list directories to get your bearings when you were given a path. Use the path.
- One search, then at most two zooms, then answer. This bound is deliberate and it
  applies to finding **one** thing. It does not apply to a question about all of
  something — that is `search-exhaustively`, where stopping early is the failure.
