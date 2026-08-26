"""Localisation precision: does the model find the right file, and what did it cost?

A resolve rate says whether an answer was right. It says nothing about whether the model
looked in the right place, and the literature is consistent that models favour recall over
precision - retrieving a great deal of code and using little of it. For a bridge whose
whole search design is "aim before you read", that is the number that matters.

Ground truth is this repository, because it is the one codebase whose contents can be
asserted without guessing. Each question has a known answer file and a known symbol, and
the questions are deliberately worded so that the answer shares no vocabulary with them -
which is exactly the case a grep cannot serve and a semantic search must.
"""
from __future__ import annotations

QUESTIONS = [
    {
        "q": "Where does this project stop too many background requests running at once?",
        "files": {"agentaus_bridge/gate.py"},
        "symbols": {"_PriorityGate", "max_concurrency", "hold"},
    },
    {
        # Two places genuinely learn a limit from the upstream: server.py learns the
        # context window from the error that announces it, and tools.py learns the
        # largest prompt the upstream will answer under load. The first version of this
        # question expected only server.py and scored a correct answer as wrong - a flaw
        # in the ground truth, not in the model, and worth recording as one.
        "q": "How does the code find out the real size limit the upstream service will accept?",
        "files": {"agentaus_bridge/server.py", "agentaus_bridge/tools.py"},
        "symbols": {"_learn_limit_from", "_MAX_MODEL_LEN", "_context_limit",
                    "note_capacity_failure", "effective_chunk_tokens"},
    },
    {
        "q": "What decides whether a turn gets to work out a plan before it answers?",
        "files": {"agentaus_bridge/augment.py"},
        "symbols": {"should_think", "mid_tool_loop"},
    },
    {
        "q": "Where does a Word file get turned into something readable?",
        "files": {"agentaus_bridge/documents.py"},
        "symbols": {"extract", "html_to_text", "soffice"},
    },
    {
        "q": "What happens when one piece of a lookup takes too long to come back?",
        "files": {"agentaus_bridge/tools.py"},
        "symbols": {"note_capacity_failure", "is_capacity_failure", "effective_chunk_tokens"},
    },
    {
        "q": "Where is the older part of a long exchange condensed so it still fits?",
        "files": {"agentaus_bridge/compact.py"},
        "symbols": {"summarise_head", "split_head_tail", "ConversationCompactor"},
    },
    {
        "q": "How does the project avoid asking the same expensive question twice?",
        "files": {"agentaus_bridge/compact.py", "agentaus_bridge/server.py",
                  "agentaus_bridge/documents.py"},
        "symbols": {"_find_prefix", "_call_signature", "_cache"},
    },
    {
        "q": "What stops a reply that claims something it has no basis for?",
        "files": {"agentaus_bridge/augment.py", "agentaus_bridge/server.py"},
        "symbols": {"GROUNDING_INSTRUCTION", "_check_grounding", "grounding_verdict"},
    },
]

SYSTEM = ("You are a code navigator. You answer with locations, not explanations. Use the "
          "tools you are given to find out; never guess a path.")

PROMPT = """<question>
{question}
</question>

<repository>
{root}
</repository>

<task>
Find where this is handled. Use `agentaus_search` on the repository path - do not guess.
</task>

<output_format>
One line per location, exactly: <relative/path.py>:<symbol name>
At most 4 lines. Nothing else.
</output_format>"""


def score(reply: str, expected_files: set, expected_symbols: set) -> dict:
    """Precision and recall over the files named, plus whether a right symbol appeared."""
    named = set()
    for line in (reply or "").splitlines():
        for part in line.replace("`", " ").split():
            if part.endswith(".py") or ".py:" in part:
                named.add(part.split(":")[0].lstrip("./").strip(","))
    hit = named & expected_files
    precision = len(hit) / len(named) if named else 0.0
    recall = len(hit) / len(expected_files) if expected_files else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    symbol_hit = any(s.lower() in (reply or "").lower() for s in expected_symbols)
    return {"named": sorted(named), "precision": precision, "recall": recall,
            "f1": f1, "symbol": symbol_hit}


def run(model: str, limit: int | None, ask, totals, log=print, root: str = ".") -> dict:
    import os
    root = os.path.abspath(root)
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
        reply = ask(model, PROMPT.format(question=case["q"], root=root),
                    system=SYSTEM, tools=tools, max_tokens=512)
        totals.add(reply)
        if not reply.ok:
            scores.append({"precision": 0.0, "recall": 0.0, "f1": 0.0, "symbol": False,
                           "named": []})
            log(f"  [{index}/{len(rows)}] CALL FAILED  {reply.error[:70]}")
            continue
        result = score(reply.text, case["files"], case["symbols"])
        scores.append(result)
        log(f"  [{index}/{len(rows)}] P={result['precision']:.2f} R={result['recall']:.2f} "
            f"F1={result['f1']:.2f} sym={'y' if result['symbol'] else 'n'} "
            f"{reply.seconds:5.1f}s  {','.join(result['named'])[:52]}")

    def mean(key):
        return sum(s[key] for s in scores) / max(1, len(scores))

    return {"suite": "retrieval", "n": len(rows),
            "precision": mean("precision"), "recall": mean("recall"), "f1": mean("f1"),
            "symbol_rate": sum(s["symbol"] for s in scores) / max(1, len(scores))}
