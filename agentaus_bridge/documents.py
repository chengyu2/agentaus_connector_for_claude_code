"""Reading Word documents and spreadsheets, via LibreOffice, with tables intact.

A `.docx` is a zip archive. Read as text it is binary noise, so the bridge used to skip
office documents entirely - which meant `agentaus_search` and `agentaus_zoom` could not
see a tender response, a specification or a requirements matrix, which is exactly the
material people keep in them.

Flattening the document XML with a pattern is not good enough either, and the failure is
quiet. Measured against LibreOffice on one real 43-row requirements table, a regex flatten
lost 2,695 characters of answer text across 12 rows - 56% of one row - and could not see
the compliance column at all, because it had no idea where one cell ended and the next
began. It also guessed at row boundaries, so the last row swallowed 77,000 characters of
the rest of the document.

LibreOffice knows the format. It converts to HTML, which keeps `<table>`, `<tr>` and
`<td>`, and this module renders that back to text with the cell boundaries still visible.
A table row becomes one line with ` | ` between cells, so line numbers stay meaningful and
`agentaus_zoom` can cite into a spreadsheet the same way it cites into source code.
"""

from __future__ import annotations

import hashlib
import html as htmllib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import settings

log = logging.getLogger("agentaus-bridge")

# Word processing, spreadsheets and presentations. All zip archives, all unreadable as
# text, all things people keep requirements and evidence in.
OFFICE_SUFFIXES = {
    ".docx", ".doc", ".dotx", ".rtf", ".odt",
    ".xlsx", ".xls", ".xlsm", ".ods", ".csv" if False else ".xlsb",
    ".pptx", ".ppt", ".odp",
}

# Where soffice usually lives. Checked in order; the configured path wins.
_CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
    "/snap/bin/libreoffice",
)

_binary: str | None = None
_looked = False


def soffice() -> str | None:
    """The LibreOffice binary, or None if it is not installed."""
    global _binary, _looked
    if _looked:
        return _binary
    _looked = True
    configured = settings.soffice_path.strip()
    if configured:
        _binary = configured if os.access(configured, os.X_OK) else None
        if _binary is None:
            log.warning("AGENTAUS_SOFFICE_PATH=%s is not executable", configured)
        return _binary
    for candidate in _CANDIDATES:
        if os.access(candidate, os.X_OK):
            _binary = candidate
            break
    else:
        _binary = shutil.which("soffice") or shutil.which("libreoffice")
    if _binary:
        log.info("office documents will be read with %s", _binary)
    return _binary


# PDFs are handled by their own ladder (`pdf.py`), not by LibreOffice. LibreOffice can
# open a PDF, but its importer treats it as a drawing to be edited: converting one here
# produced 112 characters of CSS and a hundred GIFs, which read as "this document is
# empty" when the document was eight pages of text.
PDF_SUFFIXES = {".pdf"}

READABLE_SUFFIXES = OFFICE_SUFFIXES | PDF_SUFFIXES


def is_pdf(path: str) -> bool:
    return Path(path).suffix.lower() in PDF_SUFFIXES


def is_office_document(path: str) -> bool:
    """Whether this is a file the bridge must extract rather than read as text.

    Includes PDFs. The name is now slightly wrong and the behaviour is right: every
    caller wants "is this unreadable as text", and a PDF read with `read_text` is 1.4MB
    of binary noise in the transcript.
    """
    return Path(path).suffix.lower() in READABLE_SUFFIXES


def available(path: str | None = None) -> bool:
    """Whether extraction can be performed for `path`, which needs different tools."""
    if path and is_pdf(path):
        from . import pdf as _pdf
        return settings.agentaus_pdf_extract and _pdf.available()
    return settings.agentaus_office_extract and soffice() is not None


def install_hint(path: str = "") -> str:
    """What to tell someone whose document cannot be read.

    One instruction, because there is one way to read these files here. Pure-Python
    readers exist and are far smaller, but they would be a SECOND implementation of "how
    does a table become text" - with different format coverage and a different rendering
    of the same document. Two answers to that question is worse than one large
    dependency: a row that reads one way in development and another in CI is a bug nobody
    finds until it matters.
    """
    if path and is_pdf(path):
        from . import pdf as _pdf
        return _pdf.install_hint()

    return (
        "LibreOffice reads these. Install it and the bridge picks it up with no further "
        "configuration:\n"
        "  macOS   brew install --cask libreoffice\n"
        "  Debian  sudo apt install libreoffice-writer libreoffice-calc\n"
        "  or set AGENTAUS_SOFFICE_PATH if it is installed somewhere unusual."
    )


# Converted text, keyed by path plus mtime plus size. A conversion is expensive and a
# document does not change while a search is reading it, but it MUST be re-read when the
# file changes - a stale extract is worse than a slow one.
_cache: dict[str, str] = {}


def _key(path: str) -> str:
    try:
        stat = os.stat(path)
        stamp = f"{path}\x00{stat.st_mtime_ns}\x00{stat.st_size}"
    except OSError:
        stamp = path
    return hashlib.sha256(stamp.encode()).hexdigest()


def _cell(cell: str) -> str:
    """One table cell as a single line, with its internal breaks preserved as spaces."""
    text = re.sub(r"<br\s*/?>", " ", cell, flags=re.I)
    text = re.sub(r"</p\s*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(htmllib.unescape(text).split())


def html_to_text(html: str) -> str:
    """Render converted HTML as text, keeping table structure visible.

    Rows become one line with ` | ` between cells. That keeps a row on one line - so a
    line number still identifies it - while leaving the cell boundaries the regex flatten
    destroyed.
    """
    out: list[str] = []
    position = 0
    for table in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.I | re.S):
        out.extend(_prose(html[position:table.start()]))
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.I | re.S):
            cells = [
                _cell(c) for c in
                re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)
            ]
            if any(cells):
                out.append(" | ".join(cells))
        out.append("")
        position = table.end()
    out.extend(_prose(html[position:]))
    return "\n".join(line for line in out if line is not None)


def _prose(fragment: str) -> list[str]:
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = re.sub(r"</p\s*>|</h[1-6]\s*>|</div\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return [l.strip() for l in htmllib.unescape(text).splitlines() if l.strip()]


def extract(path: str) -> str:
    """The document as text, or "" if it cannot be converted.

    Never raises: a document that will not convert should look like a document with
    nothing readable in it, not fail the turn that touched it.
    """
    key = _key(path)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    if is_pdf(path):
        from . import pdf as _pdf
        text = _pdf.extract(path)
        _cache[key] = text
        return text

    if not available():
        return ""

    binary = soffice()
    with tempfile.TemporaryDirectory(prefix="agentaus-office-") as workdir:
        try:
            subprocess.run(
                [
                    binary,
                    # A private profile per conversion. Without it, concurrent soffice
                    # invocations fight over the shared user profile and one silently
                    # produces nothing - and search runs several at once.
                    f"-env:UserInstallation=file://{workdir}/profile",
                    "--headless", "--norestore",
                    "--convert-to", "html",
                    "--outdir", workdir, path,
                ],
                capture_output=True,
                timeout=settings.agentaus_office_timeout_seconds,
                check=True,
            )
        except subprocess.TimeoutExpired:
            log.warning("LibreOffice timed out converting %s", path)
            _cache[key] = ""
            return ""
        except (subprocess.CalledProcessError, OSError) as exc:
            log.warning("LibreOffice could not convert %s (%s)", path, exc)
            _cache[key] = ""
            return ""

        produced = sorted(Path(workdir).glob("*.htm*"), key=lambda p: -p.stat().st_size)
        if not produced:
            log.warning("LibreOffice produced no output for %s", path)
            _cache[key] = ""
            return ""
        text = html_to_text(produced[0].read_text(errors="replace"))

    log.info("read %s via LibreOffice: %d chars of text", os.path.basename(path), len(text))
    _cache[key] = text
    return text


def reset_cache() -> None:
    """For tests, and for a caller that knows a document changed under it."""
    _cache.clear()


# Fields a client tool uses to name the file it read. Claude Code's `Read` uses
# `file_path`; other tools and MCP servers vary.
_PATH_FIELDS = ("file_path", "path", "notebook_path", "filename", "file")


def _named_path(tool_input) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for field in _PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def repair_tool_results(body: dict) -> dict:
    """Replace office-document read results with real text.

    Claude Code's `Read` runs on the client, so the bridge cannot change what it does -
    and what it does with a `.docx` is hand back zip noise, because a `.docx` is a zip.
    The bridge can fix the result: it knows which file the call named, it runs on the same
    machine, and it can read the document properly.

    Strictly better than what arrives, so it does not try to judge whether the original
    "looks like garbage" - a client that returned something useful for an office document
    would have had to read it the same way. Substitution is keyed on the file's own
    content (see `extract`), so the conversation prefix stays stable across turns and the
    compaction cache keeps hitting.
    """
    if not available():
        return body

    messages = body.get("messages") or []
    named: dict = {}
    for message in messages:
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                path = _named_path(block.get("input"))
                if path and is_office_document(path):
                    named[block.get("id") or ""] = path
    if not named:
        return body

    repaired_any = False
    out = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        blocks, changed = [], False
        for block in content:
            path = (
                named.get(block.get("tool_use_id") or "")
                if isinstance(block, dict) and block.get("type") == "tool_result"
                else None
            )
            if not path or block.get("is_error"):
                blocks.append(block)
                continue
            text = extract(path)
            if not text:
                blocks.append(block)
                continue
            changed = repaired_any = True
            blocks.append({
                **block,
                "content": (
                    f"[The bridge re-read {os.path.basename(path)} with LibreOffice, "
                    f"because an office document is a zip archive and cannot be read as "
                    f"text. Table rows are one line each, cells separated by ' | '.]\n"
                    + text
                ),
            })
        out.append({**message, "content": blocks} if changed else message)

    if repaired_any:
        log.info("re-read %d office document result(s) that arrived as binary", len(named))
        return {**body, "messages": out}
    return body
