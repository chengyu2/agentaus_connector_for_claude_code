---
name: repo-survey
description: Survey what is actually in a repository or folder tree before answering about it. Use when asked to review, audit, compare, or summarise "the whole repo", "all the files", "what collateral exists", or any question whose honest answer depends on more files than you can read. Prevents answering a repository-wide question from a truncated directory listing.
---

# Surveying a tree without inventing the parts you did not read

## The failure this exists to stop

Asked to review a whole repository, the usual reflex is one `find` or `ls -R`. That output
is larger than the conversation can carry, so the client truncates it to a ~2 KB preview
and saves the rest to a file. The preview then gets treated as the survey.

What that produced, verbatim, in a real session:

> *"the repo's `_notes.md` usually enforces a single, consistent fit label"* — a file that
> was never opened
> *"No apparent breach of the 'prohibitions live once' rule"* — **a policy that does not exist**

Both stated as findings, in a paragraph of otherwise reasonable analysis. That is the
failure mode: not a wrong answer, an invented one.

## The script

**1. Count before you read.** One bounded command, never a bare `find`:

```bash
find "<root>" -type f | sed 's/.*\.//' | tr 'A-Z' 'a-z' | sort | uniq -c | sort -rn | head -20
```

Now you know the shape of the tree: how many files, of what kinds. State that count in your
answer. It is the one claim about "the whole repo" you have actually earned.

**2. If a tool result was truncated, read the file it names.** The client writes the full
output to disk and tells you the path. Read that path. Do not survey from a preview.

**3. Find by meaning, not by listing.** For "which files matter for X", call
`agentaus_search` with the root path and a plain-language query. It reads by meaning and
returns quotes with line numbers. `find`, `ls -R` and `grep -r` cannot see inside a file
and produce more output than the turn can hold.

**4. Read before you characterise.** Any sentence describing what a file *contains*
requires having read that file. `agentaus_zoom` on a citation gets you the passage verbatim.
A search hit proves a fact exists; it is not enough to paraphrase from.

**5. Separate what you read from what you did not.** Structure the answer:

```
## Read and verified
<file> — <what it contains, with a line reference>

## Present but not read
<file or group> — <why: too many, not relevant, not text>

## Not covered
<anything the survey could not reach — image-only PDFs, binaries>
```

## Rules

- **Never describe the contents of a file you did not open.** If it matters, open it. If
  there are too many, say how many and which you sampled.
- **Never name a policy, convention or standard you did not read.** If a repository
  convention is relevant, quote the file that states it.
- **A count is a claim too.** "Hundreds of markdown files" needs the number.
- Image-only PDFs cannot be read as text. Say so rather than guessing at their contents.
- When the honest answer is "this tree is larger than I surveyed", that is the answer.
  A partial survey stated as partial is useful; a partial survey stated as complete is not.
