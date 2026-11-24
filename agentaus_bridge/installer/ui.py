"""Prompts and output for the installer.

questionary draws the menus and rich the colour when they are installed (the bootstrap
installs both from requirements/setup.txt). Without them every prompt falls back to plain
input(), so `agentaus-bridge uninstall` still works on an install made from requirements.txt.
With assume_yes no prompt is shown at all: each returns its default.
"""

from __future__ import annotations

import getpass
import re
import sys

try:
    import questionary
except ImportError:  # pragma: no cover - depends on what is installed
    questionary = None
try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None

_MARKUP = re.compile(r"\[/?[a-z0-9 #._-]*\]")


class Aborted(Exception):
    """The user pressed Ctrl-C or Esc at a prompt."""


class UI:
    def __init__(self, assume_yes: bool = False, interactive: bool | None = None,
                 fancy: bool | None = None, out=None):
        self.assume_yes = assume_yes
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.out = out or sys.stdout
        fancy = (questionary is not None and Console is not None) if fancy is None else fancy
        self.fancy = fancy and self.interactive and not assume_yes
        self.console = Console(file=self.out, highlight=False) if Console is not None and fancy else None

    # --- output ----------------------------------------------------------------------

    def print(self, message: str = "") -> None:
        if self.console is not None:
            self.console.print(message)
        else:
            print(_plain(message), file=self.out)
        try:
            self.out.flush()
        except Exception:  # noqa: BLE001
            pass

    def title(self, text: str) -> None:
        self.print(f"\n[bold]{text}[/bold]")
        self.print("[dim]" + "─" * min(60, max(len(text), 20)) + "[/dim]")

    def step(self, text: str) -> None:
        self.print(f"\n[bold cyan]── {text} [/bold cyan][dim]{'─' * max(4, 44 - len(text))}[/dim]")

    def info(self, text: str) -> None:
        self.print(f"  {text}")

    def ok(self, text: str) -> None:
        self.print(f"  [green]✓[/green] {text}")

    def warn(self, text: str) -> None:
        self.print(f"  [yellow]![/yellow] {text}")

    def error(self, text: str) -> None:
        self.print(f"  [red]✗[/red] {text}")

    def command(self, argv_or_text) -> None:
        text = argv_or_text if isinstance(argv_or_text, str) else " ".join(_quote(a) for a in argv_or_text)
        self.print(f"    [bold]$[/bold] [magenta]{_escape(text)}[/magenta]")

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        if self.console is not None and Table is not None:
            table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
            for h in headers:
                table.add_column(h)
            for row in rows:
                table.add_row(*row)
            self.console.print(table)
            return
        plain = [[_plain(c) for c in r] for r in [headers] + rows]
        widths = [max(len(r[i]) for r in plain) for i in range(len(headers))]
        for r in plain:
            print("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip(), file=self.out)

    # --- prompts ---------------------------------------------------------------------

    def confirm(self, question: str, default: bool = True) -> bool:
        if self.assume_yes:
            return default
        self._need_tty()
        if self.fancy:
            return _ask(questionary.confirm(question, default=default))
        suffix = " [Y/n] " if default else " [y/N] "
        while True:
            answer = _input("  " + question + suffix).strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def select(self, question: str, choices: list[tuple[str, str]], default: str | None = None) -> str:
        """choices: (label, value). Returns the value."""
        if self.assume_yes:
            return default if default is not None else choices[0][1]
        self._need_tty()
        if self.fancy:
            qchoices = [questionary.Choice(label, value=value) for label, value in choices]
            start = next((c for c in qchoices if c.value == default), None)
            return _ask(questionary.select(question, choices=qchoices, default=start))
        self.print(f"  {question}")
        for i, (label, value) in enumerate(choices, 1):
            mark = "*" if value == default else " "
            self.print(f"   {mark}{i}. {label}")
        while True:
            answer = _input("  Choose a number (enter for *): ").strip()
            if not answer and default is not None:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1][1]

    def checkbox(self, question: str, choices: list[tuple[str, str, bool, bool]]) -> list[str]:
        """choices: (label, value, checked, locked). Locked ones are always included."""
        if self.assume_yes:
            return [v for _, v, checked, locked in choices if checked or locked]
        self._need_tty()
        if self.fancy:
            qchoices = [questionary.Choice(label, value=value, checked=checked or locked,
                                           disabled="required" if locked else None)
                        for label, value, checked, locked in choices]
            picked = _ask(questionary.checkbox(
                question, choices=qchoices,
                instruction="(space to toggle, a to toggle all, enter to confirm)"))
            return [v for _, v, _, locked in choices if locked or v in picked]
        current = {v for _, v, checked, locked in choices if checked or locked}
        while True:
            self.print(f"  {question}")
            for i, (label, value, _, locked) in enumerate(choices, 1):
                box = "[x]" if value in current else "[ ]"
                self.print(f"   {i:>2}. {_escape(box)} {_escape(label)}{'  (required)' if locked else ''}")
            answer = _input("  Numbers to toggle (e.g. 2 5), enter when done: ").strip()
            if not answer:
                return [v for _, v, _, _ in choices if v in current]
            for token in answer.replace(",", " ").split():
                if token.isdigit() and 1 <= int(token) <= len(choices):
                    _, value, _, locked = choices[int(token) - 1]
                    if not locked:
                        current ^= {value}

    def text(self, question: str, default: str = "") -> str:
        if self.assume_yes:
            return default
        self._need_tty()
        if self.fancy:
            return _ask(questionary.text(question, default=default))
        answer = _input(f"  {question}" + (f" [{default}] " if default else " "))
        return answer.strip() or default

    def secret(self, question: str) -> str:
        if self.assume_yes:
            return ""
        self._need_tty()
        if self.fancy:
            return _ask(questionary.password(question))
        try:
            return getpass.getpass(f"  {question} ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise Aborted() from exc

    def _need_tty(self) -> None:
        if not self.interactive:
            raise Aborted("no terminal to ask on; re-run with --yes (and --preset/--with) "
                          "to install without questions")


def _ask(question):
    answer = question.ask()
    if answer is None:  # questionary returns None on Ctrl-C
        raise Aborted()
    return answer


def _input(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise Aborted() from exc


def _quote(arg: str) -> str:
    return arg if re.fullmatch(r"[\w@%+=:,./-]+", arg) else "'" + arg.replace("'", "'\\''") + "'"


def _plain(text: str) -> str:
    """Markup removed, escaped brackets kept, for a terminal without rich."""
    return _MARKUP.sub("", text.replace("\\[", "\0")).replace("\0", "[")


def _escape(text: str) -> str:
    """Keep rich from reading [brackets] in commands and labels as markup."""
    return text.replace("[", "\\[")
