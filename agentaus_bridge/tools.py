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

from . import documents
from . import outline
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
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz", ".tar",
    ".bz2", ".xz", ".7z", ".mp4", ".mov", ".mp3", ".wav", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".so", ".dylib", ".dll", ".exe", ".bin", ".class", ".jar", ".pyc",
    ".pyo", ".wasm", ".lock", ".map", ".min.js", ".min.css",
    # Office formats are zip archives, so reading them as text is noise. They are NOT
    # in this set: `documents.extract` converts them with LibreOffice instead, which is
    # where requirements matrices and tender responses actually live. They fall back to
    # being skipped only when LibreOffice is unavailable - see _skipped_suffixes().
    ".sqlite", ".db", ".parquet", ".pkl", ".npy", ".npz",
}


def _skipped_suffixes() -> set:
    """Suffixes to walk past.

    Formats we can extract are not skipped; formats we cannot are. Office documents and
    PDFs are asked about separately because they need different tools, and a machine
    with poppler but no LibreOffice can read one and not the other.
    """
    skipped = set(_SKIP_SUFFIXES)
    if not documents.available("x.docx"):
        skipped |= documents.OFFICE_SUFFIXES
    if not documents.available("x.pdf"):
        skipped |= documents.PDF_SUFFIXES
    return skipped

# Never read, regardless of what the model asks for or what the query matches. Secrets
# do not stop being secrets because a search term happened to appear in them, and the
# model has no reason to need them to answer a question about code.
_NEVER_READ = (
    ".env", ".env.local", ".env.production", ".netrc", ".htpasswd",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials",
)
_NEVER_READ_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")


# Names that are a private key whatever is appended to them. `id_rsa.bak` and
# `id_rsa.old` are the same key as `id_rsa`; matching the full name alone missed both.
_NEVER_READ_STEMS = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_xmss")

# `credentials` is the awkward one. `credentials.json` is a cloud service account and
# must never be read; `Credentials.pdf` in a tender folder is a capability statement and
# must be. So the extension decides, rather than blocking a word that means two things.
_SECRET_CONFIG_SUFFIXES = (".json", ".yml", ".yaml", ".ini", ".cfg", ".conf",
                           ".toml", ".env", ".csv", ".txt", ".xml", ".properties")
_SECRET_STEMS_IN_CONFIG = ("credentials", "credential", "secrets", "secret",
                           "service-account", "service_account", "apikey", "api_key")


def _is_secret(name: str) -> bool:
    """Whether a file must never be read, however the search happened to match it.

    These reads never reach Claude Code's permission prompts, so this is the only thing
    standing between a search term that happens to appear in a key file and that key
    ending up in a transcript.

    Matching the whole filename was not enough: it let through `credentials.json`, which
    is a Google service account, and `id_rsa.bak`, which is a private key with four
    characters after it.
    """
    lowered = name.lower()
    if lowered.startswith(".env"):
        return True
    if lowered in _NEVER_READ or lowered.endswith(_NEVER_READ_SUFFIXES):
        return True
    stem = lowered.rsplit(".", 1)[0] if "." in lowered else lowered
    if stem in _NEVER_READ_STEMS:
        return True
    return stem in _SECRET_STEMS_IN_CONFIG and lowered.endswith(_SECRET_CONFIG_SUFFIXES)


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
        if _is_secret(name) or any(name.endswith(s) for s in _skipped_suffixes()):
            return []
        return [path]

    skipped = _skipped_suffixes()
    found: list[str] = []
    for directory, subdirs, names in os.walk(path):
        # Pruned in place so os.walk does not descend into them at all.
        subdirs[:] = sorted(d for d in subdirs if d not in _SKIP_DIRS and not d.startswith(".cache"))
        for name in sorted(names):
            if _is_secret(name):
                continue
            lowered = name.lower()
            if any(lowered.endswith(suffix) for suffix in skipped):
                continue
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(directory, name)
            # Documents are measured on a different scale: a .pptx is mostly embedded
            # images, so its size on disk says nothing about how much text it holds.
            limit = (settings.agentaus_search_max_document_bytes
                     if documents.is_office_document(full)
                     else settings.agentaus_search_max_file_bytes)
            try:
                if os.path.getsize(full) > limit:
                    continue
            except OSError:
                continue
            found.append(full)
    return found


def read_text(path: str) -> str:
    """File contents as text, converting office documents on the way through.

    One entry point for every reader in this module, so search, zoom and the file
    shortlist all see a .docx the same way: as its text, with table rows on one line and
    cells separated by ` | `.

    Secrets are refused here as well as during enumeration. Every current caller reaches
    this through `enumerate_files`, which already excludes them, so this guard is
    redundant today - which is the point. It is one line, it costs nothing, and it means
    a future caller that reads a path the model supplied cannot become a key disclosure.
    """
    if _is_secret(os.path.basename(path)):
        log.warning("refused to read %s: excluded as a secret", os.path.basename(path))
        return ""
    if documents.is_office_document(path) and documents.available(path):
        return documents.extract(path)
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


async def _aim_with_outline(
    query: str, candidates: list[str], call: Caller
) -> list[tuple[str, int]]:
    """Ask the model which sections to read, from structure alone.

    The outline costs nothing - no model call, no upstream request - and answers the
    cheap half of the problem: "which part of this document is about X" follows from its
    headings. One call over a table of contents replaces a call per chunk over content.

    Returns [] on anything unexpected, which falls back to reading every chunk. Aiming is
    an optimisation; missing the answer is not an acceptable price for it.
    """
    toc = outline.render(candidates, read=read_text)
    if not toc.strip():
        return []
    try:
        async with hold("outline pick", "urgent"):
            reply = await call(outline.PICK_INSTRUCTION.format(
                query=query, outline=toc,
                limit=settings.agentaus_search_max_sections,
                budget=(settings.agentaus_search_section_tokens
                        * settings.agentaus_search_max_sections),
            ))
    except Exception as exc:
        log.warning("outline pick failed (%s); reading every chunk instead", exc)
        return []
    picks = outline.read_picks(reply, candidates)
    log.info("outline: %d section(s) offered -> %d picked",
             toc.count("<section "), len(picks))
    return picks[: settings.agentaus_search_max_sections]


def sections_around(path: str, lines: list[int], section_tokens: int = 0) -> list:
    """The passages containing the picked lines, sized in tokens and shared out.

    Sized in tokens, and by its own setting rather than by the chunk budget. A chunk is
    sized to cover a whole file in a few reads; a section is a passage around one
    citation, and the two are not the same number.

    Getting that wrong was not subtle. An earlier version sized each window as
    `chunk_budget * 4 // 80` LINES - 2,400 lines at a 48k chunk budget - so the first
    pick swallowed most of the file, every later pick was skipped as already covered, and
    an aimed search read ONE passage where an unaimed one read eleven. Retrieval F1 fell
    from 0.542 to 0.458, which is how the regression was noticed at all.
    """
    body = read_text(path)
    if not body.strip():
        return []
    all_lines = body.splitlines()
    share = max(400, section_tokens or settings.agentaus_search_section_tokens)

    out = []
    for target in sorted(set(lines)):
        centre = max(0, min(target - 1, len(all_lines) - 1))
        if any(start - 1 <= centre < end for _p, start, end, _b in out):
            continue                                # a previous window already covers it
        lo = hi = centre
        while True:
            grew = False
            if lo > 0 and count_tokens("\n".join(all_lines[lo - 1:hi + 1])) <= share:
                lo -= 1
                grew = True
            if hi + 1 < len(all_lines) and count_tokens(
                "\n".join(all_lines[lo:hi + 2])
            ) <= share:
                hi += 1
                grew = True
            if not grew:
                break
        out.append((path, lo + 1, hi + 1, "\n".join(all_lines[lo:hi + 1])))
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

    # A thin shortlist means the words are simply not there - which is the case a keyword
    # search gets wrong, not a sign that nothing matches. So everything gets read.
    #
    # But the shortlist is not discarded to do it. It used to be, and that was strictly
    # worse than either option on its own: searching `SC-NFR-11` - a unique identifier
    # appearing in exactly one file of 485 - matched that one file, decided one was too
    # few to trust, replaced it with all 485, produced 566 chunks and read the first 120.
    # The one file that certainly held the answer was demoted to a 1-in-485 chance of
    # being inside the cap. A thin shortlist is a weak signal, not a wrong one; it goes
    # first, and the rest of the corpus follows it.
    brute_forced = False
    if len(candidates) < settings.agentaus_search_min_candidates:
        best = list(candidates)
        rest = [f for f in files if f not in set(best)]
        candidates = best + rest
        brute_forced = True
    elif len(candidates) > settings.agentaus_search_max_candidates:
        # Ranked, so this keeps the files that matched the most distinct terms.
        candidates = candidates[: settings.agentaus_search_max_candidates]

    budget = effective_chunk_tokens()

    # Aim before reading. One call over a free table of contents, then read only the
    # sections it names - instead of a call per chunk over everything.
    aimed = False
    chunks: list[tuple[str, int, int, str]] = []
    if settings.agentaus_search_outline_first and len(candidates) > 1:
        picks = await _aim_with_outline(query, candidates, call)
        if picks:
            by_file: dict = {}
            for path, line in picks:
                by_file.setdefault(path, []).append(line)
            for path, lines in by_file.items():
                chunks.extend(sections_around(path, lines))
            aimed = bool(chunks)

    # A handful of picked sections is a NARROWER read than no aiming at all, and the
    # point of aiming is precision, not less evidence. Below a floor the sections are
    # kept and the ordinary chunks added behind them: the aimed passages still come
    # first, so a cap trims the least likely material rather than the most likely.
    if aimed and len(chunks) < settings.agentaus_search_min_candidates:
        log.info("outline picked only %d section(s); adding the ordinary chunks behind them",
                 len(chunks))
        seen = {(p, s_) for p, s_, _e, _b in chunks}
        for candidate in candidates:
            for piece in chunk_file(candidate, budget):
                if (piece[0], piece[1]) not in seen:
                    chunks.append(piece)
        aimed = False          # ranking applies again now that the set is not hand-picked

    if not chunks:
        for candidate in candidates:
            chunks.extend(chunk_file(candidate, budget))

    # Rank chunks the same way files were ranked. Shortlisting files is not enough when
    # the corpus is one enormous file: a 434 KB tender document is a single candidate
    # that still costs 27 model calls, and 22 of those chunks contain none of the terms
    # being looked for. Measured: 285 seconds before this, and the answer call after it
    # then ran past the client's patience.
    total = len(chunks)
    if not aimed and total > settings.agentaus_search_min_candidates:
        bodies = [piece[3].lower() for piece in chunks]
        # Selectivity recomputed ACROSS CHUNKS. Reusing the file-level judgement here is
        # what made this filter a no-op on a single-file corpus.
        selective = selective_terms(bodies, [t.lower() for t in terms])
        scored = []
        for piece, body in zip(chunks, bodies):
            hits = sum(1 for term in selective if term in body)
            if hits:
                scored.append((hits, piece))
        # Same reasoning as the file shortlist, and the same correction: a filter that
        # matched too little is still evidence about what matched. Ranked chunks go
        # first either way; when there are too few to trust alone, the unmatched chunks
        # follow them rather than replacing them.
        scored.sort(key=lambda pair: -pair[0])
        ranked_chunks = [piece for _hits, piece in scored]
        if len(scored) >= settings.agentaus_search_min_candidates:
            chunks = ranked_chunks
        elif ranked_chunks:
            matched = {id(piece) for piece in ranked_chunks}
            chunks = ranked_chunks + [c for c in chunks if id(c) not in matched]

    cap = settings.agentaus_search_max_chunks
    dropped = max(0, len(chunks) - cap)
    chunks = chunks[:cap]

    log.info(
        "search %r: %d file(s) -> %d candidate(s)%s -> %d of %d chunk(s)%s",
        query[:60], len(files), len(candidates),
        ("".join([" (shortlist too thin, all files)" if brute_forced else "",
                  " (aimed by outline)" if aimed else ""])),
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
        },
        "required": ["file_path", "start_line"],
    },
}


# A line that opens a new section: a Markdown heading, a bold-only line used as one, an
# underline rule, or a numbered/lettered clause of the kind tenders and specs are made
# of. Widening to one of these is what makes a passage readable rather than merely
# larger - a window cut at an arbitrary offset starts mid-sentence and loses the heading
# that says what the passage is about.
# A STRONG boundary genuinely separates subjects: a Markdown heading, or a horizontal
# rule. Growing a passage through one merges two subjects, and quoting from material
# under a different heading is how a claim ends up attributed to the wrong requirement.
#
# A weak boundary is presentational - a bold line used as a sub-heading, a numbered
# clause. Tender and spec documents are full of them, often every second or third line,
# so treating them as hard stops returns three-line passages that are technically
# sections and useless to quote from. A passage may grow through those.
_STRONG_SECTION_START = re.compile(r"^\s*(?:#{1,6}\s|={3,}\s*$|-{3,}\s*$)")

_SECTION_START = re.compile(
    r"^\s*(?:#{1,6}\s|\*\*[^*]+\*\*\s*$|={3,}\s*$|-{3,}\s*$"
    r"|\d+(?:\.\d+)*[.)]\s|[A-Z]\)\s|\[\d+\]\s*\*\*)"
)


def _widen_to_section(
    lines: list[str], start_idx: int, end_idx: int, radius: int,
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

    return lo, hi


def _fit_to_budget(
    lines: list[str], lo: int, hi: int, start_idx: int, end_idx: int,
    budget_tokens: int, floor_tokens: int = 0,
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
        # Under the ceiling. Grow outward if it is also under the floor - a boundary can
        # sit two lines from the citation, and a three-line section is useless to quote.
        #
        # Growth stops AT a boundary rather than through one. Widening past a heading
        # merges the next section into this one, which is worse than a short passage: the
        # caller is told it has the section containing its citation, and quoting from
        # material under a different heading is how a claim ends up attributed to the
        # wrong requirement.
        while floor_tokens and cost(lo, hi) < floor_tokens:
            widened = False
            if lo > 0 and not _STRONG_SECTION_START.match(lines[lo - 1]):
                lo -= 1
                widened = True
            if hi < len(lines) and not _STRONG_SECTION_START.match(lines[hi]):
                hi += 1
                widened = True
            if not widened or cost(lo, hi) > budget_tokens:
                break
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


async def run_zoom(
    file_path: str,
    start_line: int,
    end_line: int | None,
    default_path: str | None = None,
) -> str:
    """Return a cited passage widened to its section, with line numbers preserved.

    Reads a file. Nothing else - no model call, in any circumstance.

    It used to condense a passage that exceeded the return budget, and that was wrong
    from the start: the whole purpose of zooming into a citation is to see the exact
    words before quoting them, and condensing them first destroys the one thing the
    caller came for. Truncating verbatim is strictly better, and instant.

    The cost of getting that wrong was not theoretical. The threshold was set an order of
    magnitude too low, so every zoom over ordinary tender prose paid for a model call;
    those took 73 to 125 seconds under load, hit a 240-second helper timeout, and stalled
    a batch run for an hour. Raising the threshold moved the bulk downstream until the
    turn carrying it drew a Cloudflare 524. None of it needed to happen.
    """
    if (not file_path or not os.path.isabs(file_path)) and default_path:
        file_path = default_path if not file_path else os.path.join(default_path, file_path)
    if not os.path.isabs(file_path):
        return f"agentaus_zoom needs an absolute path; got {file_path!r}."
    if not _allowed_root(file_path):
        return f"{file_path} is outside AGENTAUS_SEARCH_ROOTS."
    if not enumerate_files(file_path):
        if documents.is_office_document(file_path) and not documents.available(file_path):
            return (f"{file_path} is an office document and no reader for it is "
                    f"available.\n{documents.install_hint(file_path)}")
        return f"{file_path} cannot be read as text (missing, binary, or excluded)."

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
    # A range of hundreds of lines is not a citation, and the cited range is never
    # trimmed away - so a wide one defeats the budget entirely. Capped in tokens, like
    # every other size here: walk forward from the citation until it fills the budget.
    if end > start:
        capped = start
        while capped < end and count_tokens(
            "\n".join(lines[start - 1:capped])
        ) < settings.agentaus_zoom_max_tokens:
            capped += 1
        if capped < end:
            log.info("zoom range %d-%d exceeds the token budget; treating %d-%d as the "
                     "citation", start, end, start, capped)
            end = capped
    if start > len(lines):
        return f"{file_path} has {len(lines)} lines; {start} is past the end."

    # Lines decide WHERE a section begins and ends - that is navigation, and a heading
    # is found by walking lines. Tokens decide how much of it comes back. Those are the
    # only two jobs, and they no longer share a unit.
    lo, hi = _widen_to_section(
        lines, start - 1, min(end, len(lines)), settings.agentaus_zoom_radius_lines,
    )
    lo, hi = _fit_to_budget(
        lines, lo, hi, start - 1, min(end, len(lines)),
        settings.agentaus_zoom_max_tokens, settings.agentaus_zoom_min_tokens,
    )
    window = lines[lo:hi]
    numbered = "\n".join(f"{lo + i + 1:6d}  {line}" for i, line in enumerate(window))

    # Truncation happens only when the cited lines alone exceed the budget, since the
    # surrounding context was already trimmed to fit. Nothing is ever summarised.
    budget = settings.agentaus_zoom_max_tokens * 4
    if count_tokens(numbered) > settings.agentaus_zoom_max_tokens:
        cut = numbered[:budget]
        end_of_line = cut.rfind("\n")
        numbered = cut[:end_of_line] if end_of_line > 0 else cut
        shown = lo + len(numbered.splitlines())
    else:
        shown = hi

    # Tagged, not prefaced with a sentence. A window into a file needs to announce that
    # it IS a window - otherwise the model reads a passage that stops mid-section and
    # concludes the file stops there, or quotes across the cut. The attributes carry that
    # unambiguously, and this model follows structure far more reliably than prose.
    log.info("zoom %s:%d-%d -> lines %d-%d verbatim",
             file_path, start, end, lo + 1, shown)
    complete = "true" if shown >= hi else "false"
    return (
        f'<passage file="{file_path}" lines="{lo + 1}-{shown}" '
        f'you_asked_for="{start}-{end}" section_ends_at="{hi}" '
        f'verbatim="true" complete="{complete}">\n'
        + numbered
        + "\n</passage>\n"
        + ("Every line above is exact - nothing summarised or reworded, so quote freely."
           if complete == "true" else
           f"Every line above is exact - nothing summarised or reworded, so quote freely. "
           f"The section continues past line {shown}; call {ZOOM_TOOL} with "
           f"start_line={shown + 1} for the rest.")
    )


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
