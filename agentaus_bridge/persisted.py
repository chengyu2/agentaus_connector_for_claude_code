"""Recovering tool output that Claude Code truncated to a file.

When a tool produces more than the client will carry, Claude Code writes the whole thing
to disk and passes on a preview:

    <persisted-output>
    Output too large (1.6MB). Full output saved to: /path/to/tool-results/abc.txt
    Preview (first 2KB):
    ...
    </persisted-output>

That is a sensible thing for a client to do and a terrible thing for a model to receive.
Observed on a real session: asked to survey a repository, the model ran one `find`, was
handed 2 KB of a 1.6 MB listing, and then wrote confident comparisons between files it
had never seen - including a policy that does not exist. It was told where the full output
was and did not read it.

The bridge runs on the same machine, so it can. The preview is replaced with the real
content, distilled if it is genuinely enormous, before anything else reads the turn.
"""

from __future__ import annotations

import logging
import os
import re

from .config import settings
from .translate import estimate_tokens

log = logging.getLogger("agentaus-bridge")

# "Full output saved to: <path>" is the line that matters. Matched loosely, because the
# surrounding wording is the client's and may change; the path is the payload.
_SAVED_TO = re.compile(
    r"(?:saved|written|persisted)\s+to:?\s*(?P<path>/[^\s\"'<>]+)", re.I
)
_TRUNCATION_MARKERS = ("persisted-output", "output too large", "preview (first")


def looks_truncated(text: str) -> bool:
    """Whether a tool result is a preview standing in for something bigger."""
    if not text:
        return False
    head = text[:2000].lower()
    return any(marker in head for marker in _TRUNCATION_MARKERS)


def saved_path(text: str) -> str | None:
    """The file the client wrote the full output to, if it named one."""
    found = _SAVED_TO.search(text or "")
    if not found:
        return None
    path = found.group("path").rstrip(".,)")
    return path if os.path.isfile(path) else None


def _read(path: str, limit_bytes: int) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read(limit_bytes)
    except OSError as exc:
        log.warning("could not read persisted output %s (%s)", path, exc)
        return ""
    if size > limit_bytes:
        body += (f"\n[This is the first {limit_bytes:,} of {size:,} bytes. Narrow the "
                 f"command - or read {path} directly with an offset - for the rest.]")
    return body


def restore(body: dict) -> dict:
    """Replace truncated tool previews with the output the client actually saved.

    Never raises, and leaves a result alone unless there is a real file to read: a
    preview is bad, and a preview replaced by nothing is worse.
    """
    if not settings.agentaus_restore_persisted:
        return body

    messages = body.get("messages") or []
    changed_any = False
    out = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        blocks, changed = [], False
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                blocks.append(block)
                continue
            text = block.get("content")
            if not isinstance(text, str) or not looks_truncated(text):
                blocks.append(block)
                continue
            path = saved_path(text)
            if not path:
                blocks.append(block)
                continue
            full = _read(path, settings.agentaus_restore_max_bytes)
            if not full or estimate_tokens(full) <= estimate_tokens(text):
                blocks.append(block)
                continue
            changed = changed_any = True
            blocks.append({
                **block,
                "content": (
                    f"[The client truncated this result to a preview and saved the whole "
                    f"thing to {path}. The bridge read that file; below is the real "
                    f"output, not a preview.]\n" + full
                ),
            })
        out.append({**message, "content": blocks} if changed else message)

    if changed_any:
        log.info("restored truncated tool output from disk")
        return {**body, "messages": out}
    return body
