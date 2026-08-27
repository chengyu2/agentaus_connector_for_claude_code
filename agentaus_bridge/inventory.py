"""What is actually in a folder, answered by looking rather than by searching.

The question "what kinds of documents are in here, and what does each cover" has no
answer a semantic search can produce. Search asks each chunk "does this answer the
question", and no single chunk answers a question about the whole corpus - so every
chunk says NONE, and the model gets back a few hundred characters after three minutes.
Observed live: two such searches took 183s and 52s and returned 116 and 130 characters
between them, and the turn eventually timed out having learned nothing.

That is not the model being weak. It is being handed the wrong instrument and having no
other one to reach for - and it is why a question about a whole repository came back
answered from the two files that happened to match something.

An inventory is a different shape of answer: counts, groupings, real filenames, and one
line of structure per file. It is a pure directory walk plus the outline extractor, so
it costs no model calls at all and returns in well under a second on hundreds of files.
Whatever the model does next, it starts from what is actually there instead of from a
guess about what matched.
"""

from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict

from . import outline
from .config import settings

log = logging.getLogger("agentaus-bridge")

# A listing is only useful if it fits. Past this many files the per-file lines are
# dropped and the summary carries the answer, which is still the truthful shape of it.
DETAIL_LIMIT = 120


def _kind(name: str) -> str:
    suffix = os.path.splitext(name)[1].lower().lstrip(".")
    return suffix or "(no extension)"


def _headline(path: str, read) -> str:
    """One line saying what a file appears to be, from its own structure.

    The first heading of a document or the first declaration of a module - whatever the
    outline extractor already knows how to find. No model call, and no guessing from the
    filename, which is how a survey ends up describing a document nobody opened.
    """
    try:
        sections = outline.of_file(path, read=read)
    except Exception:
        return ""
    for _start, _end, title in sections[:4]:
        text = (title or "").strip()
        if len(text) >= 8:
            return text[:90]
    return ""


def render(path: str, files: list, read=None, *, detail: bool = True) -> str:
    """The corpus as a structure: how much, of what kinds, where, and what each is.

    Tagged rather than prose. Agentaus follows a marked-up structure far more reliably
    than a paragraph describing the same thing, and this output exists to be read by a
    model that is about to decide where to look.
    """
    if not files:
        return f"<inventory root={path!r}>\n  (no readable files)\n</inventory>"

    kinds = Counter(_kind(os.path.basename(f)) for f in files)
    folders = defaultdict(list)
    for full in files:
        folders[os.path.dirname(os.path.relpath(full, path)) or "."].append(full)

    lines = [f'<inventory root="{path}" files="{len(files)}" folders="{len(folders)}">']
    lines.append("  <kinds>")
    for kind, count in kinds.most_common():
        lines.append(f'    <kind name="{kind}" count="{count}"/>')
    lines.append("  </kinds>")

    show_detail = detail and len(files) <= DETAIL_LIMIT
    lines.append("  <folders>")
    for folder in sorted(folders):
        held = sorted(folders[folder])
        lines.append(f'    <folder path="{folder}" files="{len(held)}">')
        if show_detail:
            for full in held:
                name = os.path.basename(full)
                note = _headline(full, read)
                size = ""
                try:
                    size = f' bytes="{os.path.getsize(full)}"'
                except OSError:
                    pass
                if note:
                    lines.append(f'      <file name="{name}"{size}>{note}</file>')
                else:
                    lines.append(f'      <file name="{name}"{size}/>')
        else:
            for kind, count in Counter(
                _kind(os.path.basename(f)) for f in held
            ).most_common():
                lines.append(f'      <kind name="{kind}" count="{count}"/>')
        lines.append("    </folder>")
    lines.append("  </folders>")

    if not show_detail and detail:
        # Never silently. A summary that looks like a listing is how a partial survey
        # gets reported as a complete one.
        lines.append(
            f"  <note>{len(files)} files is more than the {DETAIL_LIMIT}-file detail "
            f"limit, so folders are summarised by type rather than listed file by file. "
            f"Ask again with a narrower path, or a glob, to see individual files.</note>"
        )
    lines.append("</inventory>")
    return "\n".join(lines)
