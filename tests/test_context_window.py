"""Agentaus's context window reaches Claude Code only in sessions meant for Agentaus.

With DISABLE_COMPACT set, Claude Code applies CLAUDE_CODE_MAX_CONTEXT_TOKENS to every
model, Claude ones included. So the launcher sets it for its own session only, and only
when compaction is not disabled; tests/test_installer.py checks that the installer never
writes it into a settings file.
Also covers the old-Python warning printed at startup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import __main__ as entry  # noqa: E402
from agentaus_bridge.config import Settings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "claude-agentaus.sh"
KEY = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def _agentaus_window() -> int:
    with mock.patch.dict(os.environ):
        os.environ.pop("AGENTAUS_MAX_INPUT_TOKENS", None)
        return Settings().agentaus_max_input_tokens


PS_LAUNCHER = REPO / "scripts" / "claude-agentaus.ps1"
PWSH = shutil.which("pwsh")


class _LauncherCases:
    """Runs a launcher with stand-ins for the bridge check and claude, from a project folder.

    The claude stand-in prints the window it was given as CTX=<value> (UNSET if none).
    Subclasses say how to run their launcher.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        for d in (self.bin, self.home, self.project):
            d.mkdir()
        (self.bin / "curl").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "claude").write_text(f'#!/bin/sh\nprintf "CTX=%s\\n" "${{{KEY}-UNSET}}"\n')
        for f in self.bin.iterdir():
            f.chmod(0o755)

    def command(self) -> list[str]:
        raise NotImplementedError

    def launch(self, cwd: Path | None = None, **env: str) -> tuple[str, str]:
        """(the window claude saw, everything the launcher printed)."""
        base = {"PATH": f"{self.bin}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin",
                "HOME": str(self.home), "POWERSHELL_TELEMETRY_OPTOUT": "1",
                "POWERSHELL_UPDATECHECK": "Off", "GIT_CONFIG_NOSYSTEM": "1"}
        done = subprocess.run(self.command(), cwd=cwd or self.project, env={**base, **env},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        seen = [line[4:] for line in done.stdout.splitlines() if line.startswith("CTX=")]
        self.assertEqual(len(seen), 1, done.stdout + done.stderr)
        return seen[0], done.stdout + done.stderr

    def write_settings(self, path: Path, env: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"env": env}))

    def git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                       cwd=cwd, env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.home),
                                     "GIT_CONFIG_NOSYSTEM": "1"},
                       check=True, capture_output=True)

    def test_sets_the_bridges_window(self):
        out, _ = self.launch()
        self.assertEqual(out, str(_agentaus_window()))

    def test_left_out_when_disable_compact_is_exported(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                out, printed = self.launch(DISABLE_COMPACT=value)
                self.assertEqual(out, "UNSET")
                self.assertIn("DISABLE_COMPACT", printed)

    def test_falsy_disable_compact_still_sets_it(self):
        out, _ = self.launch(DISABLE_COMPACT="0")
        self.assertEqual(out, str(_agentaus_window()))

    def test_left_out_when_a_settings_file_names_disable_compact(self):
        for path in (self.home / ".claude" / "settings.json",
                     self.project / ".claude" / "settings.json",
                     self.project / ".claude" / "settings.local.json"):
            with self.subTest(path=path):
                self.write_settings(path, {"DISABLE_COMPACT": "1"})
                out, _ = self.launch()
                self.assertEqual(out, "UNSET")
                path.unlink()

    def test_claude_config_dir_is_the_user_settings_folder(self):
        config = self.tmp / "config"
        self.write_settings(config / "settings.json", {"DISABLE_COMPACT": "1"})
        out, _ = self.launch(CLAUDE_CONFIG_DIR=str(config))
        self.assertEqual(out, "UNSET")

    def test_a_value_the_user_set_wins(self):
        out, _ = self.launch(**{KEY: "100000"})
        self.assertEqual(out, "100000")

    @unittest.skipIf(shutil.which("git") is None, "needs git")
    def test_finds_the_repository_roots_local_settings_from_a_subfolder(self):
        # On macOS and Linux Claude Code reads settings.local.json from the git root, not the
        # folder it starts in, so a session started in a subfolder still has DISABLE_COMPACT.
        self.git(self.project, "init", "-q")
        sub = self.project / "src" / "deep"
        sub.mkdir(parents=True)
        out, _ = self.launch(cwd=sub)
        self.assertEqual(out, str(_agentaus_window()))
        self.write_settings(self.project / ".claude" / "settings.local.json", {"DISABLE_COMPACT": "1"})
        out, printed = self.launch(cwd=sub)
        self.assertEqual(out, "UNSET")
        self.assertIn("DISABLE_COMPACT", printed)

    @unittest.skipIf(shutil.which("git") is None, "needs git")
    def test_finds_the_main_checkouts_local_settings_from_a_linked_worktree(self):
        self.git(self.project, "init", "-q")
        self.git(self.project, "commit", "-q", "--allow-empty", "-m", "start")
        worktree = self.tmp / "worktree"
        self.git(self.project, "worktree", "add", "-q", "-b", "side", str(worktree))
        (worktree / "sub").mkdir()
        self.write_settings(self.project / ".claude" / "settings.local.json", {"DISABLE_COMPACT": "1"})
        for cwd in (worktree, worktree / "sub"):
            with self.subTest(cwd=cwd):
                out, _ = self.launch(cwd=cwd)
                self.assertEqual(out, "UNSET")

    def test_outside_a_repository_only_the_starting_folder_counts(self):
        # A parent folder's settings.local.json is not one Claude Code reads outside git.
        self.write_settings(self.tmp / ".claude" / "settings.local.json", {"DISABLE_COMPACT": "1"})
        out, _ = self.launch(GIT_CEILING_DIRECTORIES=str(self.tmp))
        self.assertEqual(out, str(_agentaus_window()))


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs bash")
class LauncherSetsTheWindowForItsOwnSession(_LauncherCases, unittest.TestCase):
    """scripts/claude-agentaus.sh"""

    def command(self) -> list[str]:
        return ["bash", str(LAUNCHER)]

    def test_powershell_launcher_uses_the_same_window(self):
        text = (REPO / "scripts" / "claude-agentaus.ps1").read_text(encoding="utf-8")
        self.assertIn(f'$env:{KEY} = "{_agentaus_window()}"', text)
        self.assertIn("Test-CompactDisabled", text)


@unittest.skipIf(PWSH is None or os.name == "nt", "needs pwsh with sh-script stand-ins")
class PowerShellLauncherSetsTheWindowForItsOwnSession(_LauncherCases, unittest.TestCase):
    """scripts/claude-agentaus.ps1, with Invoke-RestMethod stubbed for the bridge check."""

    def command(self) -> list[str]:
        script = str(PS_LAUNCHER).replace("'", "''")
        return [PWSH, "-NoProfile", "-NonInteractive", "-Command",
                f"function global:Invoke-RestMethod {{ }}; & '{script}'; exit $LASTEXITCODE"]

    def test_the_shell_gets_its_variables_back(self):
        script = str(PS_LAUNCHER).replace("'", "''")
        done = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command",
             f"function global:Invoke-RestMethod {{ }}; & '{script}' | Out-Null; "
             f"'AFTER=' + [string]$env:{KEY} + '|' + [string]$env:ANTHROPIC_BASE_URL"],
            cwd=self.project, capture_output=True, text=True, timeout=60,
            env={"PATH": f"{self.bin}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin",
                 "HOME": str(self.home), "POWERSHELL_TELEMETRY_OPTOUT": "1"})
        self.assertIn("AFTER=|", done.stdout, done.stdout + done.stderr)


class OldPythonWarning(unittest.TestCase):
    def test_older_than_the_minimum_warns_with_a_portable_fix(self):
        text = entry.old_python_warning((3, 10, 14))
        self.assertIn("Python 3.10", text)
        self.assertIn(".venv", text)
        self.assertIn("scripts/install.sh", text)
        self.assertIn("scripts\\install.ps1", text)
        for posix_only in ("rm -rf", "python3.12", ".venv/bin/pip"):
            self.assertNotIn(posix_only, text)
        # sys.stdlib_module_names, which spots a local random.py or json.py, exists since
        # 3.10, so that is no example of what 3.10 lacks.
        for wrong_on_310 in ("random.py", "json.py"):
            self.assertNotIn(wrong_on_310, text)

    def test_minimum_and_newer_are_quiet(self):
        for version in ((3, 11, 0), (3, 12, 3), (3, 13, 0)):
            self.assertIsNone(entry.old_python_warning(version))

    def test_main_logs_it_on_an_old_python_only(self):
        from agentaus_bridge.config import settings

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        empty_env = tmp / ".env"
        empty_env.write_text("")
        for version, expected in (((3, 10, 0), True), ((3, 11, 0), False)):
            with self.subTest(version=version), \
                    mock.patch.object(settings, "agentaus_api_key", ""), \
                    mock.patch.object(entry, "sys", mock.Mock(version_info=version)), \
                    self.assertLogs("agentaus-bridge", "INFO") as logs:
                # Without an API key main() returns 2 right after the version check.
                self.assertEqual(entry.main(["--env-file", str(empty_env)]), 2)
            warned = any("needs 3.11 or newer" in line for line in logs.output)
            self.assertEqual(warned, expected)


if __name__ == "__main__":
    unittest.main()
