"""A throwaway container per live run: no network, only the task's own files.

The agent's commands run here and nowhere else. Hidden tests are copied in only after
the agent has finished, so it can never read them.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

IMAGE = "python:3.12-slim"
OUTPUT_CHARS = 8_000


class Sandbox:
    def __init__(self, files: dict[str, str | bytes]):
        self.tmp = Path(tempfile.mkdtemp(prefix="calib-"))
        work = self.tmp / "work"
        work.mkdir()
        for rel, body in files.items():
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(body, bytes):
                path.write_bytes(body)
            else:
                path.write_text(body)
        self.name = f"calib-{uuid.uuid4().hex[:10]}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, "--network", "none",
                        "--memory", "1g", "--cpus", "1", "-w", "/work",
                        "-v", f"{work}:/work", IMAGE, "sleep", "infinity"],
                       check=True, capture_output=True)

    def bash(self, command: str, timeout: int = 60) -> str:
        try:
            done = subprocess.run(["docker", "exec", self.name, "bash", "-lc", command],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"[command timed out after {timeout}s]"
        out = (done.stdout + done.stderr) or "(no output)"
        if len(out) > OUTPUT_CHARS:
            out = out[:OUTPUT_CHARS] + f"\n[... {len(out) - OUTPUT_CHARS} more characters]"
        return out + ("" if done.returncode == 0 else f"\n[exit code {done.returncode}]")

    def read_file(self, path: str) -> str:
        return self.bash(f"cat -- {_quote(path)}")

    def write_file(self, path: str, content: str) -> str:
        target = self.tmp / "work" / Path(path.removeprefix("/work/")).as_posix().lstrip("/")
        if not str(target.resolve()).startswith(str((self.tmp / "work").resolve())):
            return "[refused: path is outside /work]"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content)} characters to {path}"

    def add_hidden(self, files: dict[str, str]) -> None:
        for rel, body in files.items():
            path = self.tmp / "work" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)

    def close(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        # Files the container created belong to root; remove them from inside first.
        subprocess.run(["docker", "run", "--rm", "-v", f"{self.tmp}:/t", IMAGE,
                        "rm", "-rf", "/t/work"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"
