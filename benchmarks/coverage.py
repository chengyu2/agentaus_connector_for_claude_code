"""Does it cover the ground, or answer from the first thing it found?

The complaint this measures, in the words it was reported in: asked to go through a repo
of hundreds of documents, the answer came back built on one or two files. Not wrong about
those files - blind to the rest. Every other suite here scores whether an answer is
*correct*. None of them scores whether it is *complete*, and completeness is the failure
that actually shows up in use.

So this asks a question that cannot be answered honestly from one file, and counts how
much of the corpus the answer demonstrably touched. A model that cites two documents out
of four hundred has not surveyed anything, however fluent the summary reads.

## It is an A/B, not a score

Reporting "cited 6 files" alone means nothing - there is no target, and inventing one
would just be a number to tune against. What is measurable is the *difference* the
`search-exhaustively` procedure makes, so each question runs twice: once with the
procedure in the system prompt, once without. Same question, same corpus, same model.

That comparison is the point. A skill is a claim that instructions change behaviour, and
a claim like that is worth checking rather than assuming - especially here, where a skill
was found telling the model to stop after two lookups while another told it to keep
going.

Citations are counted, not asserted. A filename only counts if it exists in the corpus,
so a fluent answer that invents plausible document names scores zero for them - which is
the specific failure mode a survey question invites.
"""
from __future__ import annotations

import os

CORPUS = "/Users/cheng/trellis_data_tender"

QUESTIONS = [
    "What kinds of documents are in this folder, and what does each kind cover? "
    "Name the specific files.",
    "Across this folder, what evidence exists about Trellis Data's technical "
    "capabilities? Name every document you drew on.",
    "What tender or procurement material is in this folder? List the documents and "
    "what each one is for.",
]

# The procedure under test, compressed to its operative rules. Deliberately the rules
# themselves rather than a summary of them: the question is whether these instructions
# change behaviour, so anything softened here would test something else.
PROCEDURE = """
<procedure name="search-exhaustively">
The question asks about ALL of something. Answering from the first files that matched is
the failure mode, not the answer.

1. Establish the ground first - how many files, of what kinds. You cannot know you have
   finished if you never knew what "all" was.
2. Search in ROUNDS, changing the angle each round: by what something does, by what it is
   called, by who uses it, by when it happens. One phrasing finds one neighbourhood.
3. Keep a list of files you have actually opened - not files that matched.
4. Stop after TWO consecutive rounds that surface nothing new, not after one good hit.
5. State what you did not cover. A silent cap reads as full coverage.
</procedure>
"""

SYSTEM = (
    "You answer questions about a folder of documents. Search it before you answer, and "
    "name the specific files you drew on."
)

PROMPT = "<task>\n{question}\n</task>\n\n<corpus>{root}</corpus>\n{procedure}"


def corpus_files(root: str) -> set:
    """Every filename in the corpus, lowercased, for checking citations against."""
    names = set()
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if not d.startswith(".")]
        for name in files:
            if not name.startswith("."):
                names.add(name.lower())
    return names


def cited(answer: str, known: set) -> set:
    """Filenames from the corpus that the answer actually names.

    Matched against the real listing rather than by pattern, so an invented but
    plausible-looking document name scores nothing. Stems are checked too because an
    answer reasonably writes "Echo PowerPoint WIP2" without the extension.
    """
    text = (answer or "").lower()
    found = set()
    for name in known:
        stem = os.path.splitext(name)[0]
        if len(stem) < 8:
            continue
        if name in text or stem in text:
            found.add(name)
    return found


def run(model: str, limit: int | None, ask, totals, log=print, root: str = ".") -> dict:
    corpus = CORPUS if os.path.isdir(CORPUS) else os.path.abspath(root)
    known = corpus_files(corpus)
    tools = [{
        "name": "Read",
        "description": "Read a file from the filesystem.",
        "input_schema": {"type": "object",
                         "properties": {"file_path": {"type": "string"}},
                         "required": ["file_path"]},
    }]
    rows = QUESTIONS[:limit] if limit else QUESTIONS
    log(f"  corpus: {len(known)} files under {corpus}")

    arms = {"bare": "", "skill": PROCEDURE}
    counts = {"bare": [], "skill": []}

    for index, question in enumerate(rows, 1):
        for arm, procedure in arms.items():
            reply = ask(model, PROMPT.format(question=question, root=corpus,
                                             procedure=procedure),
                        system=SYSTEM, tools=tools, max_tokens=1600)
            totals.add(reply)
            if not reply.ok:
                counts[arm].append(0)
                log(f"  [{index}/{len(rows)}] {arm:5} CALL FAILED  {reply.error[:60]}")
                continue
            names = cited(reply.text, known)
            counts[arm].append(len(names))
            log(f"  [{index}/{len(rows)}] {arm:5} cited {len(names):>2} file(s) "
                f"{reply.seconds:5.1f}s  {question[:40]}")

    def mean(arm):
        return sum(counts[arm]) / max(1, len(counts[arm]))

    bare, skill = mean("bare"), mean("skill")
    log(f"  mean files cited: bare {bare:.1f}  with procedure {skill:.1f}  "
        f"({skill - bare:+.1f})")
    return {"suite": "coverage", "n": len(rows), "bare_cited": bare,
            "skill_cited": skill, "delta": skill - bare,
            "one_file_answers": sum(1 for c in counts["bare"] + counts["skill"] if c <= 1)}
