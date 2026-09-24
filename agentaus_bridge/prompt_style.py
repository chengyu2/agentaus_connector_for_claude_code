"""How the bridge lays out what it writes for Agentaus: Markdown, or XML tags.

Markdown by default. Every prompt the bridge composes - its operating notes, the plan,
review, grounding and refusal passes, search and compaction calls, and the framing it puts
around each tool result - goes out as headings, lists and fenced blocks.
`AGENTAUS_PROMPT_STYLE=xml` restores the tagged layout the bridge used before, byte for
byte, so the two can be measured against each other.

Only the bridge's own words are restyled. Anything substituted into a prompt - the user's
request, an answer under review, a file excerpt, a tool result - is carried verbatim
inside a fence. The fence is chosen when the prompt is filled in, one tilde longer than
any run of tildes in the content, so nothing inside it can close it early or be read as
structure. That is the job the closing tag did in the XML layout, and a heading alone
cannot do it: a heading says where a section starts, never where it ends.

Templates are still written once, in the tagged form, and rendered here at `.format()`
time. Keeping one source per prompt is what makes the switch safe - two hand-maintained
copies of every prompt would drift, and the A/B would then measure the drift.
"""

from __future__ import annotations

import re

from .config import settings


def markdown() -> bool:
    return (settings.agentaus_prompt_style or "markdown").strip().lower() != "xml"


def title(name: str) -> str:
    """`tools_it_actually_ran` -> `Tools it actually ran`."""
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def fence_for(body: str) -> str:
    longest = max((len(run) for run in re.findall(r"~{3,}", body or "")), default=0)
    return "~" * max(4, longest + 1)


def _xml_attrs(attrs: dict) -> str:
    return "".join(f' {key}="{value}"' for key, value in attrs.items())


def _md_attrs(attrs: dict) -> str:
    if not attrs:
        return ""
    return "\n" + "\n".join(
        f"- {key.replace('_', ' ')}: `{value}`" for key, value in attrs.items()
    ) + "\n"


def section(name: str, body: str) -> str:
    """Instructions the bridge wrote: `<name>` in XML, a level-2 heading in Markdown."""
    if not markdown():
        return f"<{name}>\n{body}\n</{name}>"
    return f"## {title(name)}\n\n{body.strip(chr(10))}"


def data(name: str, body: str, **attrs) -> str:
    """Verbatim content: fenced in Markdown so nothing inside it reads as structure."""
    if not markdown():
        return f"<{name}{_xml_attrs(attrs)}>\n{body}\n</{name}>"
    fence = fence_for(body)
    return f"### {title(name)}\n{_md_attrs(attrs)}\n{fence}\n{body}\n{fence}"


def ref(name: str) -> str:
    """How prose refers to a block: `<name>`, or the section's heading."""
    return f"<{name}>" if not markdown() else f'the "{title(name)}" section'


# A block holding exactly one substituted field. Its content is not the bridge's, so in
# Markdown it becomes a fenced block rather than a heading over loose text.
_DATA_BLOCK = re.compile(
    r'<(?P<name>[a-z_]+)(?P<attrs>(?: [a-z_]+="[^"]*")*)>\n'
    r"\{(?P<field>\w+)\}\n"
    r"</(?P=name)>"
)
_ATTR = re.compile(r'([a-z_]+)="([^"]*)"')


class Template(str):
    """A prompt written with XML tags, rendered in the configured style by `.format()`.

    A `str`, so it concatenates, compares and prints as the tagged source it was written
    as; only `.format()` - the one thing every call site does before sending - restyles.
    """

    def format(self, *args, **values) -> str:  # noqa: A003 - mirrors str.format
        source = str(self)
        if not markdown():
            return source.format(*args, **values)
        blocks = {m.group(1) for m in re.finditer(r"^(?:\{\w+\})?<([a-z_]+)[ >]", source, re.M)}
        out: list[str] = []
        position = 0
        for match in _DATA_BLOCK.finditer(source):
            out.append(_instructions(source[position:match.start()], blocks)
                       .format(*args, **values))
            attrs = {key: value.format(*args, **values)
                     for key, value in _ATTR.findall(match.group("attrs"))}
            out.append(data(match.group("name"), str(values[match.group("field")]), **attrs))
            position = match.end()
        out.append(_instructions(source[position:], blocks).format(*args, **values))
        return _tidy("".join(out))


def _instructions(text: str, blocks: set) -> str:
    """Restyle the bridge-written part of a template: tags to headings, references to
    section names. Only names this template actually uses as blocks are touched, so a
    literal like `<path>:<line>` in an output format survives as the instruction it is."""
    def opening(match: re.Match) -> str:
        return f"{match.group(1)}## {title(match.group(2))}\n\n"

    # A block may open straight after a placeholder that supplies its own line break:
    # the planning prompt is `{context}<task>`.
    text = re.sub(r"^((?:\{\w+\})?)<([a-z_]+)>\n", opening, text, flags=re.M)
    text = re.sub(r"^</([a-z_]+)>\n?", "", text, flags=re.M)
    text = re.sub(r"<([a-z_]+)>",
                  lambda m: ref(m.group(1)) if m.group(1) in blocks else m.group(0),
                  text, flags=re.M)
    return text


def _tidy(text: str) -> str:
    """Collapse the blank lines that removed closing tags leave behind - outside fences
    only, since a fence carries content verbatim."""
    out, fence = [], None
    blank = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if fence is None and re.fullmatch(r"~{4,}", stripped):
            fence = stripped
        elif fence is not None and stripped == fence:
            fence = None
        elif fence is None and not stripped:
            blank += 1
            if blank > 1:
                continue
            out.append(line)
            continue
        blank = 0
        out.append(line)
    return "\n".join(out)


def headings(text: str) -> str:
    """`--- Operating notes ---` dividers become level-2 headings in Markdown."""
    if not markdown():
        return text
    return re.sub(r"^--- (.+?) ---$", r"## \1", text, flags=re.M)
