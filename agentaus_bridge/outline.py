"""A free structural map of a document, so search can aim before it reads.

`agentaus_search` finds things by reading: it chunks a file and spends one model call per
chunk. That is what makes it work when the answer shares no words with the question, and
it is also what makes it cost ten calls and half a minute on a large document.

The cheaper half of the problem is structural. "Which part of this document is about
security accreditation" is answerable from its headings alone, and headings cost nothing to
extract. So the outline is built locally - no model call, no upstream request - and the
model picks sections from a table of contents before anything is read in full.

Structure means different things in different files, so there are three extractors rather
than one. Source code is indexed by its declarations - the equivalent of a symbol index,
and the thing that makes "where is this defined" answerable without reading anything.
Documents are indexed by their headings. JSON is indexed by its keys, which is all its
structure amounts to.

That is three extractors for three genuinely different structures, which is not the same
mistake as having two extractors for one - a heading and a function signature are not
competing descriptions of the same thing.

Code declarations come from Tree-sitter where it is installed - exact, across nineteen
languages, and able to tell a declaration from the same words inside a docstring. Below
that sits Python's `ast`, which is exact and always available but only for Python, and
below that a declaration-line pass that works anywhere and is approximate. Each rung runs
only when the one above cannot: a fallback chain, not competing answers.
"""

from __future__ import annotations

import json
import logging
import os
import re

from . import documents
from . import symbols

log = logging.getLogger("agentaus-bridge")

# What counts as a heading in a document. Markdown headings, an underlined title, a bold
# line standing alone, a numbered clause, and a table header row - the shapes that
# actually separate subjects in specifications and tender responses.
_HEADING = re.compile(
    r"^\s*(?:"
    r"(?P<hash>#{1,6})\s+(?P<htext>.+?)\s*$"
    r"|\*\*(?P<btext>[^*]{3,120})\*\*\s*$"
    # A multi-part number is self-evidently a clause number and needs no trailing
    # punctuation - "5.3.1 Adherence to standards" is how requirement tables are written.
    # A bare single number does need it, or every line starting with a figure matches.
    r"|(?P<ntext>(?:\d+(?:\.\d+)+|\d+[.)])\s+\S.{2,118}?)\s*$"
    r")"
)


def _depth(match: re.Match) -> int:
    if match.group("hash"):
        return len(match.group("hash"))
    if match.group("btext"):
        return 4
    return 3


def _text(match: re.Match) -> str:
    return (match.group("htext") or match.group("btext") or match.group("ntext") or "").strip()


def of_text(body: str, *, max_headings: int = 400) -> list[tuple[int, int, str]]:
    """(line, depth, title) for every heading, in order."""
    found = []
    for number, line in enumerate(body.splitlines(), 1):
        if len(line) > 200:
            continue                     # a long line is prose, not a heading
        match = _HEADING.match(line)
        if match and _text(match):
            found.append((number, _depth(match), _text(match)))
            if len(found) >= max_headings:
                break
    return found


def _json_outline(body: str, path: str) -> list[tuple[int, int, str]]:
    """Top-level keys of a JSON file, which is what its structure amounts to.

    82 of the files in one real corpus are JSON. Treating them as prose wastes model
    calls on punctuation; their keys are the map.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, dict):
        return [(1, 1, f"key: {k}") for k in list(data)[:200]]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return [(1, 1, f"list of {len(data)} objects, fields: "
                       + ", ".join(list(data[0])[:12]))]
    return []


# Files whose structure is declarations rather than headings.
_CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".scala",
    ".sh", ".bash", ".zsh", ".sql", ".lua", ".dart", ".ex", ".exs", ".hs", ".pl", ".r",
}

# A declaration at low indentation, across the languages people actually write in. Nested
# helpers are deliberately not indexed: an outline of every closure is not an outline.
_DECLARATION = re.compile(
    r"^(?P<indent>[ \t]{0,4})"
    r"(?:(?:public|private|protected|internal|static|final|abstract|async|export|"
    r"default|pub|open|override|suspend|extern|inline|const)\s+)*"
    r"(?P<kind>def|class|struct|interface|enum|trait|impl|type|func|function|fn|"
    r"module|package|record|protocol|extension|CREATE\s+(?:TABLE|VIEW|FUNCTION))"
    r"\s+(?P<name>[A-Za-z_][\w.<>:]*)",
    re.I,
)


def _python_outline(body: str) -> list[tuple[int, int, str]]:
    """Top-level and class-level definitions, from the standard library's own parser.

    Exact where a pattern would only be close: `ast` knows a decorated async method from
    a string that happens to contain the word `def`.
    """
    import ast

    try:
        tree = ast.parse(body)
    except SyntaxError:
        return []

    found: list[tuple[int, int, str]] = []

    def visit(node, depth: int) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                args = ", ".join(a.arg for a in child.args.args[:6])
                found.append((child.lineno, depth, f"{prefix} {child.name}({args})"))
                if depth < 2:
                    visit(child, depth + 1)
            elif isinstance(child, ast.ClassDef):
                found.append((child.lineno, depth, f"class {child.name}"))
                visit(child, depth + 1)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and depth == 1:
                for target in getattr(child, "targets", []) or [getattr(child, "target", None)]:
                    name = getattr(target, "id", None)
                    if name and name.isupper():
                        found.append((child.lineno, depth, f"{name} (constant)"))
    visit(tree, 1)
    found.sort()
    return found


def _code_outline(body: str, max_headings: int = 400) -> list[tuple[int, int, str]]:
    """Declarations, for a language without an exact parser here."""
    found = []
    for number, line in enumerate(body.splitlines(), 1):
        if len(line) > 200:
            continue
        match = _DECLARATION.match(line)
        if match:
            depth = 1 + len(match.group("indent").replace("\t", "  ")) // 2
            found.append((number, depth,
                          f'{match.group("kind").lower()} {match.group("name")}'))
            if len(found) >= max_headings:
                break
    return found


def of_file(path: str, read=None) -> list[tuple[int, int, str]]:
    """The outline of one file. Costs a read and nothing else.

    Dispatched by what the file IS: declarations for code, headings for prose, keys for
    JSON. A file that yields nothing under its own extractor falls back to headings,
    because a heading is a reasonable guess about any text.
    """
    reader = read or (lambda p: "")
    body = reader(path)
    if not body.strip():
        return []

    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".json":
        return _json_outline(body, path)

    if suffix in _CODE_SUFFIXES or symbols.language_for(path):
        # Best available, in strict order of exactness.
        return (symbols.outline_of(path, body)
                or (_python_outline(body) if suffix in (".py", ".pyi") else [])
                or _code_outline(body)
                or of_text(body))
    return of_text(body)


def render(paths: list[str], read=None, *, max_lines: int = 600) -> str:
    """A tagged table of contents across several files, for the model to choose from.

    Line numbers are the point: whatever the model picks, it picks something
    `agentaus_zoom` and the chunk reader can address directly.
    """
    out: list[str] = []
    for path in paths:
        headings = of_file(path, read)
        if not headings:
            continue
        out.append(f'<file path="{path}">')
        for line, depth, title in headings:
            out.append(f'  <section line="{line}" depth="{depth}">{title}</section>')
        out.append("</file>")
        if len(out) >= max_lines:
            out.append(f"<!-- outline truncated at {max_lines} lines -->")
            break
    return "\n".join(out)


PICK_INSTRUCTION = """\
<question>
{query}
</question>

<outline>
{outline}
</outline>

<task>
The outline above is the section structure of some documents - headings and the line each
one starts at. No content, only structure.

Name the sections most likely to answer the question. Judge by what each heading says the
section is about; a heading that names the subject is a better bet than one that merely
shares a word with the question.
</task>

<output_format>
One per line, exactly: <path>:<line>
At most {limit} lines. Nothing else - no commentary, no explanation.
If the outline tells you nothing useful, output exactly: NONE
</output_format>
"""


def read_picks(reply: str, known: list[str]) -> list[tuple[str, int]]:
    """Parse `path:line` choices, keeping only paths that were actually offered.

    A model that invents a path gets it dropped rather than followed - the same reason
    invented tool names are caught rather than passed on.
    """
    if not reply or reply.strip().upper().startswith("NONE"):
        return []
    offered = set(known)
    picks = []
    for line in reply.splitlines():
        found = re.search(r"(?P<path>/[^\s:]+(?:[^\s:]|\\ )*):(?P<line>\d+)", line.strip())
        if not found:
            continue
        path, number = found.group("path"), int(found.group("line"))
        if path in offered and (path, number) not in picks:
            picks.append((path, number))
    return picks
