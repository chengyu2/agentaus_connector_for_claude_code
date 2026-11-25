"""Entrypoint: `python -m agentaus_bridge`.

The .env file is loaded before any bridge module is imported, because
agentaus_bridge.config snapshots the environment at import time.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# The installer refuses anything older (scripts/install.sh); a hand-made virtualenv does not.
MIN_PYTHON = (3, 11)


def old_python_warning(version: tuple) -> str | None:
    """What to tell someone whose bridge runs on a Python older than MIN_PYTHON, or None.

    The fix names no interpreter or shell command: the same main() runs the Windows
    service, and the installer picks a suitable Python and the user's own components.
    The old virtualenv must go first, because install.ps1 reuses any it finds.
    """
    if tuple(version[:2]) >= MIN_PYTHON:
        return None
    return (
        "running on Python %d.%d; the bridge needs %d.%d or newer. Some protections are off "
        "on older versions - for example, a script's local random.py or json.py is not "
        "recognised as shadowing the standard library. To rebuild on a newer Python, install "
        "Python %d.%d or newer, delete the .venv folder in the bridge's directory, and run the "
        "installer again (scripts/install.sh, or scripts\\install.ps1 on Windows)."
        % (*version[:2], *MIN_PYTHON, *MIN_PYTHON)
    )


def load_env_file(path: Path) -> int:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes.

    Existing environment variables always win, so a value exported in the shell
    overrides the file.
    """
    if not path.is_file():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in and not args_in[0].startswith("-"):
        # `setup`, `status`, `components`, ...: the installer and the agentaus-bridge
        # command. Imported lazily so the server never loads the installer's packages.
        from .installer import cli
        return cli.main(args_in)

    parser = argparse.ArgumentParser(
        prog="agentaus_bridge",
        description="Anthropic Messages API bridge for the Agentaus API.",
    )
    parser.add_argument("--host", help="Interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Port to bind (default 8787)")
    parser.add_argument("--env-file", default=".env", help="Path to the .env file")
    parser.add_argument("--log-level", help="critical|error|warning|info|debug")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the Agentaus credential and exit without starting the server",
    )
    args = parser.parse_args(argv)

    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    count = load_env_file(env_path)

    # Imported only after the .env file is in os.environ.
    from .config import settings

    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.log_level:
        settings.log_level = args.log_level

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("agentaus-bridge")
    if count:
        log.info("loaded %d settings from %s", count, env_path)
    warning = old_python_warning(sys.version_info)
    if warning:
        # Warn rather than refuse: a background service that exits on start is restarted
        # in a loop, which is worse than a bridge with one protection weakened.
        log.warning("%s", warning)

    if not settings.agentaus_api_key:
        log.error(
            "AGENTAUS_API_KEY is not set. Put it in %s or export it before starting.",
            env_path,
        )
        return 2

    # Only the core packages are required. A chosen component that is missing still lets
    # the bridge answer, just without the evidence those files hold - so say so; one the
    # user skipped at install is a choice, so it gets a single info line.
    from . import requirements

    lacking = requirements.missing()
    if lacking:
        log.error("%s", requirements.report(lacking))
        return 3
    degraded = requirements.degraded()
    if degraded:
        log.warning("%s", requirements.degraded_report(degraded))
    skipped = requirements.skipped()
    if skipped:
        log.info("not installed (skipped at install): %s", ", ".join(skipped))

    if args.check:
        return _check(settings)

    import uvicorn

    uvicorn.run(
        "agentaus_bridge.server:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=False,
        timeout_keep_alive=75,
    )
    return 0


def _check(settings) -> int:
    """One live round-trip against Agentaus so setup problems surface immediately."""
    import httpx

    log = logging.getLogger("agentaus-bridge")
    log.info("checking %s ...", settings.agentaus_url)
    try:
        response = httpx.post(
            settings.agentaus_url,
            headers={
                "Authorization": f"Bearer {settings.agentaus_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "stream": False,
            },
            timeout=60,
        )
    except httpx.HTTPError as exc:
        log.error("connection failed: %s", exc)
        return 1

    if response.status_code != 200:
        log.error("HTTP %s: %s", response.status_code, response.text[:300])
        return 1

    data = response.json()
    reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    log.info("OK - Agentaus replied %r (usage=%s)", reply.strip(), data.get("usage"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
