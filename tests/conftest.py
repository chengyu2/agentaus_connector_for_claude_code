"""Shared pytest setup for the test suite."""

import pytest


@pytest.fixture(autouse=True)
def _fresh_token_calibrator():
    """Start every test with an uncalibrated `agentaus_bridge.tokens.calibrator`.

    The calibrator is one object shared by the whole process: `server.py` feeds it
    every usage report an upstream sends, and `translate.py` scales every token
    estimate by it. Tests with stand-in upstreams report usage that drags its slope
    toward the 0.5 floor, so a later test in the same process - which, under
    `pytest-xdist --dist loadfile`, depends on how files land on workers - would see
    its estimates shrink. `test_thinking.py` alone takes the slope to ~0.59, enough to
    make `_worth_refitting` call a 200,000-token request fitting in 131,072.

    The instance is reset in place, not replaced, because `server.py` and
    `translate.py` hold their own reference to it via `from .tokens import calibrator`.
    Calibration that builds up within a single test is unaffected.
    """
    from agentaus_bridge.tokens import calibrator

    calibrator.reset()
    yield
