"""The installer: component choice, the .env and settings.json edits, the package commands,
and a whole --yes run against a temporary install folder and home directory."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import components as C  # noqa: E402
from agentaus_bridge.installer import actions as A  # noqa: E402
from agentaus_bridge.installer import cli, flow  # noqa: E402
from agentaus_bridge.installer.ui import UI, Aborted  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Components(unittest.TestCase):
    def test_resolve_adds_core_and_requirements_in_order(self):
        self.assertEqual(C.resolve(["ocr"], "linux"), ["core", "poppler", "ocr"])

    def test_resolve_drops_what_the_platform_lacks(self):
        self.assertNotIn("vision", C.resolve(["vision", "pdf"], "linux"))
        self.assertIn("vision", C.resolve(["vision"], "darwin"))
        self.assertNotIn("vision", C.preset("full", "windows"))
        self.assertIn("service", C.preset("full", "windows"))

    def test_unknown_component_is_an_error(self):
        with self.assertRaises(ValueError):
            C.resolve(["libreoffice"])

    def test_presets(self):
        self.assertEqual(C.preset("minimal", "linux"), ["core", "settings"])
        recommended = C.preset("recommended", "linux")
        self.assertNotIn("office", recommended)
        self.assertNotIn("ocr", recommended)
        self.assertEqual(C.preset("full", "darwin"), [c.key for c in C.COMPONENTS])

    def test_parse_ignores_unknown_and_blank(self):
        self.assertIsNone(C.parse(None))
        self.assertIsNone(C.parse("  "))
        self.assertEqual(C.parse("core, pdf,teleport"), ["core", "pdf"])

    def test_every_pip_group_has_a_requirements_file(self):
        for c in C.COMPONENTS:
            if c.pip:
                self.assertTrue((REPO / "requirements" / f"{c.pip}.txt").is_file(), c.key)

    def test_requirements_txt_still_installs_every_runtime_group(self):
        text = (REPO / "requirements.txt").read_text()
        for c in C.COMPONENTS:
            if c.pip:
                self.assertIn(f"requirements/{c.pip}.txt", text)


class EnvFile(unittest.TestCase):
    def test_replace_uncomment_and_append(self):
        text = "A=1\n# B=2\n# a comment = not a key\n"
        out = A.update_env_text(text, {"A": "9", "B": "3", "C": "4"})
        self.assertEqual(out, "A=9\nB=3\n# a comment = not a key\n\nC=4\n")

    def test_write_env_starts_from_the_example_and_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".env.example").write_text("AGENTAUS_API_KEY=\nBRIDGE_PORT=8787\n")
            path = A.write_env(A.Runner(), root, {"AGENTAUS_API_KEY": "sk-123"})
            self.assertEqual(A.read_env(path), {"AGENTAUS_API_KEY": "sk-123", "BRIDGE_PORT": "8787"})
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            runner = A.Runner(dry_run=True)
            A.write_env(runner, Path(d), {"X": "1"})
            self.assertFalse((Path(d) / ".env").exists())
            self.assertTrue(any(".env" in line for line in runner.log))


class Settings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "settings.json"
        self.wanted = A.bridge_env(8787)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_merge_keeps_everything_else_and_backs_up(self):
        self.path.write_text(json.dumps({"model": "opus", "env": {"FOO": "bar"}}))
        backup = A.merge_settings(A.Runner(), self.path, self.wanted)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["env"]["FOO"], "bar")
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8787")
        self.assertEqual(json.loads(backup.read_text())["env"], {"FOO": "bar"})

    def test_new_file(self):
        self.assertIsNone(A.merge_settings(A.Runner(), self.path, self.wanted))
        self.assertEqual(json.loads(self.path.read_text()), {"env": self.wanted})

    def test_changes_lists_only_what_differs(self):
        data = {"env": dict(self.wanted, ANTHROPIC_BASE_URL="http://127.0.0.1:9999")}
        self.assertEqual(A.settings_changes(data, self.wanted),
                         [("ANTHROPIC_BASE_URL", "http://127.0.0.1:9999", "http://127.0.0.1:8787")])

    def test_invalid_json_is_refused_not_overwritten(self):
        self.path.write_text("{ // a comment\n}")
        with self.assertRaises(A.SettingsError):
            A.load_settings(self.path)

    def test_unmerge_removes_only_unchanged_keys(self):
        env = dict(self.wanted, FOO="bar", ANTHROPIC_CUSTOM_MODEL_OPTION="edited")
        self.path.write_text(json.dumps({"env": env}))
        removed = A.unmerge_settings(A.Runner(), self.path, self.wanted)
        self.assertNotIn("ANTHROPIC_CUSTOM_MODEL_OPTION", removed)
        self.assertEqual(json.loads(self.path.read_text())["env"],
                         {"FOO": "bar", "ANTHROPIC_CUSTOM_MODEL_OPTION": "edited"})


class SystemCommands(unittest.TestCase):
    def test_apt_uses_sudo_unless_root(self):
        needed = [C.BY_KEY["poppler"], C.BY_KEY["ocr"]]
        cmds = flow.system_commands(needed, "apt", as_root=False)
        self.assertEqual(cmds[-1], ["sudo", "apt-get", "install", "-y", "poppler-utils", "tesseract-ocr"])
        self.assertEqual(flow.system_commands(needed, "apt", as_root=True)[0], ["apt-get", "update"])

    def test_brew_splits_formulas_and_casks(self):
        needed = [C.BY_KEY["poppler"], C.BY_KEY["office"]]
        self.assertEqual(flow.system_commands(needed, "brew", as_root=False),
                         [["brew", "install", "poppler"], ["brew", "install", "--cask", "libreoffice"]])

    def test_winget_one_per_package(self):
        cmds = flow.system_commands([C.BY_KEY["office"]], "winget", as_root=False)
        self.assertEqual(cmds[0][:5], ["winget", "install", "-e", "--id", "TheDocumentFoundation.LibreOffice"])


class Platforms(unittest.TestCase):
    def test_pacman_names_the_ocr_language_and_never_syncs_the_system(self):
        cmds = flow.system_commands([C.BY_KEY["poppler"], C.BY_KEY["ocr"], C.BY_KEY["office"]],
                                    "pacman", as_root=False)
        self.assertEqual(cmds, [["sudo", "pacman", "-S", "--needed", "--noconfirm", "poppler",
                                 "tesseract", "tesseract-data-eng", "libreoffice-fresh"]])

    def test_dnf(self):
        self.assertEqual(flow.system_commands([C.BY_KEY["ocr"]], "dnf", as_root=True),
                         [["dnf", "install", "-y", "tesseract"]])

    def test_every_system_component_has_a_package_for_every_manager(self):
        for c in C.COMPONENTS:
            if c.kind == "system":
                for manager in ("apt", "dnf", "pacman", "winget"):
                    self.assertTrue(c.packages.get(manager), f"{c.key} on {manager}")
                self.assertTrue(c.packages.get("brew") or c.packages.get("brew-cask"), c.key)

    def test_package_manager_detection(self):
        for exe, manager in (("apt-get", "apt"), ("dnf", "dnf"), ("pacman", "pacman")):
            with mock.patch("shutil.which", side_effect=lambda n, exe=exe: "/usr/bin/" + n if n == exe else None):
                self.assertEqual(C.package_manager("linux"), manager)
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(C.package_manager("linux"))

    def test_install_hints(self):
        with mock.patch.object(C, "package_manager", return_value="pacman"):
            self.assertEqual(C.install_hint(C.BY_KEY["ocr"], "linux"),
                             "sudo pacman -S --needed tesseract tesseract-data-eng")
        self.assertIn(r".venv\Scripts\pip install -r requirements/pdf.txt",
                      C.install_hint(C.BY_KEY["pdf"], "windows"))

    def test_windows_command_shim(self):
        text = A.command_text(Path(r"C:\Users\me\.agentaus-bridge"), windows=True)
        self.assertTrue(text.startswith("@echo off"))
        self.assertIn('cd /d "C:\\Users\\me\\.agentaus-bridge"', text)
        self.assertIn("-m agentaus_bridge status", text)
        self.assertIn("-m agentaus_bridge %*", text)

    def test_windows_task_scripts_have_no_double_quotes(self):
        for action in ("start", "restart", "stop", "remove", "status"):
            for cmd in A.service_commands("schtasks", action, Path(r"C:\b's dir")):
                self.assertEqual(cmd[0], "powershell")
                self.assertNotIn('"', cmd[-1], action)
        start = A.service_commands("schtasks", "start", Path(r"C:\b's dir"))[0][-1]
        self.assertIn("pythonw.exe", start)
        self.assertIn("-m agentaus_bridge.background", start)
        self.assertIn("-WorkingDirectory 'C:\\b''s dir'", start)
        self.assertIn("Start-ScheduledTask", start)
        self.assertIn("Unregister-ScheduledTask", A.service_commands("schtasks", "remove")[0][-1])
        self.assertIsNone(A.service_file("schtasks"))

    def test_add_to_user_path_has_no_double_quotes(self):
        self.assertNotIn('"', A.add_to_user_path_command(Path(r"C:\Users\me\.local\bin"))[-1])

    def test_background_sends_output_to_the_log(self):
        from agentaus_bridge import background
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "bridge.log")
            saved = sys.stdout, sys.stderr
            try:
                with mock.patch.object(background, "LOG_FILE", log), \
                        mock.patch("agentaus_bridge.__main__.main", side_effect=lambda argv: print("hello") or 5), \
                        mock.patch.object(sys, "argv", ["x", "--port", "1"]):
                    self.assertEqual(background.main(), 5)
                sys.stdout.close()
            finally:
                sys.stdout, sys.stderr = saved
            self.assertIn("hello", Path(log).read_text())


class PlainPrompts(unittest.TestCase):
    def ui(self, answers):
        ui = UI(interactive=True, fancy=False, out=io.StringIO())
        feed = iter(answers)
        patcher = mock.patch("builtins.input", side_effect=lambda prompt="": next(feed))
        patcher.start()
        self.addCleanup(patcher.stop)
        return ui

    def test_checkbox_toggles_but_never_drops_locked(self):
        ui = self.ui(["1 2 3", ""])
        picked = ui.checkbox("Pick", [("Core", "core", True, True), ("A", "a", True, False),
                                      ("B", "b", False, False)])
        self.assertEqual(picked, ["core", "b"])
        self.assertIn("[x] Core", ui.out.getvalue())

    def test_select_and_confirm_defaults(self):
        ui = self.ui(["", "2", "n"])
        self.assertEqual(ui.select("Q", [("one", "1"), ("two", "2")], default="1"), "1")
        self.assertEqual(ui.select("Q", [("one", "1"), ("two", "2")], default="1"), "2")
        self.assertFalse(ui.confirm("Sure?", True))

    def test_eof_aborts(self):
        ui = UI(interactive=True, fancy=False, out=io.StringIO())
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(Aborted):
                ui.confirm("Sure?")

    def test_assume_yes_never_reads_input(self):
        ui = UI(assume_yes=True, interactive=False, out=io.StringIO())
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertTrue(ui.confirm("x", True))
            self.assertFalse(ui.confirm("x", False))
            self.assertEqual(ui.checkbox("x", [("a", "a", True, False), ("b", "b", False, False)]), ["a"])


class RecordingRunner(A.Runner):
    def run(self, argv, check=False, **kwargs):
        self.log.append("run: " + " ".join(argv))
        return 0


class _Fixture(unittest.TestCase):
    """setup --yes into a temporary install folder, with HOME pointed at a temporary home."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "requirements", self.root / "requirements")
        shutil.copy(REPO / ".env.example", self.root / ".env.example")
        self.port = _free_port()
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.home), "AGENTAUS_API_KEY": "sk-test-key-1234"}),
            mock.patch.object(A, "service_kind", return_value=None),
            mock.patch.object(C, "_binary", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def setup(self, *argv):
        args = flow.build_parser().parse_args(["--yes", "--no-check", "--port", str(self.port), *argv])
        ui = UI(assume_yes=True, interactive=False, out=io.StringIO())
        runner = RecordingRunner()
        code = flow.Setup(args, ui, runner, root=self.root).run()
        return code, runner, ui.out.getvalue()



class WholeRun(_Fixture):
    def test_minimal_with_command(self):
        code, runner, out = self.setup("--preset", "minimal", "--with", "command", "--settings", "user")
        self.assertEqual(code, 0)
        env = A.read_env(self.root / ".env")
        self.assertEqual(env[C.ENV_KEY], "core,settings,command")
        self.assertEqual(env["AGENTAUS_API_KEY"], "sk-test-key-1234")
        self.assertEqual(env["BRIDGE_PORT"], str(self.port))
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], f"http://127.0.0.1:{self.port}")
        shim = self.home / ".local" / "bin" / "agentaus-bridge"
        self.assertIn(str(self.root), shim.read_text())
        self.assertTrue(os.access(shim, os.X_OK))
        state = A.load_state(self.root)
        self.assertEqual(state["command"], str(shim))
        pip = [l for l in runner.log if " pip install " in l]
        self.assertEqual(len(pip), 1)
        self.assertIn("requirements/core.txt", pip[0])
        self.assertNotIn("requirements/outline.txt", pip[0])
        self.assertNotIn("apt-get", " ".join(runner.log))

    def test_ocr_pulls_in_poppler_and_installs_system_packages(self):
        with mock.patch.object(C, "package_manager", return_value="apt"), \
                mock.patch.object(flow, "_is_root", return_value=True):
            code, runner, out = self.setup("--preset", "minimal", "--with", "ocr,outline", "--settings", "none")
        self.assertEqual(code, 0)
        self.assertEqual(A.read_env(self.root / ".env")[C.ENV_KEY], "core,poppler,ocr,outline,settings")
        self.assertIn("run: apt-get install -y poppler-utils tesseract-ocr", runner.log)
        self.assertIn("Added PDF text with layout (poppler) (needed by OCR", out)
        self.assertFalse((self.home / ".claude").exists())

    def test_rerun_keeps_the_previous_choice_and_the_key(self):
        self.setup("--preset", "minimal", "--settings", "none")
        os.environ.pop("AGENTAUS_API_KEY")
        code, _, _ = self.setup("--settings", "none")
        self.assertEqual(code, 0)
        env = A.read_env(self.root / ".env")
        self.assertEqual(env[C.ENV_KEY], "core,settings")
        self.assertEqual(env["AGENTAUS_API_KEY"], "sk-test-key-1234")

    def test_unknown_component_exits(self):
        with self.assertRaises(SystemExit):
            self.setup("--with", "teleport")

    def test_uninstall_reverses_settings_and_command(self):
        self.setup("--preset", "minimal", "--with", "command", "--settings", "user")
        with mock.patch.object(A, "ROOT", self.root):
            self.assertEqual(cli.main(["uninstall", "--yes"]), 0)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertNotIn("env", settings)
        self.assertFalse((self.home / ".local" / "bin" / "agentaus-bridge").exists())
        self.assertNotIn("command", A.load_state(self.root))


class ReviewFixes(_Fixture):
    """Bugs found in review, each pinned by the run that showed it."""

    def test_with_adds_up_across_repeats(self):
        code, _, _ = self.setup("--preset", "minimal", "--with", "pdf", "--with", "outline", "--settings", "none")
        self.assertEqual(A.read_env(self.root / ".env")[C.ENV_KEY], "core,pdf,outline,settings")

    def test_components_keeps_the_port_and_the_setup_choices(self):
        self.setup("--preset", "minimal", "--with", "command", "--settings", "none")
        args = flow.build_parser().parse_args(["--yes", "--components-only", "--with", "outline,service"])
        ui = UI(assume_yes=True, interactive=False, out=io.StringIO())
        flow.Setup(args, ui, RecordingRunner(), root=self.root).run()
        env = A.read_env(self.root / ".env")
        self.assertEqual(env["BRIDGE_PORT"], str(self.port))
        self.assertEqual(env[C.ENV_KEY], "core,outline,settings,command")
        self.assertIn("run `agentaus-bridge setup`", ui.out.getvalue())

    def test_uninstall_restores_overwritten_values_and_leaves_matching_ones(self):
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        mine = A.bridge_env(self.port)
        path.write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://corp-gateway.example",          # will be overwritten
            "ANTHROPIC_CUSTOM_MODEL_OPTION": mine["ANTHROPIC_CUSTOM_MODEL_OPTION"],  # already ours
        }}))
        self.setup("--preset", "minimal", "--settings", "user")
        self.assertEqual(json.loads(path.read_text())["env"]["ANTHROPIC_BASE_URL"], mine["ANTHROPIC_BASE_URL"])
        with mock.patch.object(A, "ROOT", self.root):
            cli.main(["uninstall", "--yes"])
        env = json.loads(path.read_text())["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://corp-gateway.example")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION"], mine["ANTHROPIC_CUSTOM_MODEL_OPTION"])
        self.assertNotIn("ANTHROPIC_CUSTOM_MODEL_OPTION_NAME", env)

    def test_settings_already_pointing_at_the_bridge_are_not_claimed(self):
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"env": A.bridge_env(self.port)}))
        self.setup("--preset", "minimal", "--settings", "user")
        self.assertFalse(A.load_state(self.root).get("settings"))

    def test_relative_project_path_is_relative_to_the_caller(self):
        project = self.home / "proj"
        project.mkdir()
        with mock.patch.dict(os.environ, {"AGENTAUS_INSTALL_CWD": str(self.home)}):
            self.setup("--preset", "minimal", "--settings", "proj")
        self.assertTrue((project / ".claude" / "settings.local.json").exists())
        self.assertFalse((self.root / ".claude").exists())

    def test_out_of_range_port_is_an_error_not_a_traceback(self):
        args = flow.build_parser().parse_args(["--yes", "--port", "99999"])
        with self.assertRaises(SystemExit):
            flow.Setup(args, UI(assume_yes=True, interactive=False, out=io.StringIO()),
                       RecordingRunner(), root=self.root).run()

    def test_dry_run_writes_nothing_anywhere(self):
        args = flow.build_parser().parse_args(["--yes", "--dry-run", "--no-check", "--port", str(self.port),
                                               "--preset", "full", "--settings", "user"])
        before = sorted(p for p in list(self.root.rglob("*")) + list(self.home.rglob("*")))
        flow.Setup(args, UI(assume_yes=True, interactive=False, out=io.StringIO()),
                   A.Runner(dry_run=True), root=self.root).run()
        after = sorted(p for p in list(self.root.rglob("*")) + list(self.home.rglob("*")))
        self.assertEqual(before, after)

    def test_yes_without_a_key_finishes_says_so_and_writes_no_key(self):
        os.environ.pop("AGENTAUS_API_KEY")
        code, _, out = self.setup("--preset", "minimal", "--settings", "none")
        self.assertEqual(code, 0)
        self.assertIn("No API key", out)
        self.assertIn("Add your API key", out)
        self.assertEqual(A.read_env(self.root / ".env").get("AGENTAUS_API_KEY", ""), "")

    def test_state_is_saved_before_a_later_step_aborts(self):
        with mock.patch.object(flow.Setup, "command", side_effect=Aborted()):
            with self.assertRaises(Aborted):
                self.setup("--preset", "minimal", "--with", "command", "--settings", "user")
        self.assertTrue(A.load_state(self.root).get("settings"))


@unittest.skipUnless(os.name == "posix", "POSIX file modes and symlinks")
class Writes(unittest.TestCase):
    def test_symlink_is_written_through_and_mode_kept(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "dotfiles" / "settings.json"
            real.parent.mkdir()
            real.write_text("{}")
            os.chmod(real, 0o600)
            link = Path(d) / "settings.json"
            link.symlink_to(real)
            A.merge_settings(A.Runner(), link, {"K": "v"})
            self.assertTrue(link.is_symlink())
            self.assertEqual(json.loads(real.read_text())["env"], {"K": "v"})
            self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)

    def test_systemd_unit_quotes_paths(self):
        unit = A.service_text("systemd", Path("/home/a b/100%"))
        self.assertIn("WorkingDirectory=/home/a b/100%%", unit)
        self.assertRegex(unit, r'ExecStart="[^"]+" -m agentaus_bridge')

    def test_shims_keep_the_callers_directory(self):
        self.assertIn('AGENTAUS_INSTALL_CWD="$PWD"', A.command_text(Path("/x"), windows=False))
        self.assertIn('set "AGENTAUS_INSTALL_CWD=%CD%"', A.command_text(Path("C:/x"), windows=True))


class Cli(unittest.TestCase):
    def test_unknown_command(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(cli.main(["frobnicate"]), 2)
        self.assertIn("usage: agentaus-bridge", err.getvalue())

    def test_main_routes_words_to_the_cli_and_flags_to_the_server(self):
        from agentaus_bridge import __main__ as entry
        with mock.patch.object(cli, "main", return_value=7) as routed:
            self.assertEqual(entry.main(["status"]), 7)
        routed.assert_called_once_with(["status"])

    def test_setup_without_a_terminal_needs_yes(self):
        with mock.patch("sys.stdin", new=io.StringIO()), mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(flow.main(["--dry-run"]), 2)
        self.assertIn("--yes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
