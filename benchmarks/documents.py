"""Can it answer from a PDF? The question the bridge scored zero on until today.

Every fact below lives **only** inside a PDF. Checked, not assumed: each needle was
searched for across the 263 non-PDF files in the reference corpus - 93MB of markdown,
Word, Excel and text - and appears in none of them. So a run that scores above zero here
is reading PDFs, and one that scores zero is not, regardless of how fluent the answer is.

That distinction had teeth. `.pdf` sat in the search tool's skip list, so all 48 PDFs in
the corpus were invisible: `enumerate_files` returned nothing for them and `read_text`
returned megabytes of binary. A question whose answer was in a PDF could not be answered
correctly by any amount of searching, and the failure was silent - the model answered
from whatever non-PDF file matched instead, fluently and wrongly.

Two things are scored, because finding and citing are different skills. `found` is
whether the substance is there. `cited` is whether the answer names the document it came
from - which is what makes a claim checkable by the person reading it, and the exact
discipline that four unsupported tender rows lacked.

FROZEN, on the same terms as `retrieval.py`: ground truth tuned against the answers it
receives measures nothing. The wording is deliberately not the wording in the document -
asking "what does SC-NFR-11 require" using the document's own sentence would test string
matching rather than retrieval.
"""
from __future__ import annotations

import os
import re

# Where the tender folder lives on the machine that wrote this suite. Anywhere else, pass
# --corpus or set BENCH_CORPUS: falling back to the current directory used to ask tender
# questions of the bridge's own source and report the zero as a result.
CORPUS = os.environ.get("BENCH_CORPUS", "/Users/cheng/trellis_data_tender")

QUESTIONS = [
    {
        "q": "In the tender response material, what does requirement SC-NFR-11 say about "
             "the protocol used to secure data in transit?",
        "any": [["ssl"], ["tls"]],
        "source": "P21-407",
    },
    {
        "q": "What does requirement PM-NFR-03 ask for regarding the versions that "
             "hardware and software run on?",
        "any": [["manufacturer*"], ["supported version*"], ["patch*"]],
        "source": "P21-407",
    },
    {
        "q": "What does requirement BR-NFR-01 cover, and how many deployment options "
             "were offered against it?",
        "any": [["backup*"], ["back up"], ["back-up*"], ["backing up"], ["restor*"]],
        "source": "P21-407",
    },
    {
        "q": "What does requirement SD-NFR-01 require of the way software is written?",
        "any": [["secure coding"], ["secure", "development"]],
        "source": "P21-407",
    },
    {
        "q": "Which frameworks does the response cite against requirement CM-NFR-04 for "
             "designing and managing the system?",
        "any": [["pspf"], ["ism"]],
        "source": "P21-407",
    },
    {
        "q": "Which award category did Trellis Data enter in the 2026 ACT Chief "
             "Minister's Export Awards?",
        "any": [["digital and smart"], ["smart technolog*"]],
        "source": "export_award",
    },
]

SYSTEM = (
    "You answer questions about a folder of tender documents. Search it before you "
    "answer. Quote the wording you found and name the file it came from. If you cannot "
    "find something, say so - do not describe what a document probably says."
)

PROMPT = (
    "<task>\n{question}\n</task>\n\n"
    "<corpus>{root}</corpus>\n\n"
    "Search the corpus, then answer. Name the source document."
)


def _term(term: str) -> re.Pattern:
    """A whole word or phrase, or a stem when it ends in `*`.

    Plain substrings were far too generous: "ism" matched "mechanism" and "back" matched
    "background", so a reply saying the search mechanism found nothing scored as having
    found the fact. Every change here narrows a match; none widens one, so the ground
    truth is no easier to meet than it was.
    """
    stem = term.endswith("*")
    body = re.escape(term.rstrip("*"))
    return re.compile(rf"(?<![a-z0-9]){body}" + ("" if stem else r"(?![a-z0-9])"))


def score(answer: str, case: dict) -> dict:
    """Did it find the substance, and did it say where from?

    `any` is a list of alternatives, each of which is a list of terms that must all be
    present. That shape lets a fact be recognised however it is phrased - "SSL" or "TLS"
    for the same requirement - without the ground truth being widened later to fit an
    answer it did not like.

    Cited means the answer names the source document. Any ".pdf" in the reply used to
    count, so naming the wrong PDF scored as a citation.
    """
    text = (answer or "").lower()
    found = any(all(_term(term).search(text) for term in alternative)
                for alternative in case["any"])
    cited = case["source"].lower() in text
    return {"found": found, "cited": cited, "both": found and cited}


def run(model: str, limit: int | None, ask, totals, log=print,
        corpus: str | None = None) -> dict:
    import harness

    corpus = corpus or CORPUS
    if not os.path.isdir(corpus):
        reason = (f"corpus not found: {corpus}. Pass --corpus or set BENCH_CORPUS to the "
                  f"tender folder; the questions mean nothing against anything else.")
        log(f"  SKIPPED: {reason}")
        return {"suite": "documents", "skipped": reason}
    rows = QUESTIONS[:limit] if limit else QUESTIONS
    cases = []
    for index, case in enumerate(rows, 1):
        reply = ask(model, PROMPT.format(question=case["q"], root=corpus),
                    system=SYSTEM, tools=harness.FILE_TOOLS, max_tokens=900, root=corpus)
        totals.add(reply)
        if not reply.ok:
            cases.append(harness.record(reply, q=case["q"]))
            log(f"  [{index}/{len(rows)}] CALL FAILED  {reply.error[:70]}")
            continue
        result = score(reply.text, case)
        cases.append(harness.record(reply, q=case["q"], **result))
        log(f"  [{index}/{len(rows)}] found={'y' if result['found'] else 'n'} "
            f"cited={'y' if result['cited'] else 'n'} {reply.seconds:5.1f}s  "
            f"{case['q'][:46]}")

    scored = harness.answered(cases)

    def rate(key):
        return sum(c[key] for c in scored) / max(1, len(scored))

    return {"suite": "documents", "n": len(rows), "answered": len(scored),
            "found_rate": rate("found"), "cited_rate": rate("cited"),
            "grounded_rate": rate("both"), "cases": cases}
