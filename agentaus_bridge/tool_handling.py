"""Tool calls in a model reply: sorting, validating and correcting them,
and running the bridge's own tools."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx

from . import batching
from . import harmony
from . import helper_calls
from . import import_guard
from . import read_first
from . import repair
from . import schema as tool_schema
from . import tools as bridge_tools

from .config import settings
from .graph_turn import _graph_main_loop
from .request_log import log, rlog


def _on_the_wire(payload: dict) -> dict:
    """The body actually POSTed for an agent turn: with AGENTAUS_BATCH_TOOLS, `run_tools`
    added beside its tools (see batching.py). The payload the bridge
    keeps stays in the native shape, so everything that reads it is unaffected."""
    if not settings.agentaus_batch_tools:
        return payload
    return batching.wrap_payload(payload)


def _with_unrun_calls(payload: dict, calls: list) -> dict:
    """Answer client calls that were batched with a bridge tool and so never ran.

    The bridge-tool loop asks Agentaus again once its own tools have run, and the client's
    calls from the same reply are dropped. With one call per reply that never happened;
    with batching (batching.py) a search and a Read can arrive together, and the model must
    be told the Read did not run rather than left to assume it did."""
    if not calls:
        return payload
    rlog(logging.INFO, "%d client call(s) batched with a bridge tool were not run; "
         "asking again", len(calls))
    note = ("(not run: it was batched with a bridge tool, whose results are above. Call "
            "it again if you still need it.)")
    return _with_tool_results(payload, calls, [note] * len(calls))


def _openai_calls(data: dict) -> list:
    """Tool calls from a non-streaming Agentaus response, in the accumulator's shape."""
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    return [
        {
            "id": call.get("id") or "",
            "name": (call.get("function") or {}).get("name") or "",
            "arguments": (call.get("function") or {}).get("arguments") or "{}",
        }
        for call in (message.get("tool_calls") or [])
    ]


def _recover_harmony_calls(data: dict) -> tuple:
    """Pull tool calls the model wrote into its answer text back out of it.

    Returns the response with that markup removed, plus the calls it contained. A call
    written as prose is still a call the model meant to make, and the alternative is
    showing the user a line of angle brackets and calling it an answer.
    """
    choices = list(data.get("choices") or [])
    if not choices:
        return data, []
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    text = message.get("content") or ""
    if not harmony.looks_like_harmony(text):
        return data, []

    cleaned, calls = harmony.extract(text)
    message["content"] = cleaned or None
    if calls:
        # The calls must also go back into the response, not only out of it as dicts:
        # `_with_coerced_calls` writes name and argument corrections into
        # `message.tool_calls`, and the translator builds the client's tool_use blocks
        # from it. A call left only in the dicts is dropped by both - and with the
        # markup gone the answer reads as empty, so the empty-answer fallback replaced
        # the turn the model had already spent on a tool call.
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in calls
        ]
        # The turn ends in a tool call the client is to answer, whatever the prose
        # finish_reason said. Bridge-owned calls run their rounds and re-answer; client
        # calls reach the client with stop_reason tool_use.
        choice["finish_reason"] = "tool_calls"
    choice["message"] = message
    return {**data, "choices": [choice] + choices[1:]}, calls


def _with_forced_answer(data: dict, text: str) -> dict:
    """Put a forced answer into a response, clearing the tool calls it replaces."""
    choices = list(data.get("choices") or [{}])
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    message["content"] = text
    message["tool_calls"] = None
    choice["message"] = message
    choice["finish_reason"] = "stop"
    return {**data, "choices": [choice] + choices[1:]}


def _without_bridge_calls(data: dict, drop: set | None = None) -> dict:
    """Strip bridge-owned tool calls from a response.

    Needed when the tool-round budget runs out with one still pending: Claude Code has
    never heard of `agentaus_search` and would fail the tool_use rather than run it, so
    an unanswered bridge call must never be surfaced. `drop` names more calls by id: a
    `Skill` call for a library skill is the bridge's too, but its name doesn't say so.
    """
    choices = list(data.get("choices") or [])
    if not choices:
        return data
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    drop = drop or ()
    kept = [
        call for call in (message.get("tool_calls") or [])
        if ((call.get("function") or {}).get("name") or "") not in bridge_tools.BRIDGE_TOOLS
        and call.get("id") not in drop
    ]
    message["tool_calls"] = kept or None
    if not kept and not (message.get("content") or "").strip():
        message["content"] = (
            "I ran out of search rounds before finishing. Ask again and I will continue "
            "from what I found."
        )
    choice["message"] = message
    if not kept and choice.get("finish_reason") == "tool_calls":
        choice["finish_reason"] = "stop"
    return {**data, "choices": [choice] + choices[1:]}


def _known_tool_names(payload: dict) -> set:
    """Every tool name the model was actually offered, from the payload it was sent."""
    names = set()
    for tool in (payload or {}).get("tools") or []:
        name = ((tool or {}).get("function") or {}).get("name")
        if name:
            names.add(name)
    return names


def _canonical(name: str, known: set) -> str | None:
    """The offered tool `name` refers to, allowing for how models mangle names.

    `read` for `Read`, `agentaus-search` for `agentaus_search`. Observed live: a turn
    spent one of its three tool rounds being corrected from `read` to `Read`, which is
    not a mistake worth a round trip - it is the same tool, spelled carelessly.
    """
    if name in known:
        return name
    squashed = name.lower().replace("_", "").replace("-", "").replace(" ", "")
    for candidate in known:
        if candidate.lower().replace("_", "").replace("-", "").replace(" ", "") == squashed:
            return candidate
    return None


def _partition_tool_calls(
    calls: list, known: set | None = None, library: frozenset = frozenset()
) -> tuple[list, list, list]:
    """Split tool calls into (bridge-owned, client-owned, invented).

    The third bucket is not defensive programming - it is a failure observed on the
    first live Agentaus turn, which answered a search by calling a tool named
    `open_file` that no one had offered it. Passing that through means Claude Code
    fails a tool_use for a tool it has never heard of, and the turn dies on an error
    that looks like the bridge's fault.

    `known` is the set of names actually sent upstream. Without it nothing can be
    called invented, so the check is skipped rather than guessed at.
    """
    mine, theirs, invented = [], [], []
    for call in calls:
        name = call.get("name") or ""
        if known:
            pool = known | set(bridge_tools.BRIDGE_TOOLS)
            resolved = _canonical(name, pool) or repair.resolve(name, pool)
            if resolved is None:
                invented.append(call)
                continue
            if resolved != name:
                rlog(logging.INFO, "tool name %r resolved to %r", name, resolved)
                call = {**call, "name": resolved, "asked_as": name}
                name = resolved
        if name in bridge_tools.BRIDGE_TOOLS or _is_library_skill_call(name, call, library):
            mine.append(call)
        else:
            theirs.append(call)
    return mine, theirs, invented


def _is_library_skill_call(name: str, call: dict, library: frozenset) -> bool:
    """A `Skill` call naming one of the library skills this request's listing was given.

    Those are the bridge's to answer: Claude Code has never heard of them and would fail
    the call. `library` is passed in, not read from a context variable - the streaming
    path runs `prepare` as its own task, so a variable it set never reached this check.
    """
    if name != "Skill" or not library:
        return False
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return False
    skill = str((arguments or {}).get("skill") or "").strip()
    return bool(skill) and skill in library


def _correction_for(call: dict, known: set) -> str:
    """What to tell a model that called a tool which does not exist."""
    offered = ", ".join(sorted(known)) or "(none)"
    return (
        f"There is no tool named {call.get('name')!r}. You invented it. "
        f"The tools you actually have are: {offered}. "
        f"Call one of those by its exact name, or answer from what you already know. "
        f"Do not guess at a tool name again."
    )


def _tool_schemas(payload: dict) -> dict:
    """Each offered tool's input schema, by name - the client's own and the bridge's.

    Both live in the same payload: bridge tools are injected into `tools` alongside the
    client's before the request goes upstream, so one lookup validates either kind.
    """
    schemas = {}
    for tool in (payload or {}).get("tools") or []:
        function = (tool or {}).get("function") or {}
        name = function.get("name")
        if name:
            schemas[name] = function.get("parameters") or {}
    return schemas


def _validate_tool_calls(calls: list, schemas: dict, cwd: str | None = None) -> list:
    """Fix what is unambiguously fixable; report what is not.

    Coerced arguments are written back onto the call, so a double-encoded payload is
    repaired in place rather than spending a correction round. Returns
    `(call, message)` for every call that is still invalid - those must not be passed
    on, because the client will reject them and the turn is spent either way.
    """
    broken = []
    # What the Edits before each one in this response leave in their files: a second Edit to
    # a file is matched against the text after the first, not the disk. See repair.repair.
    pending: dict = {}
    for call in calls:
        schema = schemas.get(call.get("name") or "")
        if not schema:
            continue
        # Unambiguous slips first - a field under another name, a relative path - so
        # they cost nothing rather than a correction round. See repair.py.
        try:
            repaired = repair.repair(
                call, call.get("asked_as") or call.get("name") or "", schema, cwd, pending)
            if repaired.get("correction"):
                broken.append((call, repaired["correction"]))
                continue
            call["arguments"] = repaired["arguments"]
            fixed, problems = tool_schema.validate(call.get("arguments"), schema)
        except Exception as exc:
            # A schema this subset cannot read is the client's to judge. Forwarding the
            # call unchecked costs nothing; raising here ended the whole turn.
            rlog(logging.WARNING, "could not check arguments for %s (%s); forwarding "
                 "unchecked", call.get("name"), exc)
            continue
        if problems:
            broken.append((call, tool_schema.correction_for(
                call.get("name") or "", problems, schema)))
            continue
        encoded = json.dumps(fixed)
        if encoded != call.get("arguments"):
            log.debug("repaired arguments for %s", call.get("name"))
        call["arguments"] = encoded
    return broken


def _guard_imports(calls: list, received: dict, cwd: str | None) -> None:
    """Snapshot the .py files a graph main loop's calls are about to change, so the next request
    can tell whether the change broke importing them. See import_guard.py."""
    if settings.agentaus_import_guard and calls and _graph_main_loop(received):
        if import_guard.remember(calls, cwd):
            rlog(logging.INFO, "import guard: watching this turn's .py edits")


def _with_coerced_calls(data: dict, calls: list) -> dict:
    """Put repaired arguments back into a non-streaming response.

    The streaming path emits from the call dicts themselves, so writing back is enough
    there. This path emits from `data`, which the call dicts were only copied out of.
    """
    # Names too: a call resolved from `read` to `Read` must reach the client as `Read`.
    repaired = {call.get("id"): call for call in calls if call.get("id")}
    if not repaired:
        return data
    choices = list(data.get("choices") or [])
    if not choices:
        return data
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    rebuilt = []
    for call in message.get("tool_calls") or []:
        if call.get("id") in repaired:
            function = dict(call.get("function") or {})
            function["arguments"] = repaired[call["id"]].get("arguments")
            function["name"] = repaired[call["id"]].get("name") or function.get("name")
            call = {**call, "function": function}
        rebuilt.append(call)
    message["tool_calls"] = rebuilt
    choice["message"] = message
    return {**data, "choices": [choice] + choices[1:]}


def _reads_first(calls: list, received: dict) -> list:
    """`calls` with a Read ahead of each Edit/Write the client would refuse as unread or
    changed since read. See read_first.py."""
    if not settings.agentaus_read_first or not calls:
        return calls
    if "Read" not in {t.get("name") for t in received.get("tools") or [] if isinstance(t, dict)}:
        return calls
    try:
        out, added = read_first.prepend_reads(calls, received.get("messages") or [])
    except Exception as exc:  # a convenience; never the reason a turn fails
        rlog(logging.WARNING, "read-first check failed (%s); forwarding as is", exc)
        return calls
    for path, why in added:
        rlog(logging.INFO, "read first: added a Read of %s (%s)", path, why)
    return out


def _with_reads_first(data: dict, calls: list, received: dict) -> dict:
    """The non-streaming form of _reads_first: the added Reads put into `data`."""
    out = _reads_first(calls, received)
    if len(out) == len(calls):
        return data
    choices = list(data.get("choices") or [])
    if not choices:
        return data
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    by_id = {c.get("id"): c for c in message.get("tool_calls") or []}
    rebuilt = []
    for call in out:
        if call.get("added_by_bridge"):
            rebuilt.append({"id": call["id"], "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]}})
        elif call.get("id") in by_id:
            rebuilt.append(by_id.pop(call["id"]))
    rebuilt += list(by_id.values())
    message["tool_calls"] = rebuilt
    choice["message"] = message
    return {**data, "choices": [choice] + choices[1:]}


def _with_tool_results(payload: dict, calls: list, results: list) -> dict:
    """Append calls and their results to a payload, in OpenAI's shape.

    Assistant turn carrying the tool_calls, then one `tool` message per result - the
    same shape the translator produces for the client's own tools, so the model sees no
    difference between a tool the bridge ran and one Claude Code ran.
    """
    messages = list(payload.get("messages") or [])
    messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": call.get("arguments")
                        if isinstance(call.get("arguments"), str)
                        else json.dumps(call.get("arguments") or {}),
                    },
                }
                for call in calls
            ],
        }
    )
    for call, result in zip(calls, results):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id") or "",
                "content": result or "(no output)",
            }
        )
    return {**payload, "messages": messages}


def _without_unknown_calls(data: dict, known: set) -> dict:
    """Strip invented tool calls from a non-streaming response."""
    choices = list(data.get("choices") or [])
    if not choices:
        return data
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    kept = [
        c for c in (message.get("tool_calls") or [])
        if ((c.get("function") or {}).get("name") or "") in known
    ]
    message["tool_calls"] = kept or None
    if not kept and not (message.get("content") or "").strip():
        message["content"] = (
            "I tried to use a tool that does not exist and could not recover. "
            "Ask again and I will use one of the tools I actually have."
        )
    choice["message"] = message
    if not kept and choice.get("finish_reason") == "tool_calls":
        choice["finish_reason"] = "stop"
    return {**data, "choices": [choice] + choices[1:]}


# Free-text fields where a trivial difference in wording is not a different request. A
# semantic search does not care about a trailing bracket; an exact-match signature does,
# and that difference cost a real turn 29 seconds of duplicate fan-out.
_FUZZY_FIELDS = ("query", "question")


# Words that carry no search intent. "Where is the gate" and "the gate, where is it?"
# are one question; the grammar around the nouns is not what is being looked for.
_FUNCTION_WORDS = frozenset("""
a an the is are was were be been am do does did of in on at to for from by with about
this that these those it its what which where when who whom how why and or but if then
i you we they me my our your their there here s
""".split())


def _same_request(text: str) -> str:
    """Reduce a natural-language argument to what actually distinguishes it.

    Observed live: a turn searched `should_think`, then searched `should_think(`, and the
    second ran the whole fan-out again for a result it already had. Those are one
    question, and so are two phrasings of the same sentence.

    Words are lowercased, stripped of punctuation, sorted, and cleared of grammar - a
    search is a bag of intent rather than a sentence. The content words themselves are
    kept, so two genuinely different questions still miss each other, which is the
    direction that matters: a false miss costs a repeated search, a false hit answers a
    question with someone else's result.

    Stripping is skipped only when it would leave nothing at all, so a query that is
    entirely grammar - "what is it" - still distinguishes itself from another one.
    """
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in (text or "").lower())
    words = kept.split()
    content = [_singular(word) for word in words if word not in _FUNCTION_WORDS]
    return " ".join(sorted(content or words))


def _singular(word: str) -> str:
    """Fold a trailing plural or third-person `s`, so `cap` and `caps` are one word.

    Crude on purpose. It only has to make two phrasings of one question agree, and a
    word it mangles is mangled identically on both sides, so a wrong stem costs nothing.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"                      # retries -> retry
    if len(word) > 4 and word.endswith(("sses", "xes", "zes", "ches", "shes")):
        return word[:-2]                            # classes -> class, boxes -> box
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]                            # caps -> cap
    return word                                     # class, process: not plurals


def _call_signature(call: dict) -> str:
    """A stable key for "this request, already made", for spotting a repeat."""
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": arguments}
    arguments = arguments or {}
    if isinstance(arguments, dict):
        arguments = {
            key: _same_request(value) if key in _FUZZY_FIELDS and isinstance(value, str)
            else value
            for key, value in arguments.items()
        }
    return json.dumps(
        {"n": call.get("name") or "", "a": arguments}, sort_keys=True, default=str
    )


async def _run_bridge_tools(
    client: httpx.AsyncClient,
    payload: dict,
    calls: list,
    default_path: str | None = None,
    seen: dict | None = None,
) -> dict:
    """Execute bridge-owned tool calls and return the payload to send next.

    The results are appended as an assistant turn carrying the tool_calls plus one
    `tool` message per result - OpenAI's shape, matching what the translator produces
    for the client's own tools. Claude Code never sees any of it.

    `seen` remembers what has already run this turn. It has to, because the tool ledger
    cannot: bridge tool calls never enter the Anthropic message list, so the ledger -
    which is derived from that list - is blind to exactly the tools most likely to be
    repeated. Observed live: one turn made twelve bridge calls, zooming the same passage
    twice and re-running a 58-second search, then exhausted its round budget with the
    work unfinished. The model was doing the thing `augment.py` already documents, on the
    only tools nothing was watching.
    """
    seen = seen if seen is not None else {}

    async def one(call: dict) -> str:
        signature = _call_signature(call)
        if signature in seen:
            # Answered from cache, and said so plainly. Repeating the work would cost a
            # round and a minute to hand back bytes the model already has.
            rlog(logging.WARNING, "bridge tool %s repeated with identical arguments; "
                 "returning the earlier result", call.get("name"))
            return (
                "[You already ran this exact call earlier in this turn. This is the same "
                "result, not a new one. Use it and move on - do not call it again.]\n"
                + seen[signature]
            )

        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        started_at = time.monotonic()
        result = await bridge_tools.execute(
            call.get("name") or "", arguments or {},
            lambda text: helper_calls._agentaus_summarise(client, text),
            default_path,
        )
        rlog(logging.INFO, "bridge tool %s ran in %.1fs -> %d chars",
             call.get("name"), time.monotonic() - started_at, len(result))
        seen[signature] = result
        return result

    results = await asyncio.gather(*[one(call) for call in calls])
    return _with_tool_results(payload, calls, results)
