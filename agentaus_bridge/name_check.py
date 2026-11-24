"""A name an Edit left undefined in a Python function, said in that Edit's result.

Measured (bench robust-v1 quick_edit r1 and r2): asked to rename a parameter `k` to
`factor`, the model changed the signature and every call site, said "Done", and left the
body using `k`. Claude Code reported the Edit as a success, nothing ran the function, and
both runs shipped a NameError. A weak model does not re-read the function it just edited.

The bridge is on the machine the file is on. When an Edit/MultiEdit/Write of a .py file
comes back successful, it compiles the file with `symtable` and looks, in each function the
edit touched, for names that are neither local, nor defined at module level, nor imported,
nor builtins - names that can only raise NameError when the function runs. Any it finds are
added to that call's result, so the model's next turn fixes them instead of answering.

Deliberately narrow: a module with `from x import *`, `globals()` tricks or a syntax error
is skipped, and only functions overlapping the edited lines are checked.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import symtable

_WRITES = {"Edit", "MultiEdit", "Write"}
_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__builtins__",
                                  "__spec__", "__loader__", "__package__", "__path__",
                                  "__annotations__", "__dict__", "__class__", "__module__",
                                  "__qualname__", "reveal_type"}
_MAX_BYTES = 400_000


def _args(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def undefined(text: str, filename: str, lines: set | None = None) -> list:
    """[(function name, def line, name)] for names in functions that can only raise
    NameError. `lines` limits it to functions overlapping those 1-based line numbers."""
    if "import *" in text or "globals()" in text or "__getattr__" in text \
            or "exec(" in text or "eval(" in text or "builtins" in text:
        return []
    try:
        top = symtable.symtable(text, filename, "exec")
        tree = ast.parse(text, filename)
    except (SyntaxError, ValueError, TypeError):
        return []
    spans = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.setdefault(node.name, []).append((node.lineno, node.end_lineno or node.lineno))
    known = {s.get_name() for s in top.get_symbols()} | _BUILTINS
    tables = []

    def collect(table, owner):
        for child in table.get_children():
            here = owner
            if child.get_type() == "function":
                # A comprehension or lambda is its own scope before Python 3.12; its names
                # are reported against the function it is written in.
                here = child if not child.get_name().startswith("<") or owner is None else owner
                tables.append((child, here))
                known.update(s.get_name() for s in child.get_symbols() if s.is_declared_global())
            elif child.get_type() == "class":
                here = None
            collect(child, here)

    collect(top, None)

    def span(table):
        # symtable's line is the first decorator's on some versions; ast's is the `def`.
        found = [sp for sp in spans.get(table.get_name(), []) if sp[0] >= table.get_lineno()]
        return min(found) if found else None

    out, seen = [], set()
    for table, owner in tables:
        if owner is None or owner.get_name().startswith("<"):
            continue
        sp = span(owner)
        if lines is not None and (sp is None or not any(sp[0] <= n <= sp[1] for n in lines)):
            continue
        for s in table.get_symbols():
            name = s.get_name()
            # Not local or a parameter: symtable calls every name of a function named
            # "top" global (it takes the name for the module's).
            if (s.is_referenced() and s.is_global() and not s.is_declared_global()
                    and not s.is_local() and not s.is_parameter() and not s.is_free()
                    and name not in known and (owner.get_name(), name) not in seen):
                seen.add((owner.get_name(), name))
                out.append((owner.get_name(), sp[0] if sp else owner.get_lineno(), name))
    return out


def _edited_lines(text: str, name: str, args: dict) -> set | None:
    if name == "Write":
        return None
    news = [args.get("new_string")] + [e.get("new_string") for e in args.get("edits") or []
                                       if isinstance(e, dict)]
    out = set()
    for new in news:
        if not isinstance(new, str) or not new.strip():
            continue
        at = text.find(new)
        if at < 0:
            continue
        first = text[:at].count("\n") + 1
        out.update(range(first, first + new.count("\n") + 1))
    return out


def _before(text: str, name: str, args: dict) -> str | None:
    """The file as it was before this Edit/MultiEdit, when that can be worked out."""
    if name == "Write":
        return None
    edits = [args] if name == "Edit" else [e for e in args.get("edits") or [] if isinstance(e, dict)]
    for edit in reversed(edits):
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or edit.get("replace_all"):
            return None
        if not new or text.count(new) != 1:
            return None
        text = text.replace(new, old, 1)
    return text


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content if isinstance(content, str) else ""


def _issues(use: dict, texts: dict) -> list:
    """[(path, function, def line, name)] this successful write left undefined, judged
    against the file as it is now."""
    args = _args(use.get("input"))
    path = args.get("file_path")
    if not isinstance(path, str) or not path.endswith(".py"):
        return []
    if path not in texts:
        texts[path] = None
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= _MAX_BYTES:
                with open(path, encoding="utf-8") as fh:
                    texts[path] = fh.read()
        except (OSError, UnicodeDecodeError):
            pass
    text = texts[path]
    if text is None:
        return []
    bad = undefined(text, path, _edited_lines(text, use.get("name"), args))
    if bad:
        # Only what this change introduced: a name the file already used undefined is
        # someone's deliberate `except NameError`.
        before = _before(text, use.get("name"), args)
        if before is None and "NameError" in text:
            return []
        had = {name for _, _, name in undefined(before, path)} if before else set()
        bad = [b for b in bad if b[2] not in had]
    return [(path, f, l, n) for f, l, n in bad]


def _describe(issues: list) -> str:
    return "; ".join(f"`{n}` in `{f}` ({os.path.basename(p)}, def at line {l})"
                     for p, f, l, n in issues[:6])


def _with_note(block: dict, note: str) -> dict:
    inner = block.get("content")
    inner = [{"type": "text", "text": inner}] if isinstance(inner, str) else list(inner or [])
    return {**block, "content": inner + [{"type": "text", "text": note}]}


def annotate(body: dict) -> tuple[dict, int]:
    """`body` with a note on each successful .py Edit/Write in the conversation that left a
    name undefined in a function it touched and is still undefined on disk - and, while any
    is, a reminder on the newest tool result, so the model cannot finish having lost it.

    Judged against the disk on every request: a fixed name drops out by itself."""
    messages = list(body.get("messages") or [])
    if len(messages) < 2 or messages[-1].get("role") != "user":
        return body, 0
    uses = {}
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "assistant" \
                and isinstance(message.get("content"), list):
            for b in message["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _WRITES:
                    uses[b.get("id")] = b
    if not uses:
        return body, 0
    texts, outstanding, added = {}, [], 0
    # Newest writes only: a long session's early edits are long since settled.
    recent = set(list(uses)[-40:])
    for i, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        blocks, touched = [], False
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_result"
                    and block.get("tool_use_id") in recent and not block.get("is_error")):
                issues = [x for x in _issues(uses[block["tool_use_id"]], texts)
                          if x not in outstanding]
                if issues:
                    outstanding += issues
                    block = _with_note(block, (
                        f"[bridge] Check: after this change the code uses names that are "
                        f"defined nowhere - {_describe(issues)}. Each will raise NameError when "
                        "that code runs (a rename or edit left an old name behind?). If your next "
                        "edits define them, carry on; otherwise fix them before you finish."))
                    touched = True
                    added += 1
            blocks.append(block)
        if touched:
            messages[i] = {**message, "content": blocks}
    if not outstanding:
        return body, 0
    last = messages[-1].get("content")
    if isinstance(last, list):
        at = max((k for k, b in enumerate(last) if isinstance(b, dict)
                  and b.get("type") == "tool_result"), default=None)
        if at is not None and "[bridge] Check:" not in _result_text(last[at]):
            last = list(last)
            last[at] = _with_note(last[at], (
                f"[bridge] Still undefined on disk: {_describe(outstanding)}. Fix these before "
                "you answer."))
            messages[-1] = {**messages[-1], "content": last}
    return {**body, "messages": messages}, added
