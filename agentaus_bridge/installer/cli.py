"""`agentaus-bridge <command>` - the commands behind the installed shim.

The shim runs `python -m agentaus_bridge <command>` from the install folder, and
__main__ hands any first argument that is not a flag to main() here. With no command
at all the shim shows `status`; `run` starts the server in the foreground.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .. import components as C
from . import actions as A
from .ui import UI, Aborted

USAGE = """usage: agentaus-bridge <command> [options]

  status       what is installed, and whether the bridge is answering
  doctor       status, plus a live check of the API key
  setup        run the guided installer again (setup --help for its options)
  components   add or remove components
  run          start the bridge in this terminal
  start        start the background service
  stop         stop the background service
  restart      restart the background service
  logs         follow the bridge's log
  update       pull the latest version and reinstall its packages
  uninstall    remove the settings, service and command the installer added
"""


def main(argv: list[str]) -> int:
    command, rest = argv[0], argv[1:]
    # The reader lookups read AGENTAUS_SOFFICE_PATH from the environment, as the bridge does
    # after loading .env; without it, status would call a LibreOffice the bridge finds missing.
    soffice = A.read_env(A.ROOT / ".env").get("AGENTAUS_SOFFICE_PATH")
    if soffice and "AGENTAUS_SOFFICE_PATH" not in os.environ:
        os.environ["AGENTAUS_SOFFICE_PATH"] = soffice
    if command in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"unknown command {command!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    try:
        return handler(rest)
    except Aborted:
        print("\nStopped.")
        return 130


def _setup(rest: list[str]) -> int:
    from . import flow
    return flow.main(rest)


def _components(rest: list[str]) -> int:
    from . import flow
    return flow.main(["--components-only", *rest])


def _port() -> int:
    try:
        return int(A.read_env(A.ROOT / ".env").get("BRIDGE_PORT") or A.DEFAULT_PORT)
    except ValueError:
        return A.DEFAULT_PORT


def _status(rest: list[str], ui: UI | None = None) -> int:
    ui = ui or UI(assume_yes=True)
    env = A.read_env(A.ROOT / ".env")
    state = A.load_state()
    chosen = C.parse(env.get(C.ENV_KEY))
    ui.title("Agentaus bridge")
    ui.info(f"Installed in {A.ROOT}")
    rows = []
    broken = 0
    for c in C.available():
        picked = chosen is None or c.key in chosen
        if c.kind == "setup":
            done = {"settings": bool(state.get("settings")), "service": "service" in state,
                    "command": "command" in state}[c.key]
            status = "[green]set up[/green]" if done else "[dim]not set up[/dim]"
        elif C.present(c):
            status = "[green]installed[/green]"
        elif picked:
            status = f"[red]missing[/red] - {C.install_hint(c)}"
            broken += 1
        else:
            status = "[dim]skipped[/dim]"
        rows.append([c.title, status])
    ui.print("")
    ui.table(["Component", "State"], rows)
    ui.print("")
    port = _port()
    health = A.health(port)
    if health:
        ui.ok(f"Bridge answering on http://127.0.0.1:{port}")
        if not health.get("agentaus_key_configured"):
            ui.warn("but it has no AGENTAUS_API_KEY")
    else:
        ui.warn(f"Nothing answering on http://127.0.0.1:{port}")
    key = env.get("AGENTAUS_API_KEY", "")
    (ui.ok if key else ui.warn)(f"API key in .env: {A.mask(key) if key else 'not set'}")
    for entry in state.get("settings", []):
        ui.ok(f"Claude Code settings: {entry['path']}")
    if not state.get("settings"):
        ui.info("[dim]Claude Code settings: not written by the installer[/dim]")
    return 1 if broken else 0


def _doctor(rest: list[str]) -> int:
    ui = UI(assume_yes=True)
    _status(rest, ui)
    ui.step("Live check of the API key")
    # Check the key in .env, the one the service uses, not one exported in this shell.
    env = {k: v for k, v in os.environ.items()
           if k not in ("AGENTAUS_API_KEY", C.ENV_KEY, "BRIDGE_PORT")}
    try:
        proc = subprocess.run([str(A.venv_python()), "-m", "agentaus_bridge", "--check"],
                              cwd=str(A.ROOT), env=env, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        ui.error("Agentaus did not answer within 90 seconds")
        return 1
    lines = [l for l in (proc.stdout + proc.stderr).splitlines() if l.strip()]
    for line in lines[-6:]:
        ui.info(f"[dim]{line}[/dim]")
    (ui.ok if proc.returncode == 0 else ui.error)(
        "The key works" if proc.returncode == 0 else "The check failed")
    return proc.returncode


def _run(rest: list[str]) -> int:
    python = str(A.venv_python())
    os.chdir(A.ROOT)
    if A.WINDOWS:
        # Windows has no exec: os.execv starts a new process and returns to the prompt.
        try:
            return subprocess.call([python, "-m", "agentaus_bridge", *rest])
        except KeyboardInterrupt:
            return 0
    os.execv(python, [python, "-m", "agentaus_bridge", *rest])
    return 0  # not reached


def _service_action(action: str):
    def handler(rest: list[str]) -> int:
        service = A.load_state().get("service")
        if not service:
            print("No background service is installed. Run the bridge in a terminal with "
                  "`agentaus-bridge run`, or add the service with `agentaus-bridge setup`.")
            return 1
        code = 0
        for cmd in A.service_commands(service["kind"], action):
            quiet = cmd[:2] == ["launchctl", "unload"] and action != "stop"
            code = subprocess.run(cmd, stdout=subprocess.DEVNULL if quiet else None,
                                  stderr=subprocess.DEVNULL if quiet else None).returncode
        return code
    return handler


def _logs(rest: list[str]) -> int:
    kind = (A.load_state().get("service") or {}).get("kind")
    if kind != "systemd" and not Path(A.LOG_FILE).exists():
        print(f"No log at {A.LOG_FILE} yet. A bridge started with `run` logs to its terminal.")
        return 1
    return subprocess.run(A.logs_command(kind)).returncode


def _update(rest: list[str]) -> int:
    ui = UI(assume_yes=True)
    ui.title("Updating the Agentaus bridge")
    dry = "--dry-run" in rest
    if (A.ROOT / ".git").exists():
        ui.command(["git", "pull", "--ff-only"])
        if dry:
            ui.info("[dim](dry run: not pulled)[/dim]")
        elif subprocess.run(["git", "pull", "--ff-only"], cwd=str(A.ROOT)).returncode != 0:
            ui.error("git pull failed; resolve it in the install folder and try again")
            return 1
    else:
        ui.warn("Not a git checkout. Run the installer again to fetch a new version.")
    # Reinstall the chosen components' Python packages and restart a running service,
    # asking nothing and never running sudo. In a new process, because this one still has
    # the modules from before the pull loaded.
    return subprocess.call([str(A.venv_python()), "-m", "agentaus_bridge", "components",
                            "--yes", "--no-system", *rest], cwd=str(A.ROOT))


def _uninstall(rest: list[str]) -> int:
    ui = UI(assume_yes="--yes" in rest or "-y" in rest)
    runner = A.Runner(dry_run="--dry-run" in rest)
    state = A.load_state()
    ui.title("Uninstall the Agentaus bridge")
    for entry in list(state.get("settings", [])):
        path = Path(entry["path"])
        ui.info(f"Undo the bridge's env keys in {path}")
        if ui.confirm("Undo them?", True):
            try:
                undone = A.unmerge_settings(runner, path, entry.get("env", {}), entry.get("previous"))
            except A.SettingsError as exc:
                ui.error(f"{exc}. Left it, and kept it on the list for the next uninstall.")
                continue
            restored = [k for k in undone if (entry.get("previous") or {}).get(k) is not None]
            if undone:
                ui.ok(f"Removed {len(undone) - len(restored)} key(s)"
                      + (f", restored the earlier value of {', '.join(restored)}" if restored else ""))
            else:
                ui.ok("Nothing of ours left there")
            state["settings"].remove(entry)
    service = state.get("service")
    if service:
        ui.info(f"Stop and remove the background service ({service['path']})")
        if ui.confirm("Remove it?", True):
            for cmd in A.service_commands(service["kind"], "remove"):
                runner.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if service["kind"] != "schtasks":
                runner.remove(Path(service["path"]))
            if service["kind"] == "systemd":
                runner.run(["systemctl", "--user", "daemon-reload"])
            state.pop("service")
            ui.ok("Service removed")
    command = state.get("command")
    if command:
        ui.info(f"Remove the command {command}")
        if ui.confirm("Remove it?", True):
            runner.remove(Path(command))
            state.pop("command")
            ui.ok("Command removed")
    A.save_state(runner, state)
    ui.print("")
    ui.info("The bridge itself, its virtualenv and your .env (with the key) are still in:")
    ui.info(f"  {A.ROOT}")
    ui.info("Delete that folder to remove them. System packages (LibreOffice, poppler, "
            "tesseract) were left installed, because other software may use them.")
    if state.get("settings") or "service" in state:
        ui.warn("Some pieces were kept. Restart Claude Code once the settings no longer point at the bridge.")
    else:
        ui.info("Restart Claude Code so it stops looking for the bridge.")
    return 0


COMMANDS = {
    "setup": _setup,
    "components": _components,
    "status": _status,
    "doctor": _doctor,
    "run": _run,
    "start": _service_action("start"),
    "stop": _service_action("stop"),
    "restart": _service_action("restart"),
    "logs": _logs,
    "update": _update,
    "uninstall": _uninstall,
}
