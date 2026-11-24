"""Run the bridge with its output appended to a log file.

For a service with no console: Windows' Task Scheduler starts the bridge with pythonw.exe,
which has no stdout or stderr at all, so without this every log line (and uvicorn's own
output) would vanish. launchd and systemd capture output themselves and do not use it.

    pythonw -m agentaus_bridge.background [bridge options]
"""

from __future__ import annotations

import sys

from .installer.actions import LOG_FILE


def main() -> int:
    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    from .__main__ import main as bridge
    return bridge(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
