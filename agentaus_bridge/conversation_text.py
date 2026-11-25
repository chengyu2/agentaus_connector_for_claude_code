"""Reading what the user asked out of a request's messages."""

from __future__ import annotations

import re


def _head_and_tail(text: str, head: int, tail: int) -> str:
    """The start and end of a long reply - where it says what it is, and how it ends."""
    if len(text) <= head + tail:
        return text
    return text[:head] + f"\n[... {len(text) - head - tail} characters ...]\n" + text[-tail:]


_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _without_reminders(text: str) -> str:
    """The user's words, without Claude Code's <system-reminder> blocks.

    Claude Code puts its own notes - the account email, commit attribution rules, which
    tools have loaded - in the user message beside what the user typed. Handed to the
    planner, reviewer or turn judge as "the request", they were read as part of the task.
    """
    return _REMINDER.sub("", text or "").strip()


def _launches_skill(block) -> bool:
    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
        return False
    text = block.get("content")
    if isinstance(text, list):
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return "Launching skill:" in str(text or "")


def _user_texts(body: dict) -> list:
    """What the user said, message by message: no system reminders, no background-task
    reports, and no skill bodies.

    A skill's body arrives as user text after its `Skill` call ("Launching skill: ...").
    Measured (d12 c1913, he10): the 17.5 KB workflow-authoring reference became the graph's
    "Follow-up:", in every agent's prompt - solver prompts of about 40 k characters.
    """
    out = []
    in_skill = False
    for message in body.get("messages") or []:
        content = message.get("content")
        if message.get("role") == "assistant":
            # Everything the user side sends until the next assistant turn is the skill's.
            in_skill = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill"
                for b in content)
            continue
        if message.get("role") != "user":
            continue
        if isinstance(content, list) and any(_launches_skill(b) for b in content):
            in_skill = True
        if in_skill:
            continue
        if isinstance(content, str):
            text = _without_reminders(content)
        elif isinstance(content, list):
            text = _without_reminders("\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"))
        else:
            text = ""
        if text and "<task-notification>" not in text:
            out.append(text)
    return out


def _last_user_text(body: dict) -> str:
    """The most recent thing the user actually said, as the request a pass judges against."""
    texts = _user_texts(body)
    return texts[-1] if texts else ""


def _original_request(body: dict) -> str:
    """What the conversation was started to do - the first thing the user said."""
    texts = _user_texts(body)
    return texts[0] if texts else ""
