"""Reading PDFs, which is several different problems wearing one file extension.

A PDF may carry a clean text layer, a broken one, or none at all, and nothing in the
file announces which. So this does not pick a library and hope. It runs an ordered
ladder of extractors, checks what came back **per page**, and escalates only the pages
that failed - a twenty-page report with two scanned pages costs two OCR calls, not
twenty.

The order is measured, not assumed. Across the 48 PDFs in the corpus this was built
against, `pdftotext -layout` returned more text than pdfminer or pypdf on *every single
file*, often two to three times more, and ran ten to thirty times faster. The reason is
`-layout`: it preserves the column and table geometry that the pure-Python extractors
flatten into a single stream, and a flattened table is exactly the content people put
in PDFs that they then ask questions about.

Every tier is optional. With no system binaries and no OCR this still reads a normal
text-layer PDF via pypdf; with all of them it also reads scans. Nothing here raises: a
PDF that cannot be read should look like a document with nothing in it, not fail the
turn that touched it.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys

from .config import settings

log = logging.getLogger("agentaus-bridge")

PAGE_BREAK = "\f"


def _pages(text: str) -> list[str]:
    """Split page-break-separated text into pages, without inventing a last one.

    A form feed is a page *separator*, and these extractors emit one after the final
    page too, so a naive split yields a trailing empty string. That phantom page fails
    the readability gate, which disqualified the best extractor on most of the corpus
    and silently handed the job to the worst one.
    """
    pages = text.split(PAGE_BREAK)
    while pages and not pages[-1].strip():
        pages.pop()
    return pages

# Characters that legitimately appear in extracted prose. A page whose content is
# mostly outside this set is a broken CID map producing mojibake, not text - counted
# rather than pattern-matched, because there is no regex for "looks like language".
_ORDINARY = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789 \t\n\r.,;:!?'\"()[]{}<>/\\|-_=+*&^%$#@~`"
)


def _tool(name: str) -> str | None:
    """A system binary, looked for where Homebrew and the system keep them.

    The bridge runs under launchd with a minimal PATH, so `shutil.which` alone finds
    nothing that was installed by Homebrew - the same reason `rg` is unreachable here.
    """
    found = shutil.which(name)
    if found:
        return found
    for directory in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# ---------------------------------------------------------------- text-layer tiers

def _pdftotext(path: str) -> list[str] | None:
    """Poppler. Best output and fastest by a wide margin; -layout keeps tables."""
    binary = _tool("pdftotext")
    if not binary:
        return None
    try:
        done = subprocess.run(
            [binary, "-layout", "-enc", "UTF-8", path, "-"],
            capture_output=True,
            timeout=settings.agentaus_pdf_timeout_seconds,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("pdftotext failed on %s (%s)", path, exc)
        return None
    return _pages(done.stdout.decode("utf-8", "replace"))


def _pdfminer(path: str) -> list[str] | None:
    """Pure Python, no system dependency. Slower, and loses column geometry."""
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        return None
    try:
        text = extract_text(path) or ""
    except Exception as exc:  # pdfminer raises a wide variety on malformed files
        log.debug("pdfminer failed on %s (%s)", path, exc)
        return None
    return _pages(text)


def _pypdf(path: str) -> list[str] | None:
    """Last pure-Python resort. Extracts per page natively, so paging is exact."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return [(page.extract_text() or "") for page in PdfReader(path).pages]
    except Exception as exc:
        log.debug("pypdf failed on %s (%s)", path, exc)
        return None


# Ordered by measured quality, best first.
TEXT_TIERS = (("pdftotext", _pdftotext), ("pdfminer", _pdfminer), ("pypdf", _pypdf))


# ------------------------------------------------------------------- quality gate

def page_is_readable(text: str) -> bool:
    """Whether a page came out as language, as opposed to thin or garbled.

    Two ways a page fails. It can be *thin* - almost nothing extracted, which usually
    means the page is a scan with no text layer. Or it can be *garbled* - plenty of
    characters, but mostly outside the ordinary set, which means a broken encoding.
    Both are OCR's problem; neither is distinguishable from the other up front.

    A genuinely near-empty page (a title slide, a section divider) also reads as thin.
    That is fine and deliberate: OCR will confirm it is near-empty, cheaply, and the
    alternative - guessing which sparse pages are "supposed" to be sparse - is worse.
    """
    stripped = (text or "").strip()
    if len(stripped) < settings.agentaus_pdf_min_chars_per_page:
        return False
    ordinary = sum(1 for c in stripped if c in _ORDINARY)
    return ordinary / len(stripped) >= 0.75


# --------------------------------------------------------------------- OCR tiers

def _vision_ocr(path: str, wanted: list[int]) -> dict[int, str]:
    """macOS Vision. Neural, built into the OS, no model to download or install.

    Rasterises through Quartz rather than a Python imaging library, so the OCR tier
    adds no dependency at all on a Mac - which is where this bridge runs.
    """
    if platform.system() != "Darwin":
        return {}
    try:
        import Quartz
        import Vision
        from Foundation import NSURL
    except ImportError:
        return {}

    document = Quartz.CGPDFDocumentCreateWithURL(NSURL.fileURLWithPath_(path))
    if not document:
        return {}
    scale = settings.agentaus_pdf_dpi / 72.0
    out: dict[int, str] = {}

    for index in wanted:
        page = Quartz.CGPDFDocumentGetPage(document, index + 1)
        if not page:
            continue
        box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
        width = int(box.size.width * scale) or 1
        height = int(box.size.height * scale) or 1
        context = Quartz.CGBitmapContextCreate(
            None, width, height, 8, 0,
            Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaNoneSkipLast,
        )
        if not context:
            continue
        # White first. A PDF page paints no background, so without this the glyphs land
        # on transparent black and Vision reads an empty page.
        Quartz.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
        Quartz.CGContextFillRect(context, Quartz.CGRectMake(0, 0, width, height))
        Quartz.CGContextScaleCTM(context, scale, scale)
        Quartz.CGContextTranslateCTM(context, -box.origin.x, -box.origin.y)
        Quartz.CGContextDrawPDFPage(context, page)
        image = Quartz.CGBitmapContextCreateImage(context)
        if not image:
            continue

        lines: list[str] = []

        def collect(request, _error, _sink=lines):
            for observation in request.results() or []:
                best = observation.topCandidates_(1)
                if best:
                    _sink.append(best[0].string())

        request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(collect)
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        try:
            handler.performRequests_error_([request], None)
        except Exception as exc:
            log.debug("Vision OCR failed on page %d of %s (%s)", index + 1, path, exc)
            continue
        out[index] = "\n".join(lines)
    return out


def _tesseract_ocr(path: str, wanted: list[int]) -> dict[int, str]:
    """Tesseract, via poppler's rasteriser. The cross-platform tier."""
    tesseract, render = _tool("tesseract"), _tool("pdftoppm")
    if not tesseract or not render:
        return {}
    out: dict[int, str] = {}
    for index in wanted:
        try:
            page = subprocess.run(
                [render, "-f", str(index + 1), "-l", str(index + 1),
                 "-r", str(settings.agentaus_pdf_dpi), "-png", path, "-"],
                capture_output=True,
                timeout=settings.agentaus_pdf_timeout_seconds,
                check=True,
            ).stdout
            done = subprocess.run(
                [tesseract, "-", "-", "--psm", "3"],
                input=page, capture_output=True,
                timeout=settings.agentaus_pdf_timeout_seconds,
                check=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.debug("tesseract failed on page %d of %s (%s)", index + 1, path, exc)
            continue
        out[index] = done.stdout.decode("utf-8", "replace")
    return out


OCR_TIERS = (("vision", _vision_ocr), ("tesseract", _tesseract_ocr))


def ocr_available() -> bool:
    if platform.system() == "Darwin":
        try:
            import Vision  # noqa: F401
            return True
        except ImportError:
            pass
    return bool(_tool("tesseract") and _tool("pdftoppm"))


def available() -> bool:
    """Whether any tier at all can read a PDF."""
    return any(name == "pdftotext" and _tool("pdftotext") or
               name in ("pdfminer", "pypdf") and _module(name)
               for name, _ in TEXT_TIERS)


def _module(name: str) -> bool:
    if name in sys.modules:
        return True
    try:
        __import__({"pdfminer": "pdfminer.high_level", "pypdf": "pypdf"}[name])
        return True
    except Exception:
        return False


def install_hint() -> str:
    missing = []
    if not _tool("pdftotext"):
        missing.append("`brew install poppler` for the best text extraction")
    if not ocr_available():
        missing.append("`pip install pyobjc-framework-Vision` (macOS) or "
                       "`brew install tesseract` to read scanned pages")
    if not missing:
        return ""
    return "PDF reading is degraded. Install: " + "; ".join(missing) + "."


# ------------------------------------------------------------------------ the ladder

def extract(path: str) -> str:
    """A PDF as text, page-tagged, or "" if nothing could be read.

    Runs the text tiers until one produces a result where every page is readable. If
    none does, keeps the best attempt - most pages of a mostly-good extraction are
    still worth having - and sends only its failed pages to OCR.
    """
    if not settings.agentaus_pdf_extract:
        return ""

    name = os.path.basename(path)
    best: list[str] | None = None
    best_tier = ""

    for tier, extractor in TEXT_TIERS:
        pages = extractor(path)
        if pages is None:
            continue
        if best is None or _volume(pages) > _volume(best):
            best, best_tier = pages, tier
        if all(page_is_readable(p) for p in pages):
            log.info("read %s via %s: %d pages, %d chars", name, tier, len(pages),
                     _volume(pages))
            return _assemble(pages)
        log.debug("%s left %d/%d pages unreadable in %s", tier,
                  sum(1 for p in pages if not page_is_readable(p)), len(pages), name)

    if best is None:
        log.warning("no PDF extractor could read %s. %s", name, install_hint())
        return ""

    thin = [i for i, page in enumerate(best) if not page_is_readable(page)]
    if not thin or not settings.agentaus_pdf_ocr or not ocr_available():
        if thin:
            log.warning("read %s via %s: %d of %d pages unreadable and OCR is "
                        "unavailable. %s", name, best_tier, len(thin), len(best),
                        install_hint())
        return _assemble(best)

    capped = thin[: settings.agentaus_pdf_ocr_max_pages]
    if len(capped) < len(thin):
        # A silent cap reads as full coverage. It is not.
        log.warning("%s needs OCR on %d pages; doing the first %d "
                    "(AGENTAUS_PDF_OCR_MAX_PAGES)", name, len(thin), len(capped))

    for tier, run in OCR_TIERS:
        recovered = run(path, capped)
        if not recovered:
            continue
        gained = 0
        for index, text in recovered.items():
            if len(text.strip()) > len(best[index].strip()):
                best[index] = text
                gained += 1
        log.info("read %s via %s + %s OCR on %d page(s), %d improved, %d chars total",
                 name, best_tier, tier, len(capped), gained, _volume(best))
        break
    else:
        log.warning("OCR produced nothing for %s", name)

    return _assemble(best)


def _volume(pages: list[str]) -> int:
    return sum(len(p.strip()) for p in pages)


def _assemble(pages: list[str]) -> str:
    """Pages joined with their numbers, so a citation can name one.

    Worth the tokens: "page 7" is the only coordinate a PDF has, and without it a quote
    pulled out of a 93-page tender cannot be checked by the person reading the answer.
    """
    if len(pages) == 1:
        return pages[0].strip()
    return "\n\n".join(
        f"[page {number}]\n{page.strip()}"
        for number, page in enumerate(pages, 1)
        if page.strip()
    )
