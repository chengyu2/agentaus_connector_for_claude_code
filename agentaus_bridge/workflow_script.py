"""Repairing the one slip Agentaus makes in nearly every `Workflow` script.

Agentaus writes the whole script on one line. JavaScript ends a statement at a newline
when it has to, so the same text spread over lines is valid - but on one line,
`export const meta = {...} const TASK = ...` is a parse error. Measured on Claude Code
2.1.300: every one of ten scripts was a single line, and four of the five rejected were
rejected for exactly this, one of them three times in a row before the model changed
anything.

The repair inserts `;` between a closing `}` and a keyword that can only start a new
statement. That is unambiguous: no expression continues `} const` or `} return`, and a
`;` after a block statement is an empty statement, so a script that was already valid
is unchanged in meaning. The scan is lexical so the same text inside a string, a
template literal, a comment or a regular expression is never touched.
"""

from __future__ import annotations

import re

# Keywords that only ever begin a statement. Not `while` (it closes `do {} while`), and
# not `else`, `catch` or `finally`, which continue the statement the brace closed.
_STARTS_STATEMENT = re.compile(
    r"(?:const|let|var|await|return|if|for|try|throw|function|class|export|phase|log)"
    r"(?![\w$])")

# After one of these, a `/` begins a regular expression rather than a division.
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^") | {""}
# ... and after one of these words, which an expression follows (`return /x/.test(s)`).
_REGEX_AFTER_WORDS = {"return", "typeof", "case", "do", "else", "in", "of", "instanceof",
                      "new", "delete", "void", "throw", "await", "yield"}
_WORD_BEFORE = re.compile(r"([A-Za-z_$][\w$]*)\s*$")


def _regex_follows(out: list, last: str) -> bool:
    if last in _REGEX_AFTER:
        return True
    if not (last.isalnum() or last in "_$"):
        return False
    word = _WORD_BEFORE.search("".join(out[-40:]))
    return bool(word) and word.group(1) in _REGEX_AFTER_WORDS


def separate_statements(script: str) -> str:
    """`script` with `;` inserted wherever `}` runs straight into a new statement."""
    out: list[str] = []
    i, n = 0, len(script)
    # Template literals nest through `${ ... }`: each entry is the brace depth at which
    # the expression it opened will close, back into the template text.
    template_exprs: list[int] = []
    depth = 0
    last = ""  # the last significant character outside strings and comments

    def scan_string(quote: str, start: int) -> int:
        j = start + 1
        while j < n:
            c = script[j]
            if c == "\\":
                j += 2
                continue
            if c == quote or (c == "\n" and quote != "`"):
                return j + 1
            j += 1
        return n

    while i < n:
        c = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            end = script.find("\n", i)
            end = n if end < 0 else end
            out.append(script[i:end])
            i = end
            continue
        if c == "/" and nxt == "*":
            end = script.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append(script[i:end])
            i = end
            continue
        if c in "'\"":
            end = scan_string(c, i)
            out.append(script[i:end])
            i, last = end, c
            continue
        if c == "/" and _regex_follows(out, last):
            j, in_class = i + 1, False
            while j < n and script[j] != "\n":
                if script[j] == "\\":
                    j += 2
                    continue
                if script[j] == "[":
                    in_class = True
                elif script[j] == "]":
                    in_class = False
                elif script[j] == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and (script[j].isalnum()):
                j += 1
            out.append(script[i:j])
            i, last = j, "/"
            continue
        if c == "`":
            # Template text up to its closing backtick or the next `${`.
            j = i + 1
            while j < n:
                if script[j] == "\\":
                    j += 2
                    continue
                if script[j] == "`":
                    j += 1
                    break
                if script[j] == "$" and j + 1 < n and script[j + 1] == "{":
                    template_exprs.append(depth)
                    depth += 1
                    j += 2
                    break
                j += 1
            out.append(script[i:j])
            i, last = j, ("{" if template_exprs and script[j - 1] == "{" else "`")
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if template_exprs and depth == template_exprs[-1]:
                # This brace ends a `${...}`; resume the template text after it.
                template_exprs.pop()
                j = i + 1
                while j < n:
                    if script[j] == "\\":
                        j += 2
                        continue
                    if script[j] == "`":
                        j += 1
                        break
                    if script[j] == "$" and j + 1 < n and script[j + 1] == "{":
                        template_exprs.append(depth)
                        depth += 1
                        j += 2
                        break
                    j += 1
                out.append(script[i:j])
                i, last = j, "`"
                continue
            out.append(c)
            i += 1
            k = i
            while k < n and script[k] in " \t":
                k += 1
            if k < n and script[k] not in "\n;" and _STARTS_STATEMENT.match(script, k):
                out.append(";")
            last = "}"
            continue
        out.append(c)
        if not c.isspace():
            last = c
        i += 1
    return "".join(out)
