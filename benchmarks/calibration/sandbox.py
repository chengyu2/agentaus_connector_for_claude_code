"""A throwaway container per live run: no network, only the task's own files.

The agent's commands run here and nowhere else. Hidden tests are copied in only after
the agent has finished, so it can never read them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

IMAGE = "python:3.12-slim"
OUTPUT_CHARS = 8_000
_POSIX = hasattr(os, "getuid")


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
        # Run as the host user where there is one, so files the agent creates in the container
        # stay writable by write_file and removable by close().
        user = ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"] if _POSIX else []
        subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, "--network", "none",
                        "--memory", "1g", "--cpus", "1", "-w", "/work", *user,
                        "-v", f"{work}:/work", IMAGE, "sleep", "infinity"],
                       check=True, capture_output=True)

    def bash(self, command: str, timeout: int = 60) -> str:
        try:
            done = subprocess.run(["docker", "exec", self.name, "bash", "-lc", command],
                                  capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"[command timed out after {timeout}s]"
        # Not text=True: `cat signups.png` would raise UnicodeDecodeError instead of answering.
        out = (done.stdout + done.stderr).decode("utf-8", errors="replace") or "(no output)"
        if len(out) > OUTPUT_CHARS:
            out = out[:OUTPUT_CHARS] + f"\n[... {len(out) - OUTPUT_CHARS} more characters]"
        return out + ("" if done.returncode == 0 else f"\n[exit code {done.returncode}]")

    def read_file(self, path: str) -> str:
        return self.bash(f"cat -- {_quote(path)}")

    def write_file(self, path: str, content: str) -> str:
        target = self.tmp / "work" / Path(path.removeprefix("/work/")).as_posix().lstrip("/")
        if not target.resolve().is_relative_to((self.tmp / "work").resolve()):
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
        if not _POSIX:
            # Without a host uid the container ran as root; remove its files from inside.
            subprocess.run(["docker", "run", "--rm", "-v", f"{self.tmp}:/t", IMAGE,
                            "rm", "-rf", "/t/work"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"
