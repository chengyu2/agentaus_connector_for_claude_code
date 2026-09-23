"""Pin a test module to the XML prompt layout.

The bridge writes Markdown by default (see agentaus_bridge/prompt_style.py). The modules
that import this were written against the tagged layout and assert on it - and many of
their fake upstreams recognise which prompt they were sent by its tags. They stay as the
regression suite for AGENTAUS_PROMPT_STYLE=xml, which is still supported and is the
baseline the Markdown layout is measured against. test_markdown_prompts.py covers the
same behaviour in the default layout.

    from xml_style import setUpModule, tearDownModule  # noqa: F401
"""

from agentaus_bridge.config import settings

_saved = []


def setUpModule():
    _saved.append(settings.agentaus_prompt_style)
    settings.agentaus_prompt_style = "xml"


def tearDownModule():
    settings.agentaus_prompt_style = _saved.pop() if _saved else "markdown"
