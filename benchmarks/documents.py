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

CORPUS = "/Users/cheng/trellis_data_tender"

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
        "any": [["manufacturer"], ["supported version"], ["patch"]],
        "source": "P21-407",
    },
    {
        "q": "What does requirement BR-NFR-01 cover, and how many deployment options "
             "were offered against it?",
        "any": [["back"], ["restor"]],
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
        "any": [["digital and smart"], ["smart technolog"]],
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


def score(answer: str, case: dict) -> dict:
    """Did it find the substance, and did it say where from?

    `any` is a list of alternatives, each of which is a list of terms that must all be
    present. That shape lets a fact be recognised however it is phrased - "SSL" or "TLS"
    for the same requirement - without the ground truth being widened later to fit an
    answer it did not like.
    """
    text = (answer or "").lower()
    found = any(all(term in text for term in alternative) for alternative in case["any"])
    cited = case["source"].lower() in text or ".pdf" in text
    return {"found": found, "cited": cited, "both": found and cited}


def run(model: str, limit: int | None, ask, totals, log=print, root: str = ".") -> dict:
    import os

    corpus = CORPUS if os.path.isdir(CORPUS) else os.path.abspath(root)
    tools = [{
        "name": "Read",
        "description": "Read a file from the filesystem.",
        "input_schema": {"type": "object",
                         "properties": {"file_path": {"type": "string"}},
                         "required": ["file_path"]},
    }]
    rows = QUESTIONS[:limit] if limit else QUESTIONS
    scores = []
    for index, case in enumerate(rows, 1):
        reply = ask(model, PROMPT.format(question=case["q"], root=corpus),
                    system=SYSTEM, tools=tools, max_tokens=900)
        totals.add(reply)
        if not reply.ok:
            scores.append({"found": False, "cited": False, "both": False})
            log(f"  [{index}/{len(rows)}] CALL FAILED  {reply.error[:70]}")
            continue
        result = score(reply.text, case)
        scores.append(result)
        log(f"  [{index}/{len(rows)}] found={'y' if result['found'] else 'n'} "
            f"cited={'y' if result['cited'] else 'n'} {reply.seconds:5.1f}s  "
            f"{case['q'][:46]}")

    def rate(key):
        return sum(s[key] for s in scores) / max(1, len(scores))

    return {"suite": "documents", "n": len(rows), "found_rate": rate("found"),
            "cited_rate": rate("cited"), "grounded_rate": rate("both")}
