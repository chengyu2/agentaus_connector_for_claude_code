"""Everything the installer changes on the machine, each one dry-run aware.

Kept apart from the prompts so each piece can be tested on a temporary directory: the .env
editor, the settings.json merge, the service files and the command shim.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = ".install-state.json"
SERVICE_LABEL = "com.trellisdata.agentaus-bridge"
SYSTEMD_UNIT = "agentaus-bridge.service"
WINDOWS = sys.platform.startswith("win")
TASK_NAME = "AgentausBridge"
LOG_FILE = (os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.home()),
                         "agentaus-bridge.log") if WINDOWS else "/tmp/agentaus-bridge.log")
DEFAULT_PORT = 8787


def venv_python(root: Path | None = None) -> Path:
    root = root or ROOT
    if sys.platform.startswith("win"):
        candidate = root / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = root / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


@dataclass
class Runner:
    """Runs commands and writes files, or with dry_run only says what it would do."""
    dry_run: bool = False
    log: list[str] = field(default_factory=list)

    def run(self, argv: list[str], check: bool = False, **kwargs) -> int:
        self.log.append("run: " + " ".join(argv))
        if self.dry_run:
            return 0
        try:
            return subprocess.run(argv, check=check, **kwargs).returncode
        except FileNotFoundError:
            return 127

    def write(self, path: Path, text: str, mode: int | None = None) -> None:
        self.log.append(f"write: {path}")
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)

    def remove(self, path: Path) -> None:
        self.log.append(f"remove: {path}")
        if not self.dry_run and path.exists():
            path.unlink()

    def copy(self, src: Path, dst: Path) -> None:
        self.log.append(f"copy: {src} -> {dst}")
        if not self.dry_run:
            shutil.copy2(src, dst)


# --- .env ------------------------------------------------------------------------------

def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def update_env_text(text: str, updates: dict[str, str]) -> str:
    """Set each key: replace its live line, else uncomment `# KEY=`, else append."""
    lines = text.splitlines()
    pending = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        key = stripped.partition("=")[0].strip()
        if "=" in stripped and not stripped.startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip()
        key = stripped.partition("=")[0].strip()
        if line.strip().startswith("#") and "=" in stripped and " " not in key and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{k}={v}" for k, v in pending.items())
    return "\n".join(lines) + "\n"


def write_env(runner: Runner, root: Path, updates: dict[str, str]) -> Path:
    """Update .env (created from .env.example), readable only by its owner: it holds the key."""
    path = root / ".env"
    if path.is_file():
        base = path.read_text(encoding="utf-8")
    elif (root / ".env.example").is_file():
        base = (root / ".env.example").read_text(encoding="utf-8")
    else:
        base = ""
    runner.write(path, update_env_text(base, updates), mode=stat.S_IRUSR | stat.S_IWUSR)
    return path


def mask(key: str) -> str:
    return (key[:4] + "…" + key[-4:]) if len(key) > 12 else "••••"


# --- installer state -------------------------------------------------------------------

def load_state(root: Path | None = None) -> dict:
    root = root or ROOT
    try:
        return json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(runner: Runner, state: dict, root: Path | None = None) -> None:
    root = root or ROOT
    runner.write(root / STATE_FILE, json.dumps(state, indent=2) + "\n")


# --- Claude Code settings ---------------------------------------------------------------

def bridge_env(port: int) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Agentaus (Trellis Data)",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Sovereign Australian model via the local Agentaus bridge",
    }


def settings_path(scope: str) -> Path:
    """`user` for every project, otherwise a project directory."""
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    return Path(scope).expanduser().resolve() / ".claude" / "settings.local.json"


class SettingsError(Exception):
    pass


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SettingsError(f"{path} is not valid JSON ({exc}); fix it or add the env block by hand") from exc
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise SettingsError(f"{path} does not hold a JSON object with an \"env\" object")
    return data


def settings_changes(data: dict, wanted: dict[str, str]) -> list[tuple[str, str | None, str]]:
    """(key, old value or None, new value) for every key that would change."""
    env = data.get("env", {})
    return [(k, env.get(k), v) for k, v in wanted.items() if env.get(k) != v]


def merge_settings(runner: Runner, path: Path, wanted: dict[str, str]) -> Path | None:
    """Merge the env block into the file, keeping everything else. Returns the backup."""
    data = load_settings(path)
    backup = None
    if path.exists():
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        runner.copy(path, backup)
    data.setdefault("env", {}).update(wanted)
    runner.write(path, json.dumps(data, indent=2) + "\n")
    return backup


def unmerge_settings(runner: Runner, path: Path, written: dict[str, str]) -> list[str]:
    """Remove the keys the installer wrote, where they still hold what it wrote."""
    try:
        data = load_settings(path)
    except SettingsError:
        return []
    env = data.get("env", {})
    removed = [k for k, v in written.items() if env.get(k) == v]
    if not removed:
        return []
    for k in removed:
        del env[k]
    if not env:
        data.pop("env", None)
    runner.write(path, json.dumps(data, indent=2) + "\n")
    return removed


# --- port ------------------------------------------------------------------------------

def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def health(port: int, timeout: float = 2.0) -> dict | None:
    """The bridge's /healthz on this port, or None if nothing bridge-like answers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) and "agentaus_url" in data else None


def next_free_port(start: int) -> int:
    for port in range(start + 1, start + 200):
        if port_free(port):
            return port
    raise RuntimeError("no free port found")


# --- background service ----------------------------------------------------------------

def service_kind() -> str | None:
    """launchd, systemd (user), schtasks (Windows Task Scheduler), or None."""
    if sys.platform == "darwin":
        return "launchd"
    if WINDOWS:
        return "schtasks" if shutil.which("powershell") else None
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        probe = subprocess.run(["systemctl", "--user", "show-environment"],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            return "systemd"
    return None


def service_file(kind: str) -> Path | None:
    """The file that defines the service. A scheduled task lives in Task Scheduler instead."""
    if kind == "schtasks":
        return None
    if kind == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def service_text(kind: str, root: Path | None = None) -> str:
    root = root or ROOT
    python = venv_python(root)
    if kind == "launchd":
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{SERVICE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>agentaus_bridge</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{root}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{LOG_FILE}</string>
  <key>StandardErrorPath</key><string>{LOG_FILE}</string>
</dict>
</plist>
"""
    return f"""[Unit]
Description=Agentaus bridge for Claude Code
After=network-online.target

[Service]
WorkingDirectory={root}
ExecStart={python} -m agentaus_bridge
Restart=on-failure

[Install]
WantedBy=default.target
"""


def _ps(script: str) -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", script]


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def task_register_script(root: Path | None = None) -> str:
    """Register a logon task that runs the bridge with no console window (pythonw), its
    output going to LOG_FILE through agentaus_bridge.background, restarted if it exits."""
    root = root or ROOT
    pythonw = venv_python(root).with_name("pythonw.exe")
    return "; ".join([
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(str(pythonw))} "
        f"-Argument '-m agentaus_bridge.background' -WorkingDirectory {_ps_quote(str(root))}",
        # No double quotes anywhere: Windows' argv quoting and powershell.exe disagree on them.
        "$t = New-ScheduledTaskTrigger -AtLogOn -User ($env:USERDOMAIN + '\\' + $env:USERNAME)",
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 "
        "-RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew",
        f"Register-ScheduledTask -TaskName {TASK_NAME} -Action $a -Trigger $t -Settings $s "
        "-Description 'Agentaus bridge for Claude Code' -Force | Out-Null",
    ])


def service_commands(kind: str, action: str, root: Path | None = None) -> list[list[str]]:
    """action: start (install and start), restart, stop, remove (stop for good), status."""
    if kind == "schtasks":
        return {
            "start": [_ps(task_register_script(root) + f"; Start-ScheduledTask -TaskName {TASK_NAME}")],
            "restart": [_ps(f"Stop-ScheduledTask -TaskName {TASK_NAME}; "
                            f"Enable-ScheduledTask -TaskName {TASK_NAME} | Out-Null; "
                            f"Start-ScheduledTask -TaskName {TASK_NAME}")],
            "stop": [_ps(f"Stop-ScheduledTask -TaskName {TASK_NAME}; "
                         f"Disable-ScheduledTask -TaskName {TASK_NAME} | Out-Null")],
            "remove": [_ps(f"Stop-ScheduledTask -TaskName {TASK_NAME} -ErrorAction SilentlyContinue; "
                           f"Unregister-ScheduledTask -TaskName {TASK_NAME} -Confirm:$false")],
            "status": [_ps(f"Get-ScheduledTask -TaskName {TASK_NAME} | Select-Object TaskName, State")],
        }[action]
    path = str(service_file(kind))
    if kind == "launchd":
        return {
            "start": [["launchctl", "unload", path], ["launchctl", "load", "-w", path]],
            "restart": [["launchctl", "unload", path], ["launchctl", "load", "-w", path]],
            "stop": [["launchctl", "unload", path]],
            "remove": [["launchctl", "unload", path]],
            "status": [["launchctl", "list", SERVICE_LABEL]],
        }[action]
    return {
        "start": [["systemctl", "--user", "daemon-reload"],
                  ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT]],
        "restart": [["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", SYSTEMD_UNIT],
                    ["systemctl", "--user", "restart", SYSTEMD_UNIT]],
        "stop": [["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT]],
        "remove": [["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT]],
        "status": [["systemctl", "--user", "--no-pager", "status", SYSTEMD_UNIT]],
    }[action]


def logs_command(kind: str | None) -> list[str]:
    if kind == "systemd":
        return ["journalctl", "--user", "-u", SYSTEMD_UNIT, "-f"]
    if WINDOWS:
        return _ps(f"Get-Content -Path {_ps_quote(LOG_FILE)} -Tail 100 -Wait")
    return ["tail", "-n", "100", "-f", LOG_FILE]


def linger_enabled() -> bool | None:
    if not shutil.which("loginctl"):
        return None
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    probe = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        return None
    return probe.stdout.strip().endswith("yes")


# --- the agentaus-bridge command ---------------------------------------------------------

def command_path() -> Path:
    return Path.home() / ".local" / "bin" / ("agentaus-bridge.cmd" if WINDOWS else "agentaus-bridge")


def command_text(root: Path | None = None, windows: bool = WINDOWS) -> str:
    root = root or ROOT
    python = venv_python(root)
    if windows:
        return f"""@echo off
rem The Agentaus bridge's command, written by its installer.
rem Remove it (with the service and settings) using: agentaus-bridge uninstall
setlocal
cd /d "{root}" || exit /b 1
if "%~1"=="" (
  "{python}" -m agentaus_bridge status
) else (
  "{python}" -m agentaus_bridge %*
)
"""
    return f"""#!/bin/sh
# The Agentaus bridge's command, written by its installer.
# Remove it (with the service and settings) using: agentaus-bridge uninstall
cd {shlex.quote(str(root))} || exit 1
[ $# -eq 0 ] && set -- status
exec {shlex.quote(str(python))} -m agentaus_bridge "$@"
"""


def on_path(directory: Path) -> bool:
    entries = os.environ.get("PATH", "").split(os.pathsep)
    want = os.path.normcase(str(directory.resolve()))
    return any(os.path.normcase(str(Path(e).expanduser().resolve())) == want for e in entries if e)


def add_to_user_path_command(directory: Path) -> list[str]:
    """Windows: append the directory to the user's PATH (new terminals see it)."""
    d = _ps_quote(str(directory))
    return _ps("$p = [Environment]::GetEnvironmentVariable('Path', 'User'); "
               f"if (-not (($p -split ';') -contains {d})) {{ "
               f"[Environment]::SetEnvironmentVariable('Path', (($p.TrimEnd(';') + ';' + {d}).TrimStart(';')), 'User') }}")
