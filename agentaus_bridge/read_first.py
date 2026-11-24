"""A `Read` put in front of an Edit or Write that Claude Code would refuse for want of one.

Claude Code refuses to change an existing file it has not read in this conversation ("File
has not been read yet. Read it first before writing to it.") or that changed on disk since it
last read or wrote it ("File has been modified since read, either by the user or by a
linter. Read it again before attempting to write it."). Either refusal costs a whole round
trip - on Agentaus 6-20 s - and a weak model often answers it by re-reading and retyping the
edit from memory, which is where indentation slips come in.

The bridge sees the whole conversation and the disk, so it can tell before the call reaches
the client. It emits a `Read` of the file ahead of the Edit in the same response. Claude Code
runs a response's read-only calls before the write that follows them, so the Read lands
first, the Edit passes its check, and the model's next turn has both results.

Not read yet: no successful Read, Write, Edit or MultiEdit of the path anywhere in the
conversation. Changed since: the bridge noted the file's mtime when the result of the last
such call came back (the request that carries it arrives right after the client ran it),
and the file is newer now - a Bash `sed -i`, a formatter, a test run that rewrote it.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import OrderedDict

_TOUCH = {"Read", "Write", "Edit", "MultiEdit"}
_CHANGE = {"Write", "Edit", "MultiEdit"}
# A larger file gets a one-line Read: enough to pass the check without spending the context
# on a file the model may only need one line of.
_FULL_READ_BYTES = 40_000
_LIMIT = 4096

_lock = threading.Lock()
# path -> mtime_ns when the bridge last saw the client finish a touch of it.
_seen: "OrderedDict[str, int]" = OrderedDict()


def _args(block_input) -> dict:
    if isinstance(block_input, dict):
        return block_input
    if isinstance(block_input, str):
        try:
            value = json.loads(block_input)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _mtime(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def touched(messages: list) -> set:
    """Paths the client has read or written successfully in this conversation."""
    uses = {}
    out = set()
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in _TOUCH:
                path = _args(block.get("input")).get("file_path")
                if isinstance(path, str):
                    uses[block.get("id")] = os.path.normpath(path)
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                if not block.get("is_error"):
                    out.add(uses[block["tool_use_id"]])
    return out


def observe(messages: list) -> int:
    """Note the mtime of every file whose touch result arrives in the newest message.

    Call it on each request as the client sent it, before anything else can change the
    files."""
    if not messages or not isinstance(messages[-1], dict):
        return 0
    content = messages[-1].get("content")
    if messages[-1].get("role") != "user" or not isinstance(content, list):
        return 0
    done = {b.get("tool_use_id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result" and not b.get("is_error")}
    if not done:
        return 0
    previous = next((m for m in reversed(messages[:-1])
                     if isinstance(m, dict) and m.get("role") == "assistant"), None)
    blocks = previous.get("content") if previous else None
    noted = 0
    for block in blocks if isinstance(blocks, list) else []:
        if (isinstance(block, dict) and block.get("type") == "tool_use"
                and block.get("name") in _TOUCH and block.get("id") in done):
            path = _args(block.get("input")).get("file_path")
            if not isinstance(path, str) or not os.path.isabs(path):
                continue
            stamp = _mtime(path)
            if stamp is None:
                continue
            path = os.path.normpath(path)
            with _lock:
                _seen[path] = stamp
                _seen.move_to_end(path)
                while len(_seen) > _LIMIT:
                    _seen.popitem(last=False)
            noted += 1
    return noted


def _read_call(path: str) -> dict:
    args = {"file_path": path}
    try:
        if os.path.getsize(path) > _FULL_READ_BYTES:
            args["limit"] = 1
    except OSError:
        pass
    return {"id": f"toolu_bridge_{uuid.uuid4().hex[:20]}", "name": "Read",
            "arguments": json.dumps(args), "added_by_bridge": True}


def prepend_reads(calls: list, messages: list) -> tuple[list, list]:
    """`calls` with a Read in front of each Edit/Write the client would refuse, and
    [(path, why)] for each Read added."""
    read = None
    added, out, done = [], [], set()
    for call in calls:
        if call.get("name") in _CHANGE:
            path = _args(call.get("arguments")).get("file_path")
            if isinstance(path, str) and os.path.isabs(path) and os.path.isfile(path):
                path = os.path.normpath(path)
                if read is None:
                    read = touched(messages)
                why = None
                if path in done:
                    pass
                elif path not in read:
                    why = "not read yet"
                else:
                    with _lock:
                        seen = _seen.get(path)
                    now = _mtime(path)
                    if seen is not None and now is not None and now > seen:
                        why = "changed on disk since read"
                if why:
                    out.append(_read_call(path))
                    added.append((path, why))
                    done.add(path)
                elif call.get("name") != "Read":
                    done.add(path)
        elif call.get("name") == "Read":
            path = _args(call.get("arguments")).get("file_path")
            if isinstance(path, str):
                done.add(os.path.normpath(path))
        out.append(call)
    return out, added
