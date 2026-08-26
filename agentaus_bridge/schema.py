"""Checking tool arguments against the schema the client published for that tool.

The bridge already refuses to pass on a tool *name* the model invented. It passed on
any *arguments* at all, so a well-named call with malformed input reached Claude Code,
was rejected there, and cost the turn:

    InputValidationError: [{"code": "too_small", "minimum": 2,
                            "path": ["questions", 0, "options"]}]

That is Agentaus offering a question with one option to choose from. The client is right
to reject it, but the rejection arrives as a wall of Zod internals - `origin`, `code`,
`inclusive` - which tells a model very little about what to do differently. Catching it
here turns it into a sentence.

Two things happen, in order. Arguments that are *unambiguously* fixable are fixed
silently, on the same principle that already resolves `read` to `Read`: a model that
double-encodes its JSON has not made a decision worth a round trip. Whatever is still
wrong afterwards becomes a correction the model can act on.

This is a deliberate subset of JSON Schema - the keywords Claude Code's tools actually
use. A validator that covers the whole specification would be larger, slower, and no
better at the only job here, which is catching the handful of ways a language model
gets a tool call wrong.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("agentaus-bridge")

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
}


def _named(path: str) -> str:
    return f"`{path}`" if path else "the arguments"


def coerce(value, schema: dict):
    """Fix what can only have been meant one way, and leave the rest alone.

    Every case here is a formatting slip rather than a wrong decision: a JSON object
    sent as a string containing JSON, a lone item where a list of items belongs, "true"
    for true. Recursing into properties and items matters because these slips nest -
    the double-encoded thing is usually two levels down.
    """
    if not isinstance(schema, dict):
        return value
    expected = schema.get("type")

    # Models routinely send a JSON document as a string. Decode it once, only when the
    # result is the shape the schema asked for - never turning "42" into 42 for a field
    # that wanted a string.
    if isinstance(value, str) and expected in ("object", "array"):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, _TYPES[expected]):
            value = decoded

    if expected == "array" and not isinstance(value, list) and value is not None:
        value = [value]
    elif expected == "boolean" and isinstance(value, str):
        if value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
    elif expected in ("number", "integer") and isinstance(value, str):
        try:
            number = float(value)
            value = int(number) if expected == "integer" and number.is_integer() else number
        except ValueError:
            pass
    elif expected == "string" and isinstance(value, (int, float, bool)):
        value = json.dumps(value) if isinstance(value, bool) else str(value)

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        return {
            key: coerce(item, properties[key]) if key in properties else item
            for key, item in value.items()
        }
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [coerce(item, schema["items"]) for item in value]
    return value


def check(value, schema: dict, path: str = "") -> list[str]:
    """Every way `value` breaks `schema`, phrased for the model that produced it."""
    if not isinstance(schema, dict) or not schema:
        return []
    problems: list[str] = []

    expected = schema.get("type")
    if expected in _TYPES and value is not None:
        # bool is an int in Python and never what a numeric field wants.
        wrong_type = not isinstance(value, _TYPES[expected]) or (
            expected in ("number", "integer") and isinstance(value, bool)
        )
        if wrong_type:
            actual = type(value).__name__
            return [f"{_named(path)} must be {expected}, but you sent {actual}."]

    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value or value[key] is None:
                problems.append(
                    f"{_named(path)} is missing the required field `{key}`."
                )
        properties = schema.get("properties") or {}
        for key, item in value.items():
            if key in properties:
                problems += check(item, properties[key], f"{path}.{key}" if path else key)

    elif isinstance(value, list):
        minimum, maximum = schema.get("minItems"), schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            problems.append(
                f"{_named(path)} needs at least {minimum} items; you gave {len(value)}."
            )
        if isinstance(maximum, int) and len(value) > maximum:
            problems.append(
                f"{_named(path)} takes at most {maximum} items; you gave {len(value)}."
            )
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                problems += check(item, schema["items"], f"{path}[{index}]")

    elif isinstance(value, str):
        minimum, maximum = schema.get("minLength"), schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            problems.append(f"{_named(path)} must be at least {minimum} characters.")
        if isinstance(maximum, int) and len(value) > maximum:
            problems.append(
                f"{_named(path)} must be at most {maximum} characters; "
                f"yours is {len(value)}."
            )

    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            problems.append(f"{_named(path)} must be at least {minimum}.")
        if isinstance(maximum, (int, float)) and value > maximum:
            problems.append(f"{_named(path)} must be at most {maximum}.")

    allowed = schema.get("enum")
    if isinstance(allowed, list) and allowed and value not in allowed:
        shown = ", ".join(json.dumps(option) for option in allowed[:8])
        problems.append(f"{_named(path)} must be one of: {shown}.")

    return problems


def validate(arguments, schema: dict) -> tuple[dict, list[str]]:
    """Coerce then check. Returns the arguments to send and what is still wrong."""
    if not isinstance(schema, dict) or not schema:
        return arguments if isinstance(arguments, dict) else {}, []
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except (ValueError, TypeError):
            return {}, ["The arguments were not valid JSON. Send a JSON object."]
    if not isinstance(arguments, dict):
        return {}, [f"The arguments must be a JSON object, not {type(arguments).__name__}."]
    fixed = coerce(arguments, schema)
    return fixed, check(fixed, schema)


def correction_for(name: str, problems: list[str], schema: dict) -> str:
    """What to tell a model whose tool call will be rejected if it is passed on.

    Names the tool, states each fault as a sentence, and re-states the required fields.
    Deliberately not the client's validator output: a model given `{"code":"too_small",
    "inclusive":true}` tends to reply with an apology rather than a corrected call.
    """
    required = ", ".join(f"`{key}`" for key in (schema.get("required") or []))
    faults = " ".join(problems[:6])
    tail = f" Required fields for `{name}`: {required}." if required else ""
    return (
        f"Your call to `{name}` was not valid and was not run. {faults}{tail} "
        f"Call `{name}` again with those corrected, or use a different tool."
    )
