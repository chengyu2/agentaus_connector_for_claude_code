"""--doctor (Feature 3): offline checkable checks.

The live credential check (6) needs the network and is skipped here. Each offline
check is a pure function returning (ok, text); nothing prints at import time.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import doctor  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402


class TestDoctorOfflineChecks(unittest.TestCase):
    def test_python_version_is_39_or_newer(self):
        ok, text = doctor.check_python_version()
        self.assertTrue(ok)
        self.assertIn("Python", text)

    def test_env_key_reports_missing_env(self):
        # Point the check at a directory with no .env, without touching the real one.
        real = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                ok, text = doctor.check_env_key()
            finally:
                os.chdir(real)
        self.assertFalse(ok)
        self.assertIn(".env", text)

    def test_host_token_warns_when_exposed_without_token(self):
        old_host, old_token = settings.host, settings.bridge_token
        try:
            settings.host = "0.0.0.0"
            settings.bridge_token = ""
            ok, text = doctor.check_host_token()
            self.assertFalse(ok)
            self.assertIn("BRIDGE_TOKEN", text)
        finally:
            settings.host, settings.bridge_token = old_host, old_token

    def test_host_token_fine_on_loopback_without_token(self):
        old_host, old_token = settings.host, settings.bridge_token
        try:
            settings.host = "127.0.0.1"
            settings.bridge_token = ""
            ok, _ = doctor.check_host_token()
            self.assertTrue(ok)
        finally:
            settings.host, settings.bridge_token = old_host, old_token

    def test_claude_settings_reports_invalid_json_clearly(self):
        real = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            os.chdir(tmp)
            try:
                (tmp_path / ".claude").mkdir()
                (tmp_path / ".claude" / "settings.json").write_text(
                    "{ not json", encoding="utf-8"
                )
                ok, text = doctor.check_claude_settings()
                self.assertFalse(ok)
                self.assertIn("invalid JSON", text)
            finally:
                os.chdir(real)

    def test_claude_settings_accepts_correct_pointers(self):
        real = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            os.chdir(tmp)
            try:
                (tmp_path / ".claude").mkdir()
                (tmp_path / ".claude" / "settings.json").write_text(
                    json.dumps({
                        "env": {
                            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{settings.port}",
                            "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus",
                        }
                    }),
                    encoding="utf-8",
                )
                ok, text = doctor.check_claude_settings()
                self.assertTrue(ok)
                self.assertIn("agentaus", text)
            finally:
                os.chdir(real)

    def test_skills_folder_present_in_this_repo(self):
        ok, text = doctor.check_skills()
        self.assertTrue(ok)
        self.assertIn("Open Folder", text)


if __name__ == "__main__":
    unittest.main()
