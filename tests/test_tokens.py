"""Token counting and calibration.

The context-window arithmetic is only as good as the token count behind it. Counting
characters and dividing by four under-counts dense code and JSON by roughly half, which
turns "this request fits" into a rejected request.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge.tokens import TokenCalibrator, count_tokens, has_tokeniser  # noqa: E402


class TestCounting(unittest.TestCase):
    def test_code_is_not_under_counted_the_way_chars_over_four_is(self):
        """The failure that motivated this: dense code tokenises far worse than prose."""
        code = 'def f(x): return {"a": [1,2,3], "b": x*2}\n' * 200

        naive = len(code) // 4
        real = count_tokens(code)

        if has_tokeniser():
            self.assertGreater(real, naive * 1.5,
                               "real count should be far above the character estimate")

    def test_empty_text_is_free(self):
        self.assertEqual(count_tokens(""), 0)

    def test_counting_never_raises_on_odd_input(self):
        """Counting must never be the reason a request fails."""
        for text in ("\x00\x01", "🙂" * 100, "<|endoftext|>", "a" * 100_000):
            self.assertGreaterEqual(count_tokens(text), 0)


class TestCalibration(unittest.TestCase):
    """Calibration must model Agentaus' fixed per-request overhead, not a ratio.

    Measured against the live API:

        our count      Agentaus      difference
               75         2,490          +2,415
              525         2,940          +2,415
            5,025         7,440          +2,415

    Constant additive overhead, slope 1. Fitting a *ratio* to the first row gives 33x;
    applying that to a 50,000-token conversation inflates it past the window and
    rejects a request that would have been fine. That happened while building this.
    """

    MEASURED = [(75, 2490), (525, 2940), (5025, 7440)]

    def test_recovers_the_measured_relationship(self):
        c = TokenCalibrator(alpha=1.0, safety_tokens=0)
        for counted, reported in self.MEASURED:
            c.observe(counted, reported)

        self.assertAlmostEqual(c.slope, 1.0, places=2)
        self.assertAlmostEqual(c.overhead, 2415, delta=50)

    def test_large_request_is_not_inflated_by_a_small_sample(self):
        """The specific bug: a ratio fitted on small requests wrecks large ones."""
        c = TokenCalibrator(alpha=1.0, safety_tokens=0)
        for counted, reported in self.MEASURED:
            c.observe(counted, reported)

        # 50,000 real tokens plus ~2,415 overhead. A ratio model would say ~1,650,000.
        self.assertLess(c.adjust(50_000), 60_000,
                        "a large request was inflated far past its true size")
        self.assertGreater(c.adjust(50_000), 50_000, "overhead must still be counted")

    def test_fixed_overhead_dominates_a_tiny_request(self):
        c = TokenCalibrator(alpha=1.0, safety_tokens=0)
        for counted, reported in self.MEASURED:
            c.observe(counted, reported)

        self.assertGreater(c.adjust(10), 2000, "per-request overhead was not applied")

    def test_single_sample_assumes_slope_one(self):
        """With one point, slope and intercept cannot be separated. Assume slope 1 and
        attribute the difference to overhead, which is the conservative reading."""
        c = TokenCalibrator(alpha=1.0, safety_tokens=0)
        c.observe(counted=1000, reported=3400)

        self.assertAlmostEqual(c.slope, 1.0, places=2)
        self.assertAlmostEqual(c.overhead, 2400, delta=10)

    def test_absurd_values_are_clamped(self):
        c = TokenCalibrator(alpha=1.0, safety_tokens=0)
        c.observe(1000, 10_000_000)

        self.assertLessEqual(c.slope, 2.0)
        self.assertLessEqual(c.overhead, 32768)

    def test_ignores_useless_observations(self):
        c = TokenCalibrator()
        for counted, reported in ((0, 100), (100, 0), (-5, 10), (10, -5)):
            c.observe(counted, reported)

        self.assertEqual(c.samples, 0)

    def test_biases_upward_because_under_counting_is_the_expensive_error(self):
        """Over-counting compacts sooner than needed; under-counting gets rejected."""
        c = TokenCalibrator(safety_tokens=256)

        self.assertGreater(c.adjust(1000), 1000)

    def test_starts_neutral_before_any_evidence(self):
        c = TokenCalibrator(safety_tokens=0)

        self.assertEqual(c.adjust(1234), 1234)

    def test_smooths_rather_than_lurching_on_one_outlier(self):
        c = TokenCalibrator(alpha=0.35, safety_tokens=0)
        for counted, reported in self.MEASURED:
            c.observe(counted, reported)
        before = c.overhead
        c.observe(1000, 25_000)   # an outlier

        self.assertLess(c.overhead, before + 12_000, "one outlier moved it too far")


if __name__ == "__main__":
    unittest.main()
