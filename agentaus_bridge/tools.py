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
import time
from typing import Awaitable, Callable

from .compact import _chunk, normalise_identifiers
from .config import settings
from .gate import hold
from .tokens import count_tokens

log = logging.getLogger("agentaus-bridge")

Caller = Callable[[str], Awaitable[str]]

SEARCH_TOOL = "agentaus_search"
WEB_SEARCH_TOOL = "agentaus_web_search"

# Names the bridge answers itself. Anything else is Claude Code's and is passed through.
BRIDGE_TOOLS = {SEARCH_TOOL, WEB_SEARCH_TOOL}


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

Answer from what the search returns, not from memory. For every fact you state, give the \
source URL it came from. If the search finds nothing useful, say so plainly rather than \
answering from what you already believe - a confident answer from memory is exactly what \
this search was run to avoid.

Be terse. No preamble.
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
        async with hold("web search"):
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
        name = os.path.basename(path)
        if _is_secret(name):
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
A developer is searching a codebase for the answer to this question:

{query}

List the literal strings most likely to appear in source code that answers it: function \
and variable names, class names, config keys, library calls, error text. Include \
synonyms and the terms a programmer would actually type, not the words of the question.

Output 5 to 10 terms, one per line, nothing else. No numbering, no explanation.
"""


CHUNK_INSTRUCTION = """\
Below is an excerpt from `{path}` (lines {start}-{end}).

Decide whether it contains anything that answers this question:

{query}

If it does, quote the relevant lines verbatim with their line numbers, then add one \
short sentence saying how they answer it. Quote only what is relevant - not the whole \
excerpt.

If it does not, reply with exactly: NONE

EXCERPT:
{body}
"""


MERGE_HITS_INSTRUCTION = """\
Below are separate excerpts from a codebase, each already judged relevant to this \
question:

{query}

Combine them into one answer. Keep every file path and line number exactly as written. \
Order them so the most direct answer comes first. Remove repetition, but never drop a \
location that appears only once. Do not add anything that is not below.

EXCERPTS:
{body}
"""


async def expand_query(query: str, call: Caller) -> list[str]:
    """Literal terms worth scanning for, from a plain-language question.

    This is what keeps the prefilter from being a plain keyword match: the question
    "where do we cap concurrent calls" contains none of the words that appear in the
    code that does it, and expansion is what bridges that.
    """
    try:
        async with hold("query expansion"):
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


def shortlist(files: list[str], terms: list[str]) -> list[tuple[str, int]]:
    """Files containing any expanded term, ranked by how many distinct terms hit.

    A plain substring scan in Python rather than a ripgrep subprocess: `rg` is a shell
    function provided by the Claude Code extension and is not on a clean PATH, so the
    launchd-managed bridge cannot see it. At repo scale the difference does not matter.
    """
    lowered = [t.lower() for t in terms]
    scored: list[tuple[str, int]] = []
    for path in files:
        body = read_text(path).lower()
        if not body:
            continue
        hits = sum(1 for term in lowered if term in body)
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
    query: str, path: str, glob: str | None, call: Caller
) -> str:
    """Execute one `agentaus_search` call and return text for the tool result."""
    if not os.path.isabs(path):
        return f"agentaus_search needs an absolute path; got {path!r}."
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

    chunks: list[tuple[str, int, int, str]] = []
    for candidate in candidates:
        chunks.extend(chunk_file(candidate, settings.agentaus_search_chunk_tokens))

    cap = settings.agentaus_search_max_chunks
    dropped = max(0, len(chunks) - cap)
    chunks = chunks[:cap]

    log.info(
        "search %r: %d file(s) -> %d candidate(s)%s -> %d chunk(s)%s",
        query[:60], len(files), len(candidates),
        " (brute force: shortlist too thin)" if brute_forced else "",
        len(chunks), f", {dropped} over the cap" if dropped else "",
    )

    if not chunks:
        return f"Nothing readable to search under {path}."

    async def look(piece: tuple[str, int, int, str]) -> str:
        chunk_path, start, end, body = piece
        try:
            async with hold("search chunk"):
                answer = await call(
                    CHUNK_INSTRUCTION.format(
                        path=chunk_path, start=start, end=end, query=query, body=body
                    )
                )
        except Exception as exc:
            log.warning("search chunk failed for %s (%s)", chunk_path, exc)
            return ""
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
            async with hold("search merge"):
                merged = await call(
                    MERGE_HITS_INSTRUCTION.format(query=query, body=joined)
                )
            if merged and merged.strip():
                joined = normalise_identifiers(merged.strip())
        except Exception as exc:
            log.warning("search merge failed (%s); returning the raw hits", exc)

    return header + joined


async def execute(name: str, arguments: dict, call: Caller) -> str:
    """Run one bridge-owned tool call. Never raises - a failure becomes tool output."""
    try:
        if name == SEARCH_TOOL:
            return await run_search(
                str(arguments.get("query") or ""),
                str(arguments.get("path") or ""),
                (arguments.get("glob") or None),
                call,
            )
        if name == WEB_SEARCH_TOOL:
            return await run_web_search(str(arguments.get("query") or ""), call)
        return f"[bridge] Unknown tool {name!r}."
    except Exception as exc:  # a broken tool must not end the turn
        log.warning("bridge tool %s failed: %s", name, exc)
        return f"[bridge] {name} failed: {exc}"
