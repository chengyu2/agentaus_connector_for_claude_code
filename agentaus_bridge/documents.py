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


def is_office_document(path: str) -> bool:
    return Path(path).suffix.lower() in OFFICE_SUFFIXES


def available() -> bool:
    """Whether office extraction can actually be performed."""
    return settings.agentaus_office_extract and soffice() is not None


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
    if not available():
        return ""
    key = _key(path)
    cached = _cache.get(key)
    if cached is not None:
        return cached

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
