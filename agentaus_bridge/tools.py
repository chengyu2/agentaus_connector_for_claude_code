"""Tools the bridge executes itself, on Agentaus turns only.

Claude Code owns every other tool: it sends the schemas, the bridge translates the
calls, and the client runs them. These are different. The bridge appends its own tool
to the list Agentaus sees, and when Agentaus calls it the bridge runs it and feeds the
result straight back - the `tool_use` never reaches Claude Code at all.

Why bother, when Claude Code already offers `Grep`: because `Grep` is a regex, and a
smaller model writes a regex against a *guess* about what the code looks like. It then
gets a plausible match and answers from it. `augment.py` already names this failure and
asks the model not to make it; instruction cannot fix it while the only search tool on
offer is pattern matching.

`agentaus_search` instead reads by meaning. It shortlists files cheaply, splits them
into chunks, and asks Agentaus about every chunk in parallel - the same fan-out the
compactor uses for summarising, pointed at a different question. The words in the query
never have to appear in the answer.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import time
from typing import Awaitable, Callable

from .compact import _chunk, normalise_identifiers
from .config import settings
from .gate import hold
from .tokens import count_tokens

log = logging.getLogger("agentaus-bridge")

Caller = Callable[[str], Awaitable[str]]

# A term present in more than this fraction of files is treated as noise for ranking.
_TERM_UBIQUITY_CEILING = 0.5

# The largest helper prompt this upstream has actually proved it can answer.
#
# Agentaus sits behind Cloudflare, which answers 524 when the origin takes too long. How
# long a prompt takes depends on how busy the origin is, so the safe size is not a
# constant: 48k tokens answered in 23 seconds when measured alone, then produced 524s
# under two concurrent searches. Retrying the same oversized prompt just times out again,
# which is how a batch job burned four retries and reported failure.
#
# So the bridge learns it, exactly as it already learns the context window from the error
# message that announces it. Halve on a capacity failure, recover slowly on success, and
# never go below a size small enough to be answerable at all.
_MIN_CHUNK_TOKENS = 3000
_learned_chunk_ceiling: int | None = None


def effective_chunk_tokens() -> int:
    """The chunk size to actually use: configured, capped by what has been proven."""
    configured = max(_MIN_CHUNK_TOKENS, settings.agentaus_search_chunk_tokens)
    if _learned_chunk_ceiling is None:
        return configured
    return min(configured, _learned_chunk_ceiling)


def note_capacity_failure(prompt_tokens: int) -> None:
    """Record that a prompt this large was too slow for the upstream right now."""
    global _learned_chunk_ceiling
    halved = max(_MIN_CHUNK_TOKENS, prompt_tokens // 2)
    if _learned_chunk_ceiling is None or halved < _learned_chunk_ceiling:
        _learned_chunk_ceiling = halved
        log.warning(
            "upstream timed out on a %d-token prompt; capping search chunks at %d "
            "tokens from now on", prompt_tokens, halved,
        )


def note_capacity_success(prompt_tokens: int) -> None:
    """Let the ceiling drift back up when large prompts are being answered again.

    Recovery is deliberately slower than the backoff: load comes and goes, and a ceiling
    that snaps straight back re-learns the same failure every time it lifts.
    """
    global _learned_chunk_ceiling
    if _learned_chunk_ceiling is None:
        return
    if prompt_tokens >= _learned_chunk_ceiling * 0.8:
        raised = int(_learned_chunk_ceiling * 1.25)
        if raised >= settings.agentaus_search_chunk_tokens:
            _learned_chunk_ceiling = None
            log.info("upstream is answering full-size prompts again; cap lifted")
        else:
            _learned_chunk_ceiling = raised


def reset_learned_capacity() -> None:
    """For tests: module state that would otherwise leak between cases."""
    global _learned_chunk_ceiling
    _learned_chunk_ceiling = None


def is_capacity_failure(exc: BaseException) -> bool:
    """Whether a failure means "that prompt was too big for now", not "that was wrong".

    524 is Cloudflare's origin timeout; the bridge's own helper timeout says the same
    thing from this side of the connection.
    """
    text = str(exc).lower()
    return any(m in text for m in ("524", "522", "504", "timed out", "timeout"))

SEARCH_TOOL = "agentaus_search"
WEB_SEARCH_TOOL = "agentaus_web_search"
INVESTIGATE_TOOL = "agentaus_investigate"
ZOOM_TOOL = "agentaus_zoom"

# Names the bridge answers itself. Anything else is Claude Code's and is passed through.
BRIDGE_TOOLS = {SEARCH_TOOL, WEB_SEARCH_TOOL, INVESTIGATE_TOOL, ZOOM_TOOL}


SEARCH_SCHEMA = {
    "name": SEARCH_TOOL,
    "description": (
        "Search a file tree for information by meaning rather than by pattern. Ask in "
        "plain language - 'where is the retry backoff decided', 'what happens when the "
        "token count is wrong' - and the matching code is read and quoted back with "
        "file paths and line numbers.\n\n"
        "Prefer this over Grep for any question about how something works, where a "
        "behaviour lives, or what handles a case. It finds the answer even when the "
        "words you searched for appear nowhere in the file, which is exactly when a "
        "regex silently returns nothing and you conclude the code does not exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you are looking for, as a plain-language question.",
            },
            "path": {
                "type": "string",
                "description": "Absolute path to the directory or file to search.",
            },
            "glob": {
                "type": "string",
                "description": "Optional filename filter, e.g. '*.py'.",
            },
        },
        "required": ["query", "path"],
    },
}


WEB_SEARCH_SCHEMA = {
    "name": WEB_SEARCH_TOOL,
    "description": (
        "Search the web and answer from what is found. Use this for anything current, "
        "external, or outside this codebase: library documentation, an error message "
        "you do not recognise, a version or release date, an API you have not read.\n\n"
        "This is the only web search available on this model - Claude Code's own "
        "WebSearch is an Anthropic server-side tool and is not reachable from here. "
        "Expect it to take longer than a normal reply, because a real search runs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for, as a plain-language question.",
            },
        },
        "required": ["query"],
    },
}


# Agentaus runs its own web search, but it is triggered by the prompt rather than by a
# parameter: the phrase "web search this" is what turns it on. That makes it invisible
# to an agent loop - the model either happens to say it or does not, and nothing can
# observe or direct the result.
#
# Wrapping it in a tool turns a prompt convention into something the loop can call
# deliberately, see the output of, and cite. The search itself still runs inside
# Agentaus, so nothing leaves the sovereign path to answer it.
WEB_SEARCH_INSTRUCTION = """\
web search this: {query}

<task>
Answer from what the search returns, not from memory. For every fact you state, give the \
source URL it came from. If the search finds nothing useful, say so plainly rather than \
answering from what you already believe - a confident answer from memory is exactly what \
this search was run to avoid.
</task>

<output_format>
Terse. Markdown. Each fact followed by its source URL. No preamble, no tags.
</output_format>
"""


async def run_web_search(query: str, call: Caller) -> str:
    """Execute one `agentaus_web_search` call.

    A single upstream call, so no fan-out and no chunking - but it is slower than an
    ordinary reply because a real search runs behind it. The keepalive pings on the
    streaming path are what stop that looking like a stall.
    """
    query = (query or "").strip()
    if not query:
        return "agentaus_web_search needs a query."
    started_at = time.monotonic()
    try:
        async with hold("web search", "urgent"):
            answer = await call(WEB_SEARCH_INSTRUCTION.format(query=query))
    except Exception as exc:
        log.warning("web search failed for %r (%s)", query[:60], exc)
        return f"[bridge] web search failed: {exc}"
    log.info("web search %r took %.1fs -> %d chars",
             query[:60], time.monotonic() - started_at, len(answer or ""))
    answer = normalise_identifiers((answer or "").strip())
    return answer or f"The web search for {query!r} returned nothing usable."


# Directories that are never worth reading and would swamp the shortlist.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    ".idea", ".vscode", "target", "vendor", ".tox", "site-packages",
}

# Binary and generated content: reading it costs a model call and answers nothing.
_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".bz2", ".xz", ".7z", ".mp4", ".mov", ".mp3", ".wav", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".so", ".dylib", ".dll", ".exe", ".bin", ".class", ".jar", ".pyc",
    ".pyo", ".wasm", ".lock", ".map", ".min.js", ".min.css",
    # Office formats are zip archives. Read as text they are binary noise, and a search
    # will happily chunk that noise and spend a model call on every piece of it -
    # observed burning most of a 120-chunk budget on one 350 KB .docx.
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".ods", ".odp", ".rtf",
    ".sqlite", ".db", ".parquet", ".pkl", ".npy", ".npz",
}

# Never read, regardless of what the model asks for or what the query matches. Secrets
# do not stop being secrets because a search term happened to appear in them, and the
# model has no reason to need them to answer a question about code.
_NEVER_READ = (
    ".env", ".env.local", ".env.production", ".netrc", ".htpasswd",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials",
)
_NEVER_READ_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith(".env"):
        return True
    return lowered in _NEVER_READ or lowered.endswith(_NEVER_READ_SUFFIXES)


def _allowed_root(path: str) -> bool:
    """Whether `path` is inside a configured search root.

    An empty AGENTAUS_SEARCH_ROOTS allows anything absolute, which matches what Claude
    Code's own Read tool would let the model open. Setting it confines the bridge, which
    matters because these reads never reach Claude Code's permission prompts.
    """
    roots = [r for r in settings.agentaus_search_roots.split(":") if r.strip()]
    if not roots:
        return True
    resolved = os.path.realpath(path)
    return any(
        resolved == os.path.realpath(r) or resolved.startswith(os.path.realpath(r) + os.sep)
        for r in roots
    )


def enumerate_files(path: str, glob: str | None = None) -> list[str]:
    """Readable text files under `path`, in a stable order."""
    if os.path.isfile(path):
        name = os.path.basename(path).lower()
        # Naming a binary explicitly does not make it readable as text, so the same
        # exclusions apply to a direct path as to a walked one.
        if _is_secret(name) or any(name.endswith(suffix) for suffix in _SKIP_SUFFIXES):
            return []
        return [path]

    found: list[str] = []
    for directory, subdirs, names in os.walk(path):
        # Pruned in place so os.walk does not descend into them at all.
        subdirs[:] = sorted(d for d in subdirs if d not in _SKIP_DIRS and not d.startswith(".cache"))
        for name in sorted(names):
            if _is_secret(name):
                continue
            lowered = name.lower()
            if any(lowered.endswith(suffix) for suffix in _SKIP_SUFFIXES):
                continue
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(directory, name)
            try:
                if os.path.getsize(full) > settings.agentaus_search_max_file_bytes:
                    continue
            except OSError:
                continue
            found.append(full)
    return found


def read_text(path: str) -> str:
    """File contents, or empty string if it cannot be read as text."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


EXPAND_INSTRUCTION = """\
A developer is searching a codebase for the answer to the question below.

<question>
{query}
</question>

<task>
List the literal strings most likely to appear in source code that answers it: function \
and variable names, class names, config keys, library calls, error text. Include \
synonyms and the terms a programmer would actually type, not the words of the question.

Prefer specific terms over general ones. A word like "max", "call" or "limit" appears in \
every file of every codebase and tells the search nothing.
</task>

<output_format>
5 to 10 terms, one per line, nothing else. No numbering, no explanation, no tags.
</output_format>
"""


CHUNK_INSTRUCTION = """\
<question>
{query}
</question>

<excerpt file="{path}" lines="{start}-{end}">
{body}
</excerpt>

<task>
Decide whether the excerpt contains anything that answers the question.

If it does: quote the relevant lines verbatim with their line numbers, then add one \
short sentence saying how they answer it. Quote only what is relevant, never the whole \
excerpt.

If it does not: reply with exactly NONE and nothing else.
</task>

<output_format>
Either the quoted lines, or the single word NONE. No tags, no preamble.
</output_format>
"""


MERGE_HITS_INSTRUCTION = """\
<question>
{query}
</question>

<excerpts>
{body}
</excerpts>

<task>
Each excerpt above was already judged relevant. Combine them into one answer.

Keep every file path and line number exactly as written. Order them so the most direct \
answer comes first. Remove repetition, but never drop a location that appears only once. \
Add nothing that is not above.
</task>

<output_format>
The combined answer. No tags, no preamble.
</output_format>
"""


async def expand_query(query: str, call: Caller) -> list[str]:
    """Literal terms worth scanning for, from a plain-language question.

    This is what keeps the prefilter from being a plain keyword match: the question
    "where do we cap concurrent calls" contains none of the words that appear in the
    code that does it, and expansion is what bridges that.
    """
    try:
        async with hold("query expansion", "urgent"):
            raw = await call(EXPAND_INSTRUCTION.format(query=query))
    except Exception as exc:
        log.warning("query expansion failed (%s); falling back to the query's own words", exc)
        raw = ""

    terms = [
        normalise_identifiers(line).strip().strip("`\"'*-").strip()
        for line in (raw or "").splitlines()
    ]
    terms = [t for t in terms if 2 <= len(t) <= 60]
    # The question's own words are still worth scanning for, and are the only terms
    # available when expansion failed.
    terms += [w for w in query.replace("_", " ").split() if len(w) > 3]
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique[:20]


def selective_terms(texts: list[str], terms: list[str]) -> list[str]:
    """The subset of `terms` that actually distinguishes one text from another.

    A term present in more than half the texts carries no signal: expansion returns the
    words a programmer would type, and some of those - "document", "max", "call" - are
    in almost everything. Ranking on them ranks nothing.

    Measured over whatever is being ranked, not always over files. Ranking chunks with
    selectivity computed across files is meaningless when the corpus is ONE file: there
    is nothing for a term to be ubiquitous *across*, so every term survives and every
    chunk scores. That is how a 434 KB document came to be read 29 chunks out of 29.
    """
    if not texts:
        return list(terms)
    ceiling = max(1, int(len(texts) * _TERM_UBIQUITY_CEILING))
    keep = []
    for term in terms:
        matches = sum(1 for body in texts if term in body)
        if 0 < matches <= ceiling:
            keep.append(term)
    # Every term ubiquitous means ranking on them is still better than not ranking.
    return keep or list(terms)


def shortlist(files: list[str], terms: list[str]) -> list[tuple[str, int]]:
    """Files containing an expanded term, ranked by how many distinct terms hit.

    A plain substring scan in Python rather than a ripgrep subprocess: `rg` is a shell
    function provided by the Claude Code extension and is not on a clean PATH, so the
    launchd-managed bridge cannot see it. At repo scale the difference does not matter.

    Terms that appear in nearly every file are dropped before ranking. Expansion returns
    words a programmer would type, and some of those - "max", "limit", "call" - are in
    every file of any codebase. Counting them ranks nothing and shortlists everything:
    on a live search they turned 40 files into 21 "candidates" and a 75-second fan-out.
    """
    lowered = [t.lower() for t in terms]
    bodies: dict[str, str] = {}
    for path in files:
        body = read_text(path).lower()
        if body:
            bodies[path] = body

    if not bodies:
        return []

    selective = selective_terms(list(bodies.values()), lowered)

    scored = []
    for path, body in bodies.items():
        hits = sum(1 for term in selective if term in body)
        if hits:
            scored.append((path, hits))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def chunk_file(path: str, budget_tokens: int) -> list[tuple[str, int, int, str]]:
    """Split one file into (path, start_line, end_line, body) pieces."""
    body = read_text(path)
    if not body.strip():
        return []
    pieces = _chunk(body, budget_tokens)
    out: list[tuple[str, int, int, str]] = []
    line = 1
    for piece in pieces:
        span = piece.count("\n") + 1
        out.append((path, line, line + span - 1, piece))
        line += span
    return out


async def run_search(
    query: str, path: str, glob: str | None, call: Caller, default_path: str | None = None
) -> str:
    """Execute one `agentaus_search` call and return text for the tool result.

    A missing or relative `path` falls back to the repository Claude Code named in its
    system prompt. Refusing instead would spend a whole tool round teaching the model
    something the bridge already knows.
    """
    if (not path or not os.path.isabs(path)) and default_path:
        resolved = default_path if not path else os.path.join(default_path, path)
        log.info("search path %r resolved against the working directory -> %s",
                 path, resolved)
        path = resolved
    if not os.path.isabs(path):
        return (f"agentaus_search needs an absolute path; got {path!r}. "
                f"Pass the repository's absolute path as `path`.")
    if not os.path.exists(path):
        return f"No such path: {path}"
    if not _allowed_root(path):
        return (
            f"{path} is outside AGENTAUS_SEARCH_ROOTS, which this bridge is confined to."
        )

    files = enumerate_files(path, glob)
    if not files:
        return f"No readable files under {path}" + (f" matching {glob}" if glob else "") + "."

    terms = await expand_query(query, call)
    ranked = shortlist(files, terms)
    candidates = [p for p, _ in ranked]

    # A thin shortlist means the words are simply not there - which is the case a
    # keyword search gets wrong, not a sign that nothing matches. Read everything.
    brute_forced = False
    if len(candidates) < settings.agentaus_search_min_candidates:
        candidates = files
        brute_forced = True
    elif len(candidates) > settings.agentaus_search_max_candidates:
        # Ranked, so this keeps the files that matched the most distinct terms.
        candidates = candidates[: settings.agentaus_search_max_candidates]

    budget = effective_chunk_tokens()
    chunks: list[tuple[str, int, int, str]] = []
    for candidate in candidates:
        chunks.extend(chunk_file(candidate, budget))

    # Rank chunks the same way files were ranked. Shortlisting files is not enough when
    # the corpus is one enormous file: a 434 KB tender document is a single candidate
    # that still costs 27 model calls, and 22 of those chunks contain none of the terms
    # being looked for. Measured: 285 seconds before this, and the answer call after it
    # then ran past the client's patience.
    total = len(chunks)
    if total > settings.agentaus_search_min_candidates:
        bodies = [piece[3].lower() for piece in chunks]
        # Selectivity recomputed ACROSS CHUNKS. Reusing the file-level judgement here is
        # what made this filter a no-op on a single-file corpus.
        selective = selective_terms(bodies, [t.lower() for t in terms])
        scored = []
        for piece, body in zip(chunks, bodies):
            hits = sum(1 for term in selective if term in body)
            if hits:
                scored.append((hits, piece))
        # Same reasoning as the file shortlist: too few hits means the words are absent,
        # not the answer, so read everything rather than trusting the filter.
        if len(scored) >= settings.agentaus_search_min_candidates:
            scored.sort(key=lambda pair: -pair[0])
            chunks = [piece for _hits, piece in scored]

    cap = settings.agentaus_search_max_chunks
    dropped = max(0, len(chunks) - cap)
    chunks = chunks[:cap]

    log.info(
        "search %r: %d file(s) -> %d candidate(s)%s -> %d of %d chunk(s)%s",
        query[:60], len(files), len(candidates),
        " (brute force: shortlist too thin)" if brute_forced else "",
        len(chunks), total, f", {dropped} over the cap" if dropped else "",
    )

    if not chunks:
        return f"Nothing readable to search under {path}."

    async def look(piece: tuple[str, int, int, str], depth: int = 0) -> str:
        chunk_path, start, end, body = piece
        prompt = CHUNK_INSTRUCTION.format(
            path=chunk_path, start=start, end=end, query=query, body=body
        )
        size = count_tokens(prompt)
        try:
            async with hold("search chunk", "urgent"):
                answer = await call(prompt)
        except Exception as exc:
            if is_capacity_failure(exc) and depth < 2 and len(body.splitlines()) > 4:
                # Too big for the upstream as it stands. Halve it and read both halves
                # rather than dropping the excerpt: retrying the same prompt fails the
                # same way, and giving up loses evidence that is genuinely there.
                note_capacity_failure(size)
                lines = body.splitlines()
                middle = len(lines) // 2
                log.warning("search chunk of %d tokens timed out; splitting %s:%d-%d",
                            size, chunk_path, start, end)
                halves = await asyncio.gather(
                    look((chunk_path, start, start + middle - 1,
                          "\n".join(lines[:middle])), depth + 1),
                    look((chunk_path, start + middle, end,
                          "\n".join(lines[middle:])), depth + 1),
                )
                return "\n\n".join(h for h in halves if h.strip())
            log.warning("search chunk failed for %s (%s)", chunk_path, exc)
            return ""
        note_capacity_success(size)
        answer = normalise_identifiers((answer or "").strip())
        if not answer or answer.upper().strip().rstrip(".") == "NONE":
            return ""
        return f"--- {chunk_path}:{start}-{end} ---\n{answer}"

    results = await asyncio.gather(*[look(piece) for piece in chunks])
    hits = [r for r in results if r.strip()]

    header = ""
    if dropped:
        # Said out loud, because a silent cap reads as full coverage.
        header = (
            f"[Searched {len(chunks)} of {len(chunks) + dropped} excerpts - the rest "
            f"were over the {cap}-excerpt limit. Narrow `path` or set a `glob` to cover "
            f"everything.]\n\n"
        )

    if not hits:
        return header + (
            f"No excerpt under {path} answered: {query}\n"
            f"Searched {len(chunks)} excerpt(s) across {len(candidates)} file(s)."
        )

    joined = "\n\n".join(hits)
    if count_tokens(joined) > settings.agentaus_search_chunk_tokens:
        try:
            async with hold("search merge", "urgent"):
                merged = await call(
                    MERGE_HITS_INSTRUCTION.format(query=query, body=joined)
                )
            if merged and merged.strip():
                joined = normalise_identifiers(merged.strip())
        except Exception as exc:
            log.warning("search merge failed (%s); returning the raw hits", exc)

    # Close the loop: a citation is only useful if the model knows it can open it.
    footer = ""
    if settings.agentaus_zoom:
        footer = (
            f"\n\n[Each hit above is a citation, not the whole passage. To quote or "
            f"paraphrase any of it accurately, call `{ZOOM_TOOL}` with that file and line "
            f"number and read it in context first.]"
        )
    return header + joined + footer


INVESTIGATE_SCHEMA = {
    "name": INVESTIGATE_TOOL,
    "description": (
        "Investigate a question about a codebase from several independent angles at "
        "once, and report only what more than one of them agreed on.\n\n"
        "Slower and more thorough than `agentaus_search`. Use it when being wrong would "
        "be expensive: before a refactor, when tracing why something behaves as it "
        "does, or when a single search gave an answer you do not fully believe. The "
        "report separates what was corroborated from what only one angle found."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What you need to establish, as a plain-language question.",
            },
            "path": {
                "type": "string",
                "description": "Absolute path to the directory to investigate.",
            },
        },
        "required": ["question", "path"],
    },
}


# Three angles on the same question. Deliberately different rather than three copies of
# one prompt: redundancy catches a flaky answer, but only diversity catches a failure
# mode a single framing cannot see. "Where is it defined" and "what breaks if it
# changes" fail in different places, which is what makes their agreement worth
# something.
INVESTIGATE_LENSES = (
    ("definition", "Where is this implemented or defined? Name the file and the lines."),
    ("usage", "Where is this called, read or depended on from elsewhere?"),
    ("consequence", "What behaviour depends on this, and what would break if it changed?"),
)


CORROBORATE_INSTRUCTION = """\
Three independent searches were run over the same codebase to answer one question. Each \
looked from a different angle and did not see the others' results.

<question>
{question}
</question>

<reports>
{body}
</reports>

<task>
Produce one report with exactly two sections:

## Established
Facts that appear in TWO OR MORE of the reports below. For each, give the file and line \
numbers, exactly as written.

## Single-source
Facts that appear in only ONE report. These may still be true - one angle often sees \
something the others had no reason to look at - but they were not corroborated, so say \
so rather than presenting them as settled.

Rules:
- Two reports naming the same file and behaviour agree, even in different words.
- Never invent a fact to fill a section. An empty section is a real result: write "none".
- Reproduce every path and identifier EXACTLY, in backticks, with plain ASCII hyphens.
- No preamble.
</task>

<output_format>
Markdown, with the two headings above. No tags.
</output_format>
"""


async def run_investigate(
    question: str, path: str, call: Caller, default_path: str | None = None
) -> str:
    """Answer one question from several angles and report what corroborates.

    The lenses run concurrently and each is an ordinary `run_search`, so the whole thing
    is bounded by the same global cap as everything else - three searches share the six
    slots rather than getting six each.
    """
    question = (question or "").strip()
    if not question:
        return "agentaus_investigate needs a question."
    if (not path or not os.path.isabs(path)) and default_path:
        path = default_path if not path else os.path.join(default_path, path)
    if not os.path.isabs(path):
        return f"agentaus_investigate needs an absolute path; got {path!r}."

    started_at = time.monotonic()
    reports = await asyncio.gather(*[
        run_search(f"{question}\n\nSpecifically: {angle}", path, None, call,
                   default_path)
        for _name, angle in INVESTIGATE_LENSES
    ], return_exceptions=True)

    usable = []
    for (name, _angle), report in zip(INVESTIGATE_LENSES, reports):
        if isinstance(report, Exception):
            log.warning("investigate lens %s failed: %s", name, report)
            continue
        if report and "No excerpt" not in report:
            usable.append(f"### Report: {name}\n{report}")

    log.info("investigate %r: %d of %d lens(es) found something in %.1fs",
             question[:60], len(usable), len(INVESTIGATE_LENSES),
             time.monotonic() - started_at)

    if not usable:
        return (
            f"None of the {len(INVESTIGATE_LENSES)} angles found anything under {path} "
            f"for: {question}"
        )
    if len(usable) == 1:
        # Nothing to corroborate against. Saying so is more useful than a "## Established"
        # heading over a single unverified source, which would read as agreement.
        return ("[Only one of the three angles found anything, so nothing here is "
                "corroborated.]\n\n" + usable[0])

    try:
        async with hold("investigate merge", "urgent"):
            merged = await call(CORROBORATE_INSTRUCTION.format(
                question=question, body="\n\n".join(usable)
            ))
    except Exception as exc:
        log.warning("investigate merge failed (%s); returning the raw reports", exc)
        return "\n\n".join(usable)

    merged = normalise_identifiers((merged or "").strip())
    return merged or "\n\n".join(usable)


ZOOM_SCHEMA = {
    "name": ZOOM_TOOL,
    "description": (
        "Read a cited passage in its full surrounding context. Give it a file and a line "
        "number from an `agentaus_search` result and it returns that passage widened to "
        "its whole section, with line numbers intact.\n\n"
        "Use it whenever you need to WRITE from evidence rather than merely cite it. A "
        "search quotes a line or two - enough to prove something is there, not enough to "
        "paraphrase it accurately or see what it depends on. Zoom in before you commit "
        "to wording."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file."},
            "start_line": {"type": "integer", "description": "First line of interest (1-based)."},
            "end_line": {"type": "integer", "description": "Last line of interest. Defaults to start_line."},
            "purpose": {
                "type": "string",
                "description": (
                    "What you need from the passage. Only used if the section is too "
                    "large to return whole, in which case it decides what is kept."
                ),
            },
        },
        "required": ["file_path", "start_line"],
    },
}


# A line that opens a new section: a Markdown heading, a bold-only line used as one, an
# underline rule, or a numbered/lettered clause of the kind tenders and specs are made
# of. Widening to one of these is what makes a passage readable rather than merely
# larger - a window cut at an arbitrary offset starts mid-sentence and loses the heading
# that says what the passage is about.
_SECTION_START = re.compile(
    r"^\s*(?:#{1,6}\s|\*\*[^*]+\*\*\s*$|={3,}\s*$|-{3,}\s*$"
    r"|\d+(?:\.\d+)*[.)]\s|[A-Z]\)\s|\[\d+\]\s*\*\*)"
)


def _widen_to_section(
    lines: list[str], start_idx: int, end_idx: int, radius: int, ceiling: int,
    floor_lines: int = 0,
) -> tuple[int, int]:
    """Grow [start_idx, end_idx) outward to the section containing it.

    Prefers a real boundary within `radius`; falls back to a blank line; failing both,
    takes the raw radius. `ceiling` is a hard stop so one heading-less file cannot
    return itself in full.
    """
    lo = start_idx
    floor_ = max(0, start_idx - radius)
    for i in range(start_idx, floor_ - 1, -1):
        if i < start_idx and _SECTION_START.match(lines[i]):
            lo = i
            break
    else:
        for i in range(start_idx, floor_ - 1, -1):
            if i < start_idx and not lines[i].strip():
                lo = i + 1
                break
        else:
            lo = floor_

    hi = end_idx
    limit = min(len(lines), end_idx + radius)
    for i in range(end_idx, limit):
        if _SECTION_START.match(lines[i]):
            hi = i
            break
    else:
        for i in range(limit - 1, end_idx - 1, -1):
            if not lines[i].strip():
                hi = i
                break
        else:
            hi = limit

    # A boundary can sit two lines away. Tender and spec documents use a bold single
    # line as a sub-heading constantly, so honouring the nearest boundary alone returned
    # three-line "sections" - technically a section, useless to write from. Keep going
    # outward, boundary by boundary, until there is enough to read.
    if floor_lines and hi - lo < floor_lines:
        want = floor_lines - (hi - lo)
        back = min(want // 2, lo - max(0, start_idx - radius))
        lo = max(0, lo - back)
        hi = min(len(lines), hi + (want - back))
        # Prefer stopping on a boundary if one is within reach of the new edge.
        for i in range(hi, min(len(lines), hi + 20)):
            if _SECTION_START.match(lines[i]):
                hi = i
                break

    if hi - lo > ceiling:
        # Keep the cited range centred rather than truncating one side of it.
        overflow = (hi - lo) - ceiling
        trim = overflow // 2
        lo, hi = lo + trim, hi - (overflow - trim)
        lo = min(lo, start_idx)
        hi = max(hi, end_idx)
    return lo, hi


def _trim_to_budget(
    lines: list[str], lo: int, hi: int, start_idx: int, end_idx: int, budget_tokens: int
) -> tuple[int, int]:
    """Shrink [lo, hi) to fit a token budget, keeping the cited lines.

    A line-count ceiling is the wrong unit when line lengths vary by an order of
    magnitude: 150 lines is 6,000 characters of source code and 47,000 characters of
    tender prose. `compact._chunk` already learned this - "sizing is by characters
    throughout, never by line count" - and zoom was ignoring it, which is how a passage
    came back at 25,000 characters and broke the turn carrying it.

    The cited lines are never trimmed away; if they alone exceed the budget the caller
    gets them and nothing else.
    """
    def cost(a: int, b: int) -> int:
        return count_tokens("\n".join(lines[a:b]))

    if cost(lo, hi) <= budget_tokens:
        return lo, hi

    # Grow outward from the citation instead of shrinking inward: that way the lines
    # nearest the quote are the ones kept.
    low, high = max(lo, start_idx), min(hi, max(end_idx, start_idx + 1))
    while True:
        widened = False
        if low > lo and cost(low - 1, high) <= budget_tokens:
            low -= 1
            widened = True
        if high < hi and cost(low, high + 1) <= budget_tokens:
            high += 1
            widened = True
        if not widened:
            break
    return low, high


ZOOM_INSTRUCTION = """\
<passage file="{path}" lines="{start}-{end}">
{body}
</passage>

<purpose>
{purpose}
</purpose>

<task>
The passage is too long to return whole. Keep the parts that serve the purpose above and
drop the rest.

Keep verbatim, with their line numbers: every sentence that bears on the purpose, every
identifier, certification, standard, product name, number and date. Where you drop
material, say what you dropped on one line.
</task>

<output_format>
The kept lines, each still prefixed with its original line number. Then one
"[dropped: ...]" line. No tags, no preamble.
</output_format>
"""


async def run_zoom(
    file_path: str,
    start_line: int,
    end_line: int | None,
    purpose: str,
    call: Caller,
    default_path: str | None = None,
) -> str:
    """Return a cited passage widened to its section, with line numbers preserved."""
    if (not file_path or not os.path.isabs(file_path)) and default_path:
        file_path = default_path if not file_path else os.path.join(default_path, file_path)
    if not os.path.isabs(file_path):
        return f"agentaus_zoom needs an absolute path; got {file_path!r}."
    if not _allowed_root(file_path):
        return f"{file_path} is outside AGENTAUS_SEARCH_ROOTS."
    if not enumerate_files(file_path):
        return (f"{file_path} cannot be read as text (missing, binary, or excluded). "
                f"Office documents are zip archives - extract them first.")

    body = read_text(file_path)
    if not body.strip():
        return f"{file_path} is empty."
    lines = body.splitlines()

    try:
        start = max(1, int(start_line))
    except (TypeError, ValueError):
        return f"agentaus_zoom needs an integer start_line; got {start_line!r}."
    end = start
    try:
        if end_line:
            end = max(start, int(end_line))
    except (TypeError, ValueError):
        end = start
    if start > len(lines):
        return f"{file_path} has {len(lines)} lines; {start} is past the end."

    lo, hi = _widen_to_section(
        lines, start - 1, min(end, len(lines)),
        settings.agentaus_zoom_radius_lines, settings.agentaus_zoom_max_lines,
        settings.agentaus_zoom_min_lines,
    )
    # Lines decide WHERE the section starts and ends; tokens decide how much of it fits.
    lo, hi = _trim_to_budget(
        lines, lo, hi, start - 1, min(end, len(lines)),
        settings.agentaus_zoom_max_tokens,
    )
    window = lines[lo:hi]
    numbered = "\n".join(f"{lo + i + 1:6d}  {line}" for i, line in enumerate(window))

    header = (
        f"{file_path}:{lo + 1}-{hi}  (you asked for {start}-{end}; widened to the "
        f"surrounding section)\n"
    )

    if count_tokens(numbered) <= settings.agentaus_zoom_max_tokens:
        # Verbatim wherever it fits. Condensing a passage the caller is about to quote
        # from would defeat the point of zooming in on it.
        log.info("zoom %s:%d-%d -> %d line(s) verbatim", file_path, start, end, hi - lo)
        return header + numbered

    prompt = ZOOM_INSTRUCTION.format(
        path=file_path, start=lo + 1, end=hi, body=numbered,
        purpose=purpose or "understand this passage well enough to quote it accurately",
    )
    try:
        async with hold("zoom", "urgent"):
            kept = await call(prompt)
    except Exception as exc:
        # Truncating verbatim is a better answer than a condensation that never arrives,
        # and the learned ceiling is told about it so the next caller aims lower.
        if is_capacity_failure(exc):
            note_capacity_failure(count_tokens(prompt))
        log.warning("zoom condensation failed (%s); truncating instead", exc)
        budget = settings.agentaus_zoom_max_tokens * 4
        return header + numbered[:budget] + "\n[truncated: the section is larger than the limit]"

    kept = normalise_identifiers((kept or "").strip())
    log.info("zoom %s:%d-%d -> %d line(s) condensed", file_path, start, end, hi - lo)
    return header + (kept or numbered[: settings.agentaus_zoom_max_tokens * 4])


async def execute(
    name: str, arguments: dict, call: Caller, default_path: str | None = None
) -> str:
    """Run one bridge-owned tool call. Never raises - a failure becomes tool output."""
    try:
        if name == SEARCH_TOOL:
            return await run_search(
                str(arguments.get("query") or ""),
                str(arguments.get("path") or ""),
                (arguments.get("glob") or None),
                call,
                default_path,
            )
        if name == WEB_SEARCH_TOOL:
            return await run_web_search(str(arguments.get("query") or ""), call)
        if name == ZOOM_TOOL:
            return await run_zoom(
                str(arguments.get("file_path") or ""),
                arguments.get("start_line"),
                arguments.get("end_line"),
                str(arguments.get("purpose") or ""),
                call,
                default_path,
            )
        if name == INVESTIGATE_TOOL:
            return await run_investigate(
                str(arguments.get("question") or ""),
                str(arguments.get("path") or ""),
                call,
                default_path,
            )
        return f"[bridge] Unknown tool {name!r}."
    except Exception as exc:  # a broken tool must not end the turn
        log.warning("bridge tool %s failed: %s", name, exc)
        return f"[bridge] {name} failed: {exc}"
