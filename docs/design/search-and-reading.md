# Search and reading

How the bridge's own tools aim before they read. The tools themselves are listed in the README's [Tools the bridge runs itself](../../README.md#tools-the-bridge-runs-itself).

## Aiming before reading

Search finds things by reading — one model call per chunk. That is what makes it work when
the answer shares no words with the question, and what makes it cost ten calls on a large
file.

The cheap half of that problem is structural. *"Which part of this is about security
accreditation"* follows from declarations or headings, and those cost **nothing** to
extract: no model call, no upstream request. So the bridge builds an outline locally and
spends **one** call over a table of contents, then reads only the sections it names.

```
corpus                              content   outline   ratio
this bridge's own source (code)      66,486     6,431     10x
a 434KB tender response (prose)      89,579     9,276     10x
a returnable-documents .docx         26,865       188    143x
```

Aiming is an optimisation and never a gate. An unparseable reply, an invented path, a
failed call — all fall back to reading every chunk, because reading less is not worth
missing the answer.

### Three extractors, for three structures

| Corpus | Indexed by |
| --- | --- |
| Source code | declarations — `def`, `class`, `func`, `struct`, `interface`, `impl`… |
| Prose | headings — Markdown, bold-only lines, clause numbers like `5.3.1` |
| JSON | its keys, which is all its structure amounts to |

That is not the same mistake as two extractors for one structure. A function signature and
a heading are not competing descriptions of the same thing.

### Code uses Tree-sitter, and why that is worth a dependency

Declarations come from **Tree-sitter**, across nineteen languages — Python, JavaScript,
TypeScript, TSX, Go, Rust, Java, Kotlin, Swift, C, C++, C#, Ruby, PHP, Scala, Bash, SQL,
Lua, Dart. It produces a Concrete Syntax Tree with every token mapped to an exact byte,
line and column, so a declaration is what the *grammar* says it is:

```
     36  def gate()
     84  class _PriorityGate
    113  async def acquire(self, priority)
```

A line pattern gets most of those and is wrong where it matters. It cannot tell a
declaration from the same words inside a docstring or a comment, and it misses a signature
written across two lines — both of which are tested for.

There is a strict quality ordering rather than competing implementations, and each rung
runs only when the one above cannot:

```
1. Tree-sitter       exact, 19 languages, required
2. Python `ast`      exact, one language, always available
3. declaration pass  approximate, any language, always available
```

Tree-sitter is a [requirement](../../README.md#requirements), installed from `requirements.txt`, so rung
one is always there. The lower rungs remain for a grammar that fails on one file.

## Zoom: from a citation to something you can write with

`agentaus_search` returns hits like `Drafted_Responses.md:3585-3586` with a line or two
quoted. That is enough to prove a fact is there and **not** enough to write from — and a
model given only that produces exactly what you would expect:

```
ASSERTION: supports scanned images, drawings, correspondence...
EVIDENCE:  "REST-based API catalogue... accepts any file type"
ADD:       "(evidence: FIN-2025-26-00616 - lines 3585-3586)"
```

A citation where a sentence was wanted, because a citation was all it had.

`agentaus_zoom` closes that loop. Give it a file and a line number and it returns the
passage **widened to its section, verbatim, with line numbers intact** — and never calls a
model to do it:

```
agentaus_zoom(file_path=..., start_line=3359)
  │
  ├─ widen ..... outward to the nearest section boundary — Markdown heading, bold-only
  │              line, numbered clause — so the passage arrives with its heading
  ├─ trim ...... to AGENTAUS_ZOOM_MAX_TOKENS, growing outward from the citation so the
  │              lines nearest the quote are the ones kept
  └─ return .... tagged, so the model knows it is holding a window and not a whole file

<passage file="…" lines="3352-3366" you_asked_for="3359-3359"
         section_ends_at="3366" verbatim="true" complete="true">
  3352  **Secure, governed environment**
  ...
</passage>
```

The attributes are the point. A passage that stops mid-section reads, to a model, like a
file that stops there — so it either concludes the content is absent or quotes across the
cut. `complete="false"` and `section_ends_at` say otherwise, and this model follows
structure far more reliably than it follows a sentence.

**It never condenses.** It used to, above a token threshold, and that was wrong from the
start: the point of zooming into a citation is to see the exact words before quoting them,
and condensing them first destroys the only thing the caller came for. Truncating verbatim
is strictly better and instant.

Getting that wrong was expensive. The threshold was an order of magnitude too low, so
every zoom over ordinary tender prose paid for a model call — 73 to 125 seconds under
load, into a 240-second helper timeout, stalling a batch run for an hour. Raising the
threshold moved the bulk downstream until the turn carrying it drew a Cloudflare 524. The
whole class of problem disappeared when the condensation path was deleted rather than
tuned.

Search results end with a line telling the model this tool exists — a citation is only
useful if it knows it can open it.
