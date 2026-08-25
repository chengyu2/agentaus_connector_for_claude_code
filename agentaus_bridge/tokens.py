"""Token counting for the context-window arithmetic.

Counting characters and dividing by four is wrong in both directions depending on the
text: dense code and JSON tokenise far worse than prose, and the error is large enough
to matter when the whole point is deciding whether a request fits a hard limit. An
under-count sends a request that Agentaus rejects; an over-count compacts a
conversation that did not need it.

Two mechanisms improve on it:

1. **A real BPE tokeniser** (`tiktoken`) when it is installed, which is far closer than
   any character ratio.

2. **Calibration against Agentaus itself.** Even a real tokeniser is the *wrong* one -
   Agentaus does not publish which it uses. But every response reports
   `usage.input_tokens` for a request we just counted ourselves, so the ratio between
   the two is directly observable. The bridge tracks it and corrects, which converges
   on the truth without ever knowing the model's tokeniser.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("agentaus-bridge")

try:  # optional dependency: the bridge works without it, just less precisely
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - depends on the install
    _ENCODING = None


def has_tokeniser() -> bool:
    return _ENCODING is not None


def count_tokens(text: str) -> int:
    """Token count for `text`, by BPE where available and characters otherwise."""
    if not text:
        return 0
    if _ENCODING is not None:
        try:
            return len(_ENCODING.encode(text, disallowed_special=()))
        except Exception:  # pragma: no cover - never fail a request over counting
            pass
    return max(1, len(text) // 4)


class TokenCalibrator:
    """Learns how our token count maps onto the one Agentaus charges.

    Measured against the live API, the relationship is **additive, not multiplicative**:

        our count      Agentaus      difference
               75         2,490          +2,415
              525         2,940          +2,415
            5,025         7,440          +2,415

    A constant ~2,415 tokens of per-request overhead, with a slope of essentially 1 -
    so a BPE count is already accurate and what must be accounted for is the fixed
    cost Agentaus adds on top.

    Modelling this as a ratio is actively harmful. Fitted on a small request the ratio
    looks like 33x, and applying that to a large one inflates a 50,000-token
    conversation past the window and rejects a request that would have been fine.
    That is not hypothetical: it happened while this was being built.

        reported ~= slope * counted + overhead
    """

    # Guards against a malformed usage field poisoning the arithmetic.
    _SLOPE_RANGE = (0.5, 2.0)
    _OVERHEAD_RANGE = (0, 32768)

    def __init__(self, alpha: float = 0.35, safety_tokens: int = 256) -> None:
        self._slope = 1.0
        self._overhead = 0.0
        self._alpha = alpha
        self._safety_tokens = safety_tokens
        self._lock = threading.Lock()
        self._samples: list[tuple[int, int]] = []
        self.samples = 0

    @property
    def slope(self) -> float:
        return self._slope

    @property
    def overhead(self) -> int:
        return int(self._overhead)

    @property
    def ratio(self) -> float:
        """Kept for reporting only; the model is not multiplicative."""
        return self._slope

    def observe(self, counted: int, reported: int) -> None:
        if counted <= 0 or reported <= 0:
            return
        with self._lock:
            self._samples.append((counted, reported))
            if len(self._samples) > 64:
                self._samples.pop(0)
            self.samples += 1

            spread = {c for c, _ in self._samples}
            if len(self._samples) >= 3 and len(spread) >= 3:
                slope, overhead = self._fit(self._samples)
            else:
                # Too few points to separate slope from intercept: assume slope 1 and
                # attribute the whole difference to fixed overhead, which is the
                # conservative reading and matches what was measured.
                slope = 1.0
                overhead = reported - counted

            slope = min(max(slope, self._SLOPE_RANGE[0]), self._SLOPE_RANGE[1])
            overhead = min(max(overhead, self._OVERHEAD_RANGE[0]), self._OVERHEAD_RANGE[1])

            if self.samples == 1:
                self._slope, self._overhead = slope, float(overhead)
            else:
                self._slope = (1 - self._alpha) * self._slope + self._alpha * slope
                self._overhead = (1 - self._alpha) * self._overhead + self._alpha * overhead

            if self.samples in (1, 3, 10) or self.samples % 50 == 0:
                log.info(
                    "token calibration: reported ~= %.3f * counted + %d "
                    "(%d samples; latest counted %d, reported %d)",
                    self._slope, int(self._overhead), self.samples, counted, reported,
                )

    @staticmethod
    def _fit(samples: list[tuple[int, int]]) -> tuple[float, float]:
        """Least-squares fit of reported against counted."""
        n = len(samples)
        sx = sum(c for c, _ in samples)
        sy = sum(r for _, r in samples)
        sxx = sum(c * c for c, _ in samples)
        sxy = sum(c * r for c, r in samples)
        denominator = n * sxx - sx * sx
        if denominator == 0:
            return 1.0, (sy - sx) / n
        slope = (n * sxy - sx * sy) / denominator
        intercept = (sy - slope * sx) / n
        return slope, intercept

    def adjust(self, counted: int) -> int:
        """Our count, corrected toward what Agentaus will actually charge.

        A small safety margin is added because under-counting gets a request rejected,
        while over-counting only compacts slightly sooner than strictly necessary.
        """
        return int(counted * self._slope + self._overhead) + self._safety_tokens


calibrator = TokenCalibrator()
