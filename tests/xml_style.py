"""Pin a test module to the XML prompt layout.

The bridge writes Markdown by default (see agentaus_bridge/prompt_style.py). The modules
that import this were written against the tagged layout and assert on it - and many of
their fake upstreams recognise which prompt they were sent by its tags. They stay as the
regression suite for AGENTAUS_PROMPT_STYLE=xml, which is still supported and is the
baseline the Markdown layout is measured against. test_markdown_prompts.py covers the
same behaviour in the default layout.

    from xml_style import setUpModule, tearDownModule  # noqa: F401

The prompt diet (on by default, see agentaus_bridge/diet.py) is pinned off here too: these
modules assert on the full guidance, the zoom and investigate tools and the skill library,
which the diet leaves out. test_diet.py covers the diet.
"""

from agentaus_bridge.config import settings

# The full, pre-diet prompt: what the diet switches off when it is on.
FULL_PROMPT = {
    "agentaus_prompt_diet": False,
    "agentaus_zoom": True,
    "agentaus_investigate": True,
    "agentaus_skill_routing": True,
    "agentaus_skill_library": True,
}

_saved = []


def pin_full_prompt() -> dict:
    saved = {k: getattr(settings, k) for k in FULL_PROMPT}
    for k, v in FULL_PROMPT.items():
        setattr(settings, k, v)
    return saved


def restore(saved: dict) -> None:
    for k, v in saved.items():
        setattr(settings, k, v)


def setUpModule():
    _saved.append((settings.agentaus_prompt_style, pin_full_prompt()))
    settings.agentaus_prompt_style = "xml"


def tearDownModule():
    style, saved = _saved.pop() if _saved else ("markdown", {})
    settings.agentaus_prompt_style = style
    restore(saved)
