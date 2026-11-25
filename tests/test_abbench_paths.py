"""Portability of the abbench harness: path resolution and shell syntax.

Nothing here launches a model, a bridge or a session. Every location the harness uses
comes from the environment or the script's own directory, never a hard-coded absolute
path belonging to whoever wrote it.
"""

import importlib
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ABBENCH = REPO / "abbench"
sys.path.insert(0, str(ABBENCH))

import ab  # noqa: E402
import summarise  # noqa: E402

AB_SOURCES = [
    "ab.py", "skill_use.py", "stream_probe.py", "summarise.py",
    "bridges.sh", "cdev.sh", "cnext.sh", "cref.sh", "ctest_agentaus.sh",
]
AB_ENV = (
    "AB_RUNS", "AB_PEER_DATA", "AB_CLAUDE_CODE", "AB_PY", "AB_PATH",
    "AB_STOCK", "AB_SNAP", "AB_LOGS", "AB_FIXTURE_SKILLS",
)
ZSH = shutil.which("zsh")


class EnvironmentCase(unittest.TestCase):
    """Isolate the process environment and module-level path snapshots."""

    def setUp(self):
        self.saved_env = os.environ.copy()
        for name in AB_ENV:
            os.environ.pop(name, None)
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        # RUNS/SMALL are intentionally import-time snapshots in the command-line tools.
        # Restore those snapshots before removing the temporary directory.
        importlib.reload(ab)
        importlib.reload(summarise)
        if "skill_use" in sys.modules:
            importlib.reload(sys.modules["skill_use"])
        self.temporary.cleanup()


class RunsAndPeerData(EnvironmentCase):
    def test_runs_root_default_is_outside_the_repo(self):
        root = ab.runs_root()
        self.assertEqual(root, Path.home() / "agentaus_ab_runs")
        self.assertNotIn(REPO, root.parents)
        self.assertNotEqual(root, REPO)

    def test_runs_root_follows_ab_runs(self):
        os.environ["AB_RUNS"] = str(self.tmp / "runs")
        self.assertEqual(ab.runs_root(), self.tmp / "runs")

    def test_peer_data_without_env_is_an_error_not_someone_elses_path(self):
        with self.assertRaisesRegex(SystemExit, "AB_PEER_DATA") as raised:
            ab.peer_data()
        self.assertNotIn("/Users/", str(raised.exception))

    def test_peer_data_follows_ab_peer_data(self):
        os.environ["AB_PEER_DATA"] = str(self.tmp / "bench-data")
        self.assertEqual(ab.peer_data(), self.tmp / "bench-data")


class ClaudeCodeAndEnvironment(EnvironmentCase):
    def test_claude_binary_follows_ab_claude_code(self):
        os.environ["AB_CLAUDE_CODE"] = "~/my-claude/claude"
        self.assertEqual(ab.claude_binary(), Path.home() / "my-claude" / "claude")

    def test_claude_binary_without_one_says_what_to_set(self):
        os.environ["HOME"] = str(self.tmp)
        with self.assertRaisesRegex(SystemExit, "AB_CLAUDE_CODE"):
            ab.claude_binary()

    def test_clean_env_path_is_overridable(self):
        os.environ["AB_PATH"] = "/usr/bin:/bin"
        self.assertEqual(ab.clean_env(8795)["PATH"], "/usr/bin:/bin")
        del os.environ["AB_PATH"]
        env = ab.clean_env(8795)
        self.assertIn("/usr/bin", env["PATH"])
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8795")


class SummarisePaths(EnvironmentCase):
    def test_runs_root_matches_ab(self):
        os.environ["AB_RUNS"] = str(self.tmp / "elsewhere")
        self.assertEqual(summarise.runs_root(), ab.runs_root())
        self.assertEqual(ab.runs_root(), self.tmp / "elsewhere")

    def test_load_reads_runs_from_ab_runs(self):
        os.environ["AB_RUNS"] = str(self.tmp)
        label = self.tmp / "mylabel" / "agentaus_tuned"
        label.mkdir(parents=True)
        (label / "mmlu_000.json").write_text(
            '{"suite": "mmlu", "arm": "agentaus_tuned", "task": "mmlu_000",'
            ' "wall_s": 1.0, "timed_out": false, "num_turns": 1,'
            ' "infra_error": false, "grade": {"score": 1.0, "right": 2, "n": 2}}'
        )
        out = summarise.summarise(summarise.load(["mylabel"]))
        self.assertEqual(out["mmlu"]["agentaus_tuned"]["score"], 1.0)


class SkillUsePaths(EnvironmentCase):
    def reload_skill_use(self):
        import skill_use
        importlib.reload(ab)
        return importlib.reload(skill_use)

    def test_runs_and_fixtures_derive_from_ab_runs(self):
        os.environ["AB_RUNS"] = str(self.tmp)
        skill_use = self.reload_skill_use()
        self.assertEqual(skill_use.RUNS, self.tmp / "skill_use")
        self.assertEqual(skill_use.SMALL, self.tmp / "_fixture_skills")

    def test_fixture_follows_ab_fixture_skills(self):
        os.environ["AB_RUNS"] = str(self.tmp)
        os.environ["AB_FIXTURE_SKILLS"] = str(self.tmp / "skills-here")
        skill_use = self.reload_skill_use()
        self.assertEqual(skill_use.SMALL, self.tmp / "skills-here")


class SourceAndShellChecks(unittest.TestCase):
    def test_no_source_names_another_developers_path(self):
        for name in AB_SOURCES:
            with self.subTest(name=name):
                self.assertNotIn("/Users/cheng", (ABBENCH / name).read_text())

    def test_driver_scripts_cd_to_their_own_directory(self):
        for name in ("cdev.sh", "cnext.sh", "cref.sh", "ctest_agentaus.sh", "bridges.sh"):
            with self.subTest(name=name):
                self.assertIn('cd -- "$(dirname -- "$0")"', (ABBENCH / name).read_text())

    def test_shell_scripts_parse_under_bash(self):
        # bash accepts the small zsh-compatible subset used here and is available on
        # Linux; the real zsh parse below additionally runs on macOS CI.
        for name in (n for n in AB_SOURCES if n.endswith(".sh")):
            with self.subTest(name=name):
                result = subprocess.run(
                    ["bash", "-n", str(ABBENCH / name)], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(ZSH, "zsh not installed")
    def test_shell_scripts_parse_under_zsh(self):
        for name in (n for n in AB_SOURCES if n.endswith(".sh")):
            with self.subTest(name=name):
                result = subprocess.run(
                    [ZSH, "-n", str(ABBENCH / name)], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_sources_compile(self):
        for name in (n for n in AB_SOURCES if n.endswith(".py")):
            with self.subTest(name=name):
                py_compile.compile(str(ABBENCH / name), doraise=True)

    @unittest.skipUnless(ZSH, "zsh not installed")
    def test_bridges_rejects_a_missing_checkout_before_launching(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            logs = tmp / "logs"
            missing = tmp / "no-such-stock"
            env = dict(
                os.environ,
                AB_PY=sys.executable,
                AB_LOGS=str(logs),
                AB_STOCK=str(missing),
            )
            result = subprocess.run(
                [ZSH, str(ABBENCH / "bridges.sh"), "baseline"],
                env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(str(missing), result.stderr)
            self.assertIn("AB_STOCK", result.stderr)
            self.assertFalse(list(logs.glob("bridge-*")), "no bridge may be launched")


if __name__ == "__main__":
    unittest.main()
