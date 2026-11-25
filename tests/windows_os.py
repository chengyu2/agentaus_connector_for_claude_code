"""`os` as a module sees it on native Windows, for tests that run on macOS and Linux.

Patching a module's `os` with this gives it ntpath (case-insensitive, either slash, drive
letters), a backslash `sep`, `;` between search roots and `os.name == "nt"`. Everything else -
os.walk, os.stat, open files - is the real `os`, so the test still touches the real disk.
Only that module's view changes; the process's own `os` and `os.path` are left alone.
"""

from __future__ import annotations

import ntpath
import os


class Proxy:
    """`base` with some attributes replaced."""

    def __init__(self, base, **overrides):
        self._base = base
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._base, name)


def windows_os(path=ntpath, **overrides) -> Proxy:
    """An `os` whose path module is `path` (ntpath unless given) and whose separators and
    name are Windows'."""
    attrs = {"path": path, "sep": "\\", "altsep": "/", "pathsep": ";", "name": "nt"}
    attrs.update(overrides)
    return Proxy(os, **attrs)
