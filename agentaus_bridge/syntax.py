"""Does the code in this answer parse?

An answer containing Python that will not compile is wrong, and knowing that requires no
model: `ast.parse` decides it instantly and is never a matter of opinion. The self-review
pass is asked something else - "is this correct" - which genuinely needs a model, and it
passed an answer whose bracket did not match.

Measured: HumanEval/100 failed with "closing parenthesis ']' does not match opening", a
bracket slip in otherwise sound logic, on a turn where review ran and revised nothing.
Claude Opus solved the same problem from the same prompt, so the gap there was a typo
rather than a misunderstanding - the cheapest possible thing to catch and the most
embarrassing to ship.

Only Python is checked, because only Python can be checked here without a parser per
language. Tree-sitter could extend it - it already parses nineteen - but a grammar
accepting a file is a weaker statement than a compiler accepting it, so this stays
narrow and certain rather than broad and approximate.
"""

from __future__ import annotations

import ast
import logging
import re

log = logging.getLogger("agentaus-bridge")

# A fenced block that says it is Python, or one with no language given at all - an
# unlabelled block in a coding answer is Python far more often than not.
_FENCED = re.compile(r"```(?P<lang>[A-Za-z+#]*)\s*\n(?P<body>.*?)```", re.S)

_PY_LANGS = {"", "py", "python", "python3"}

# Enough of a signal that the answer IS code rather than prose that happens to be
# indented. Without this, an explanation containing a colon gets sent for a "fix".
_LOOKS_LIKE_PYTHON = re.compile(
    r"^\s*(?:def |class |import |from \w+ import |async def |@\w+)", re.M
)


def blocks(text: str) -> list[str]:
    """Python source in an answer: fenced blocks, or the whole thing if it is bare code."""
    found = [m.group("body") for m in _FENCED.finditer(text or "")
             if m.group("lang").lower() in _PY_LANGS]
    if found:
        return found
    body = (text or "").strip()
    # Bare code, no fence. Only if it opens like Python and has no prose-looking first
    # line, so an explanation is never mistaken for a broken program.
    if body and _LOOKS_LIKE_PYTHON.match(body):
        return [body]
    return []


def first_error(text: str) -> str:
    """The first syntax error in the answer's Python, or "" if it all parses."""
    for source in blocks(text):
        try:
            ast.parse(source)
        except SyntaxError as exc:
            where = f" (line {exc.lineno})" if exc.lineno else ""
            return f"{exc.msg}{where}"
        except (ValueError, RecursionError) as exc:
            # A null byte, or something pathological. Still not valid source.
            return str(exc)[:120]
    return ""


FIX_INSTRUCTION = """\
The code you produced does not parse.

<error>
{error}
</error>

<your_answer>
{answer}
</your_answer>

<task>
Fix only that. Do not redesign anything, do not rename anything, and do not change the
logic - a program that fails to parse is almost always a typo, and the fix is almost
always one character.

Reissue the whole answer in the same shape as before.
</task>
"""
