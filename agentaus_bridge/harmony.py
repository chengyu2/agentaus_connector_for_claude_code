"""Tool calls the model wrote as text instead of sending as tool calls.

Observed live, as the entire content of an answer:

    <|start|>assistant<|channel|>commentary to=functions.agentaus_web_search
    {"query":"SC-NFR-11 protocol secure data in transit"}<|call|>

That is OpenAI's Harmony channel format. It is how the model is *supposed* to express a
tool call internally, and normally the serving stack parses it and hands the caller a
structured `tool_calls` field. Sometimes it does not, and the markup arrives as ordinary
assistant text.

The bridge had no idea what it was, so all three things that could go wrong did: the tool
never ran, the raw markup was shown to the user as the answer, and the turn was spent. A
question with a findable answer came back as a line of angle brackets.

So this recovers them. A call written as text is still a call the model meant to make,
and running it is strictly better than printing it. What cannot be parsed is at least
stripped - these control tokens are never something a person asked to see.

The parsing is deliberately forgiving about shape. Harmony has several spellings of the
same thing (`<|constrain|>json<|message|>` between the name and the arguments, or nothing
at all), the closing token is sometimes absent when generation stopped early, and the
model that got here is already off the happy path. Being strict would just turn one
failure mode into another.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("agentaus-bridge")

# The control tokens themselves. Stripped whether or not a call was recovered, because
# they are never content: a user who sees `<|channel|>` is looking at a leak.
CONTROL_TOKENS = (
    "<|start|>", "<|end|>", "<|message|>", "<|call|>", "<|return|>",
    "<|channel|>", "<|constrain|>", "<|endoftext|>", "<|im_start|>", "<|im_end|>",
)

_TARGET = "to=functions."


def looks_like_harmony(text: str) -> bool:
    """Cheap check, so the parser only runs on the rare turn that needs it."""
    if not text:
        return False
    return _TARGET in text or any(token in text for token in CONTROL_TOKENS)


def _json_object_at(text: str, start: int) -> tuple[dict | None, int]:
    """Read one balanced JSON object beginning at or after `start`.

    Brace counting rather than a regex: arguments nest, and a regex that matches to the
    last `}` swallows any following call while one that matches to the first truncates
    the arguments.
    """
    opening = text.find("{", start)
    if opening == -1:
        return None, start

    depth, index, in_string, escaped = 0, opening, False, False
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                body = text[opening:index + 1]
                try:
                    parsed = json.loads(body)
                except ValueError:
                    return None, index + 1
                return (parsed if isinstance(parsed, dict) else None), index + 1
        index += 1
    return None, len(text)


def _name_at(text: str, start: int) -> tuple[str, int]:
    """The tool name following `to=functions.`, ending at whitespace or a token."""
    index = start
    while index < len(text):
        char = text[index]
        if char.isalnum() or char in "_-.":
            index += 1
            continue
        break
    return text[start:index].strip("."), index


# Channel names. These are labels in the protocol, not words the model chose to write,
# so they are only removed where the protocol puts them - directly after `<|channel|>`,
# or at the very front of a header. Never mid-sentence: "the final answer" must survive.
_CHANNELS = ("commentary", "analysis", "final", "assistant")


def strip_control_tokens(text: str) -> str:
    """Remove Harmony scaffolding, leaving whatever prose was mixed in with it."""
    cleaned = text or ""
    # Channel headers first, as whole units. Doing this after the generic token strip
    # would leave the bare label stranded in the middle of the sentence it interrupted.
    for token in ("<|channel|>", "<|start|>"):
        for label in _CHANNELS:
            cleaned = cleaned.replace(token + label, " ")
    for token in CONTROL_TOKENS:
        cleaned = cleaned.replace(token, " ")
    stripped = cleaned.strip()
    for label in _CHANNELS:
        if stripped.lower().startswith(label):
            stripped = stripped[len(label):].lstrip()
    return " ".join(stripped.split())


def extract(text: str) -> tuple[str, list]:
    """Split text into (what is left to say, tool calls the model wrote as prose).

    Returned calls use the same shape as the accumulator's, so the caller can treat them
    exactly like calls that arrived properly and nothing downstream needs to know the
    difference.
    """
    if not looks_like_harmony(text):
        return text, []

    calls: list = []
    kept: list = []
    cursor = 0

    while True:
        marker = text.find(_TARGET, cursor)
        if marker == -1:
            kept.append(text[cursor:])
            break

        kept.append(text[cursor:marker])
        name, after_name = _name_at(text, marker + len(_TARGET))
        arguments, after_args = _json_object_at(text, after_name)

        if not name or arguments is None:
            # Unparseable. Skip past the marker so the scan cannot loop, and let the
            # control-token strip clean up whatever is left.
            cursor = marker + len(_TARGET)
            continue

        calls.append({
            "id": f"harmony_{len(calls)}",
            "name": name,
            "arguments": json.dumps(arguments),
        })

        end = text.find("<|call|>", after_args)
        cursor = end + len("<|call|>") if end != -1 else after_args

    remaining = strip_control_tokens("".join(kept))
    if calls:
        log.warning(
            "recovered %d tool call(s) written as text rather than sent as tool calls: %s",
            len(calls), ", ".join(call["name"] for call in calls),
        )
    return remaining, calls
