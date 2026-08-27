"""Repairing the typography a model applies to things that are not prose.

Agentaus rewrites ASCII hyphens as U+2011 and wraps identifiers in `$$...$$`. Left
alone, a file path in an answer cannot be copied, clicked or searched for, and a
summary fed back into the conversation carries a path that no longer resolves.

Kept apart from the compactor because it is a string utility with no dependencies, and
because the translator needs it too - importing it from `compact` there is a circular
import, which is the honest signal that it never belonged in the compactor.
"""

from __future__ import annotations

import re
import unicodedata


_TYPOGRAPHIC = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2212": "-", "\u00a0": " ", "\u202f": " ",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
}

_MATH_WRAPPED = re.compile(r"\$\$\s*([^$\n]{1,120}?)\s*\$\$")


def normalise_identifiers(text: str) -> str:
    """Undo typographic substitutions so identifiers survive verbatim."""
    if not text:
        return text
    text = unicodedata.normalize("NFKC", text)
    for fancy, plain in _TYPOGRAPHIC.items():
        text = text.replace(fancy, plain)
    return _MATH_WRAPPED.sub(r"`\1`", text)


# Characters that are never a deliberate choice in an answer and always break what they
# appear in. A non-breaking hyphen looks exactly like a hyphen and is not one, so a
# filename carrying one cannot be copied, clicked, or found - observed live, in a table
# of real documents that a citation check correctly rejected as not existing.
#
# En dash, em dash and smart quotes are deliberately NOT here. They are ordinary
# typography in prose, and flattening "the bridge - which caps at 6 -" to make a
# hypothetical identifier safe trades a real cost for a speculative one. Inside code
# spans they are normalised anyway, which is where an identifier actually lives.
_ALWAYS_WRONG = {"\u2010": "-", "\u2011": "-", "\u00a0": " ", "\u202f": " "}

_CODE_SPAN = re.compile(r"`([^`\n]{1,200})`")


def normalise_for_display(text: str) -> str:
    """Repair an answer on its way to the client, without flattening its prose.

    Narrower than `normalise_identifiers`, which is right for search results and
    summaries the model will re-read and wrong for text a person is about to read.
    """
    if not text:
        return text
    for wrong, right in _ALWAYS_WRONG.items():
        text = text.replace(wrong, right)
    # Inside backticks everything is an identifier, so the full treatment applies.
    return _CODE_SPAN.sub(lambda m: "`" + normalise_identifiers(m.group(1)) + "`", text)
