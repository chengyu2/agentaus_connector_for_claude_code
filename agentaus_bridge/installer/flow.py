"""The guided setup behind `python -m agentaus_bridge setup` (and scripts/install.sh).

Pick components, see the plan, then confirm each step that reaches outside the install
folder: sudo package installs, Claude Code's settings.json, the service, the command.
--yes answers every question with its default, --dry-run changes nothing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from .. import components as C
from . import actions as A
from .ui import UI, Aborted

RECOMMENDED = "recommended"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentaus-bridge setup",
        description="Install or reconfigure the Agentaus bridge, one confirmed step at a time.",
    )
    p.add_argument("-y", "--yes", action="store_true",
                   help="ask nothing: take each question's default (the recommended components, "
                        "settings for every project, the key from $AGENTAUS_API_KEY)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen and change nothing")
    p.add_argument("--preset", choices=list(C.PRESETS), help="start from this set of components")
    p.add_argument("--with", dest="with_", default="", metavar="A,B",
                   help="add components (" + ", ".join(C.BY_KEY) + ")")
    p.add_argument("--without", default="", metavar="A,B", help="leave components out")
    p.add_argument("--settings", metavar="SCOPE",
                   help="where Claude Code learns about the bridge: user (every project), none, "
                        "or a project directory")
    p.add_argument("--port", type=int, help="port for the bridge (default 8787, or what .env has)")
    p.add_argument("--no-check", action="store_true", help="skip the live check of the API key")
    p.add_argument("--plain", action="store_true", help="plain numbered prompts instead of menus")
    p.add_argument("--components-only", action="store_true", help=argparse.SUPPRESS)
    return p


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def system_commands(needed: list[C.Component], manager: str, as_root: bool) -> list[list[str]]:
    """The package-manager commands that install these system components."""
    sudo = [] if as_root else ["sudo"]
    if manager == "brew":
        formulas = [p for c in needed for p in c.packages.get("brew", ()) if "brew-cask" not in c.packages]
        casks = [p for c in needed for p in c.packages.get("brew-cask", ())]
        return ([["brew", "install", *formulas]] if formulas else []) + \
               ([["brew", "install", "--cask", *casks]] if casks else [])
    if manager == "winget":
        return [["winget", "install", "-e", "--id", p, "--accept-source-agreements",
                 "--accept-package-agreements"] for c in needed for p in c.packages.get("winget", ())]
    packages = [p for c in needed for p in c.packages.get(manager, ())]
    if not packages:
        return []
    if manager == "apt":
        return [sudo + ["apt-get", "update"], sudo + ["apt-get", "install", "-y", *packages]]
    if manager == "dnf":
        return [sudo + ["dnf", "install", "-y", *packages]]
    if manager == "pacman":
        return [sudo + ["pacman", "-S", "--needed", "--noconfirm", *packages]]
    return []


class Setup:
    def __init__(self, args: argparse.Namespace, ui: UI, runner: A.Runner, root: Path | None = None):
        root = root or A.ROOT
        self.args = args
        self.ui = ui
        self.runner = runner
        self.root = root
        self.platform = C.platform_name()
        self.env = A.read_env(root / ".env")
        self.state = A.load_state(root)
        self.keys: list[str] = []
        self.port = A.DEFAULT_PORT
        self.service_running = False
        self.settings_done = False

    # --- the whole run -----------------------------------------------------------------

    def run(self) -> int:
        ui = self.ui
        ui.title("Agentaus bridge installer")
        if self.args.dry_run:
            ui.warn("[bold]Dry run:[/bold] nothing will be installed or changed.")
        v = sys.version_info
        ui.ok(f"Python {v.major}.{v.minor}.{v.micro}")
        ui.ok(f"Installing into {self.root}")

        self.keys = self.choose_components()
        if not self.args.components_only:
            self.port = self.choose_port()
        self.show_plan()
        if not ui.confirm("Continue?", True):
            raise Aborted()

        self.install_system()
        self.install_python()
        updates = {C.ENV_KEY: ",".join(self.keys)}
        if self.port != A.DEFAULT_PORT or "BRIDGE_PORT" in self.env:
            updates["BRIDGE_PORT"] = str(self.port)
        A.write_env(self.runner, self.root, updates)
        self.state["components"] = self.keys

        if not self.args.components_only:
            self.api_key()
            if "settings" in self.keys:
                self.settings()
            if "service" in self.keys:
                self.service()
            if "command" in self.keys:
                self.command()
        elif "service" in self.state:
            self.restart_service()
        A.save_state(self.runner, self.state, self.root)
        self.finish()
        return 0

    # --- components --------------------------------------------------------------------

    def _present(self) -> set[str]:
        return {c.key for c in C.available(self.platform) if C.present(c)}

    def choose_components(self) -> list[str]:
        ui, args = self.ui, self.args
        ui.step("Components")
        previous = C.parse(self.env.get(C.ENV_KEY))
        present = self._present()
        with_, without = _split(args.with_), _split(args.without)
        try:
            if args.preset or with_ or without or ui.assume_yes:
                base = C.preset(args.preset, self.platform) if args.preset else \
                    (previous or C.preset(RECOMMENDED, self.platform))
                picked = [k for k in dict.fromkeys(base + with_) if k not in without]
                if "core" in without:
                    ui.warn("core cannot be left out; keeping it")
            else:
                picked = self._ask_components(previous, present)
            keys = C.resolve(picked, self.platform)
        except ValueError as exc:
            ui.error(str(exc))
            raise SystemExit(2) from exc
        for key in keys:
            if key not in picked:
                needed_by = [C.BY_KEY[k].title for k in keys if key in C.BY_KEY[k].requires]
                ui.info(f"Added {C.BY_KEY[key].title}"
                        + (f" (needed by {', '.join(needed_by)})" if needed_by else ""))
        for key in without:
            if key in keys and key != "core":
                ui.warn(f"Kept {C.BY_KEY[key].title}: another chosen component needs it")
        return keys

    def _ask_components(self, previous: list[str] | None, present: set[str]) -> list[str]:
        ui = self.ui
        choices = []
        if previous:
            choices.append((f"Keep the current selection ({', '.join(previous)})", "current"))
        choices += [
            ("Recommended - PDFs, code outline, settings, service (no LibreOffice, no OCR)", RECOMMENDED),
            ("Minimal - just the bridge and Claude Code settings", "minimal"),
            ("Full - everything, including LibreOffice (~500 MB) and OCR", "full"),
            ("Custom - choose each component", "custom"),
        ]
        choice = ui.select("What should be installed?", choices, default=choices[0][1])
        if choice == "current":
            return list(previous)
        if choice != "custom":
            return C.preset(choice, self.platform)
        start = set(previous or C.preset(RECOMMENDED, self.platform)) | present
        boxes = []
        for c in C.available(self.platform):
            notes = [c.size] if c.size else []
            if c.key in present:
                notes.append("already installed")
            elif c.kind == "system":
                manager = C.package_manager(self.platform)
                notes.append(f"via {manager}" if manager else "install by hand")
                if manager in ("apt", "dnf", "pacman") and not _is_root():
                    notes.append("needs sudo")
            if c.requires:
                notes.append("needs " + ", ".join(C.BY_KEY[r].title.split(" (")[0] for r in c.requires))
            label = f"{c.title}" + (f"  [{'; '.join(notes)}]" if notes else "")
            boxes.append((label, c.key, c.key in start, c.locked))
        return ui.checkbox("Choose components", boxes)

    def show_plan(self) -> None:
        ui = self.ui
        present = self._present()
        rows = []
        for c in C.available(self.platform):
            if c.key in self.keys:
                if c.kind == "setup":
                    status = "[cyan]will set up[/cyan]"
                elif c.key in present:
                    status = "[green]installed ✓[/green]"
                else:
                    status = "[yellow]will install[/yellow]"
            else:
                status = "[dim]skipped[/dim]"
            rows.append([c.title, c.size or "-", status])
        ui.print("")
        ui.table(["Component", "Size", "Plan"], rows)
        skipped = [c for c in C.available(self.platform) if c.runtime and c.key not in self.keys]
        if skipped:
            ui.print("")
            ui.info("[dim]Skipping means:[/dim]")
            for c in skipped:
                extra = " (it is installed, so the bridge still uses it)" if c.key in present else ""
                ui.info(f"[dim]- {c.title}: {c.loses}{extra}[/dim]")
        if not self.args.components_only:
            ui.print("")
            ui.info(f"The bridge will listen on 127.0.0.1:{self.port}.")

    def choose_port(self) -> int:
        ui = self.ui
        try:
            port = int(self.args.port or self.env.get("BRIDGE_PORT") or A.DEFAULT_PORT)
        except ValueError:
            port = A.DEFAULT_PORT
        if A.port_free(port):
            return port
        if A.health(port):
            ui.ok(f"An Agentaus bridge already answers on port {port}; keeping that port")
            return port
        alternative = A.next_free_port(port)
        ui.warn(f"Port {port} is in use by another program")
        if ui.confirm(f"Use port {alternative} instead?", True):
            return alternative
        return port

    # --- installs ----------------------------------------------------------------------

    def install_system(self) -> None:
        ui = self.ui
        present = self._present()
        needed = [C.BY_KEY[k] for k in self.keys
                  if C.BY_KEY[k].kind == "system" and k not in present]
        if not needed:
            return
        ui.step("System packages")
        manager = C.package_manager(self.platform)
        if manager is None:
            ui.warn("No supported package manager found (brew, apt, dnf, pacman, winget). "
                    "Install these yourself:")
            for c in needed:
                ui.info(f"- {c.title}: {C.install_hint(c, self.platform)}")
            return
        commands = system_commands(needed, manager, _is_root())
        if not commands:
            return
        if commands[0][0] == "sudo" and not _which("sudo"):
            ui.warn("These need root and sudo is not installed. Run them as root, then re-run setup:")
            for cmd in commands:
                ui.command(cmd[1:])
            return
        ui.info(f"Installs {', '.join(c.title for c in needed)} with:")
        for cmd in commands:
            ui.command(cmd)
        if not ui.confirm("Run these commands?", True):
            ui.warn("Skipped. The bridge will say what it cannot read when it starts.")
            return
        for cmd in commands:
            if self.runner.run(cmd) != 0:
                ui.warn(f"`{' '.join(cmd)}` failed; carrying on with the rest")
                if manager == "pacman":
                    ui.info("If pacman could not find a package, its database is out of date: "
                            "run `sudo pacman -Syu`, then setup again.")
        _forget_cached_lookups()
        if self.args.dry_run:
            return
        for c in needed:
            if C.present(c):
                ui.ok(f"{c.title} installed")
            else:
                ui.warn(f"{c.title} still not found. Install by hand: {C.install_hint(c, self.platform)}")

    def install_python(self) -> None:
        ui = self.ui
        groups = ["core"] + [C.BY_KEY[k].pip for k in self.keys
                             if C.BY_KEY[k].kind == "python" and k != "core" and C.BY_KEY[k].pip]
        argv = [str(A.venv_python(self.root)), "-m", "pip", "install", "--quiet",
                "--disable-pip-version-check"]
        for g in groups:
            argv += ["-r", str(Path("requirements") / f"{g}.txt")]
        ui.step("Python packages")
        ui.info(f"Into the virtualenv in {self.root / '.venv'}:")
        ui.command(argv)
        if self.runner.run(argv, cwd=str(self.root)) != 0:
            ui.error("pip failed; see its output above.")
            raise SystemExit(1)
        ui.ok("Python packages installed")

    # --- key ---------------------------------------------------------------------------

    def api_key(self) -> None:
        ui = self.ui
        ui.step("Agentaus API key")
        existing = self.env.get("AGENTAUS_API_KEY", "")
        if ui.assume_yes:
            key = os.environ.get("AGENTAUS_API_KEY", "") or existing
            if not key:
                ui.warn("No key given. Put AGENTAUS_API_KEY in .env, or re-run with it exported.")
                return
        elif existing and ui.confirm(f"Keep the key already in .env ({A.mask(existing)})?", True):
            key = existing
        else:
            ui.info("Create one in the API dashboard at https://agentaus.com.au")
            while True:
                key = ui.secret("Paste your Agentaus API key (input hidden):").strip()
                if key:
                    break
                if ui.confirm("Carry on without a key? You can add it to .env later.", False):
                    return
        if key != existing:
            A.write_env(self.runner, self.root, {"AGENTAUS_API_KEY": key})
            self.env["AGENTAUS_API_KEY"] = key
            ui.ok(f"Saved to {self.root / '.env'} (readable only by you)")
        if self.args.no_check or self.args.dry_run:
            return
        while True:
            ui.info("Checking the key with a live call to Agentaus...")
            ok, message = self.check_key()
            if ok:
                ui.ok(message)
                return
            ui.error(message)
            if ui.assume_yes:
                ui.warn("Carrying on. Check again later with: agentaus-bridge --check")
                return
            choice = ui.select("What now?", [("Enter a different key", "new"), ("Try again", "retry"),
                                             ("Carry on anyway", "skip")], default="new")
            if choice == "skip":
                return
            if choice == "new":
                key = ui.secret("Paste your Agentaus API key (input hidden):").strip()
                if key:
                    A.write_env(self.runner, self.root, {"AGENTAUS_API_KEY": key})

    def check_key(self) -> tuple[bool, str]:
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENTAUS_API_KEY", C.ENV_KEY, "BRIDGE_PORT")}
        try:
            proc = subprocess.run([str(A.venv_python(self.root)), "-m", "agentaus_bridge", "--check"],
                                  cwd=str(self.root), env=env, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return False, "Agentaus did not answer within 90 seconds"
        lines = [l for l in (proc.stdout + proc.stderr).splitlines() if l.strip()]
        for line in lines:
            if "OK - Agentaus replied" in line:
                return True, "Agentaus replied - the key works"
        detail = next((l for l in reversed(lines) if "HTTP" in l or "failed" in l), lines[-1] if lines else "")
        hint = " (the key is wrong or expired)" if "401" in detail else ""
        return False, f"The check failed{hint}: {detail.split(': ', 1)[-1][:200]}"

    # --- Claude Code settings ----------------------------------------------------------

    def settings(self) -> None:
        ui = self.ui
        ui.step("Claude Code settings")
        scope = self.args.settings
        if scope is None:
            if ui.assume_yes:
                scope = "user"
            else:
                ui.info("Claude Code only sees the bridge if a settings file it loads says so.")
                scope = ui.select("Where should Agentaus appear?", [
                    ("Every project (~/.claude/settings.json)", "user"),
                    ("One project (its .claude/settings.local.json)", "project"),
                    ("Skip - I'll set it up myself", "none"),
                ], default="user")
                if scope == "project":
                    start = os.environ.get("AGENTAUS_INSTALL_CWD") or str(Path.home())
                    scope = ui.text("Project directory:", default=start)
        if scope == "none":
            ui.info("Skipped. See \"Pointing Claude Code at the bridge\" in the README.")
            return
        if scope != "user" and not Path(scope).expanduser().is_dir():
            ui.error(f"{scope} is not a directory; skipping the settings step")
            return
        path = A.settings_path(scope)
        wanted = A.bridge_env(self.port)
        try:
            changes = A.settings_changes(A.load_settings(path), wanted)
        except A.SettingsError as exc:
            ui.error(str(exc))
            return
        if not changes:
            ui.ok(f"{path} already points Claude Code at the bridge")
        else:
            ui.info(f"Adds to the \"env\" block of {path}:")
            for key, old, new in changes:
                if old is None:
                    ui.print(f"    [green]+ \"{key}\": \"{new}\"[/green]")
                else:
                    ui.print(f"    [yellow]~ \"{key}\": \"{old}\" → \"{new}\"[/yellow]")
            ui.info("[dim]Everything else in the file is kept, and a backup is made first.[/dim]")
            if not ui.confirm("Apply?", True):
                ui.warn("Skipped.")
                return
            backup = A.merge_settings(self.runner, path, wanted)
            ui.ok(f"Updated {path}" + (f" (backup: {backup.name})" if backup else ""))
        self.settings_done = True
        entries = [e for e in self.state.get("settings", []) if e.get("path") != str(path)]
        self.state["settings"] = entries + [{"path": str(path), "env": wanted}]
        if scope == "user" and "service" not in self.keys:
            ui.warn("Every Claude Code session now goes through the bridge, so while it is not "
                    "running Claude models fail too. The background service keeps it running.")

    # --- service -----------------------------------------------------------------------

    def service(self) -> None:
        ui = self.ui
        ui.step("Background service")
        kind = A.service_kind()
        if kind is None:
            ui.warn("No launchd, systemd user session or Task Scheduler here, so the bridge "
                    "will not start on its own.")
            return
        path = A.service_file(kind)
        existed = path is not None and path.exists()
        what = {"launchd": "a launchd agent", "systemd": "a systemd user service",
                "schtasks": f"a Task Scheduler task ({A.TASK_NAME}) that runs at logon"}[kind]
        ui.info(f"Installs {what}, which starts the bridge when you log in and restarts it if it dies:")
        ui.info(f"[dim]{path if path else 'log: ' + A.LOG_FILE}[/dim]")
        if not ui.confirm("Install and start it?", True):
            ui.warn("Skipped.")
            return
        if path is not None:
            self.runner.write(path, A.service_text(kind, self.root))
        self.state["service"] = {"kind": kind, "path": str(path) if path else A.TASK_NAME}
        if not self._service_do(kind, "restart" if existed else "start"):
            return
        if kind == "systemd" and A.linger_enabled() is False:
            ui.info("A user service stops when you log out unless lingering is on.")
            if ui.confirm("Keep it running after logout (loginctl enable-linger)?", True):
                user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
                if self.runner.run(["loginctl", "enable-linger", user]) != 0:
                    ui.warn("loginctl enable-linger failed; try it with sudo")
        self._wait_for_health(kind)

    def restart_service(self) -> None:
        service = self.state.get("service") or {}
        kind = service.get("kind")
        if kind and (kind == "schtasks" or Path(service.get("path", "")).exists()):
            self.ui.step("Background service")
            if self._service_do(kind, "restart"):
                self._wait_for_health(kind)

    def _service_do(self, kind: str, action: str) -> bool:
        for cmd in A.service_commands(kind, action, self.root):
            quiet = cmd[:2] == ["launchctl", "unload"]
            code = self.runner.run(cmd, stdout=subprocess.DEVNULL if quiet else None,
                                   stderr=subprocess.DEVNULL if quiet else None)
            if code != 0 and not quiet:
                self.ui.error(f"Starting the service failed (exit {code}): {' '.join(cmd)[:300]}")
                return False
        return True

    def _wait_for_health(self, kind: str) -> None:
        if self.args.dry_run:
            return
        for _ in range(40):
            if A.health(self.port, timeout=1):
                self.service_running = True
                self.ui.ok(f"The bridge is running on http://127.0.0.1:{self.port}")
                return
            time.sleep(0.5)
        logs = "journalctl --user -u agentaus-bridge" if kind == "systemd" else A.LOG_FILE
        self.ui.warn(f"The service started but the bridge is not answering yet. Check: {logs}")

    # --- command -----------------------------------------------------------------------

    def command(self) -> None:
        ui = self.ui
        ui.step("agentaus-bridge command")
        path = A.command_path()
        ui.info(f"Adds {path} with status, start, stop, logs, doctor, update, components and uninstall.")
        if not ui.confirm("Add it?", True):
            ui.warn("Skipped.")
            return
        self.runner.write(path, A.command_text(self.root), mode=0o755)
        self.state["command"] = str(path)
        ui.ok(f"Added {path}")
        if A.on_path(path.parent):
            return
        if A.WINDOWS:
            ui.info(f"{path.parent} is not on your PATH yet.")
            if ui.confirm("Add it to your user PATH? (new terminals will see it)", True):
                if self.runner.run(A.add_to_user_path_command(path.parent)) == 0:
                    ui.ok("Added. Open a new terminal to use agentaus-bridge.")
                else:
                    ui.warn("Could not change PATH; add the folder in System Properties instead.")
            return
        shell = Path(os.environ.get("SHELL", "bash")).name
        rc = {"zsh": "~/.zshrc", "fish": "~/.config/fish/config.fish"}.get(shell, "~/.bashrc")
        ui.warn(f"{path.parent} is not on your PATH. Add it with:")
        line = ("fish_add_path ~/.local/bin" if shell == "fish"
                else "export PATH=\"$HOME/.local/bin:$PATH\"")
        ui.command(f"echo '{line}' >> {rc}")

    # --- end ---------------------------------------------------------------------------

    def finish(self) -> None:
        ui = self.ui
        ui.title("Dry run finished - nothing was changed" if self.args.dry_run else "All done")
        has_command = "command" in self.state
        cmd = "agentaus-bridge" if has_command else f"{A.venv_python(self.root)} -m agentaus_bridge"
        if self.args.components_only:
            ui.info(f"Components: {', '.join(self.keys)}")
            return
        steps = []
        if not self.service_running:
            script = "start-bridge.ps1" if A.WINDOWS else "start-bridge.sh"
            start = "agentaus-bridge run" if has_command else str(self.root / "scripts" / script)
            steps.append(f"Start the bridge:  {start}   (leave it running)")
        if self.settings_done:
            steps.append("Restart Claude Code (in VS Code: \"Developer: Reload Window\"), "
                         "run /model and pick Agentaus (Trellis Data)")
        else:
            steps.append("Point Claude Code at the bridge: see \"Pointing Claude Code at the bridge\" in the README")
        for i, s in enumerate(steps, 1):
            ui.info(f"{i}. {s}")
        ui.print("")
        ui.info(f"[dim]Check it:          {cmd} doctor[/dim]")
        ui.info(f"[dim]Change components: {cmd} components[/dim]")
        ui.info(f"[dim]Remove it:         {cmd} uninstall[/dim]")


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _which(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def _forget_cached_lookups() -> None:
    """documents.soffice() caches its answer; a reader installed just now must be seen."""
    try:
        from .. import documents
        documents._looked = False
        documents._binary = None
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    ui = UI(assume_yes=args.yes, fancy=False if args.plain else None)
    if not ui.assume_yes and not ui.interactive:
        ui.error("No terminal to ask questions on. Re-run with --yes (and --preset or --with) "
                 "to install without questions.")
        return 2
    runner = A.Runner(dry_run=args.dry_run)
    try:
        return Setup(args, ui, runner).run()
    except Aborted as exc:
        ui.print("")
        ui.warn(f"Stopped{': ' + str(exc) if str(exc) else ''}. Nothing after this point was changed; "
                "run setup again to pick up where you left off.")
        return 130
