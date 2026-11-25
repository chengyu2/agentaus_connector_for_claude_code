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
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run install/config checks and exit without starting the server",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="With --doctor, skip the live API credential check",
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

    if args.doctor:
        # Deliberately before the key/requirements hard-exits below: --doctor must be
        # able to *report* those problems, not die on them.
        from . import doctor

        return doctor.run(offline=args.offline)

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

    if settings.host not in ("127.0.0.1", "::1", "localhost") and not settings.bridge_token:
        log.warning(
            "BRIDGE_HOST=%s exposes the bridge to your whole network with no BRIDGE_TOKEN: "
            "anyone on this Wi-Fi could spend your Agentaus credit. Set BRIDGE_TOKEN in .env.",
            settings.host,
        )

    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((settings.host if settings.host != "localhost" else "127.0.0.1",
                        settings.port))
        except OSError:
            log.error(
                "Port %s is already in use - most likely an older bridge is still running "
                "(check other terminal windows, or the autostart task). Stop it first, or "
                "start this one with --port <other> and update ANTHROPIC_BASE_URL to match.",
                settings.port,
            )
            return 4

    import uvicorn

    uvicorn.run(
        "agentaus_bridge.server:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=False,
        timeout_keep_alive=75,
        # Ctrl+C used to hang on "Waiting for connections to close" while Claude Code
        # held a stream open. Give in-flight requests a few seconds, then exit.
        timeout_graceful_shutdown=5,
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
