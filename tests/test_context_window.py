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


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs bash")
class LauncherSetsTheWindowForItsOwnSession(unittest.TestCase):
    """Runs scripts/claude-agentaus.sh with stand-ins for curl and claude on PATH."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        for d in (self.bin, self.home, self.project):
            d.mkdir()
        (self.bin / "curl").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "claude").write_text(f'#!/bin/sh\nprintf "%s" "${{{KEY}-UNSET}}"\n')
        for f in self.bin.iterdir():
            f.chmod(0o755)

    def launch(self, **env: str) -> tuple[str, str]:
        base = {"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(self.home)}
        done = subprocess.run(["bash", str(LAUNCHER)], cwd=self.project, env={**base, **env},
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout, done.stderr

    def write_settings(self, path: Path, env: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"env": env}))

    def test_sets_the_bridges_window(self):
        out, _ = self.launch()
        self.assertEqual(out, str(_agentaus_window()))

    def test_left_out_when_disable_compact_is_exported(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                out, err = self.launch(DISABLE_COMPACT=value)
                self.assertEqual(out, "UNSET")
                self.assertIn("DISABLE_COMPACT", err)

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

    def test_powershell_launcher_uses_the_same_window(self):
        text = (REPO / "scripts" / "claude-agentaus.ps1").read_text(encoding="utf-8")
        self.assertIn(f'$env:{KEY} = "{_agentaus_window()}"', text)
        self.assertIn("Test-CompactDisabled", text)


class OldPythonWarning(unittest.TestCase):
    def test_older_than_the_minimum_warns_with_a_portable_fix(self):
        text = entry.old_python_warning((3, 10, 14))
        self.assertIn("Python 3.10", text)
        self.assertIn(".venv", text)
        self.assertIn("scripts/install.sh", text)
        self.assertIn("scripts\\install.ps1", text)
        for posix_only in ("rm -rf", "python3.12", ".venv/bin/pip"):
            self.assertNotIn(posix_only, text)

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
