"""Commits Agentaus writes credit Agentaus, not Claude.

Claude Code tells the model how to sign a commit in a `<system-reminder>` beside the
user's first message. For a model it does not know - `agentaus` included - the line it
asks for is `Co-Authored-By: Claude Code <noreply@anthropic.com>`, so every commit an
Agentaus session made credited Claude on GitHub (captured from Claude Code 2.1.293).

The bridge swaps that trailer for Agentaus' own on the requests it sends to Agentaus.
Claude turns pass through untouched, so each commit credits the model that wrote it. A
project that sets `attribution.commit` in `.claude/settings.json` gets that line from
Claude Code instead, for every model; there is then no Claude trailer here to swap.

Only Claude Code's own words are rewritten: reminders, the system prompt and `system`-role
entries, and only a trailer standing on a line of its own. A tool result, what the user
typed, or a file quoted in a reminder (`  12→Co-Authored-By: ...`) is history, and the
model has to see it as it is.
"""

from __future__ import annotations

import re

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_CLAUDE_TRAILER = re.compile(
    r"^Co-Authored-By:[^\n<]*<noreply@anthropic\.com>[ \t]*$", re.I | re.M)


def credit(body: dict, coauthor: str) -> tuple[dict, int]:
    """`body` with Claude's co-author trailer replaced by one for `coauthor`.

    `coauthor` is `Name <email>`; empty leaves the body alone. Returns (body, trailers
    replaced).
    """
    if not coauthor:
        return body, 0
    line = f"Co-Authored-By: {coauthor}"
    swapped = 0

    def swap(text: str) -> str:
        nonlocal swapped
        text, n = _CLAUDE_TRAILER.subn(lambda _: line, text)
        swapped += n
        return text

    def in_reminders(text: str) -> str:
        return _REMINDER.sub(lambda m: swap(m.group(0)), text)

    def fix_content(content, fix):
        if isinstance(content, str):
            return fix(content)
        if isinstance(content, list):
            return [{**b, "text": fix(b["text"])}
                    if isinstance(b, dict) and isinstance(b.get("text"), str) else b
                    for b in content]
        return content

    out = dict(body)
    if "system" in body:
        out["system"] = fix_content(body["system"], swap)
    out["messages"] = [
        {**m, "content": fix_content(m.get("content"),
                                     swap if m.get("role") == "system" else in_reminders)}
        if m.get("role") in ("system", "user") else m
        for m in body.get("messages") or []]
    return (out, swapped) if swapped else (body, 0)
