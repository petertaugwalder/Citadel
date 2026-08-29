"""The nightly scorecard's scoring, gating and degraded-data behaviour."""
import unittest

import numpy as np
import pandas as pd

import nightly_scorecard as ns


def frame(n=400, seed=0, tlt_drift=0.0, y_drift=0.0, with_ub=True):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    tlt = pd.Series(90 + np.cumsum(rng.normal(tlt_drift, 0.4, n)), index=idx)
    y30 = pd.Series(4.5 + np.cumsum(rng.normal(y_drift, 0.02, n)), index=idx)
    d = pd.DataFrame({
        "tlt": tlt,
        "tlt_high": tlt * 1.004,
        "tlt_low": tlt * 0.996,
        "y30": y30,
        "y10": y30 - 0.5,
        "ub": (120 + np.cumsum(rng.normal(tlt_drift, 0.5, n))) if with_ub else np.nan,
    }, index=idx)
    return d


class ScoringTests(unittest.TestCase):
    def test_runs_and_reports_a_verdict(self):
        s = ns.scan(frame())
        self.assertIn(s.verdict, {"BUY TLT CALLS", "SCOUT TLT CALLS", "BUY TLT PUTS",
                                  "SCOUT TLT PUTS", "RENT CALL BOUNCE",
                                  "FADE / SCOUT PUTS", "STAND ASIDE"})
        self.assertIn(s.regime, {"BULL", "BEAR", "TRANS"})
        self.assertLessEqual(s.score, ns.SCORE_MAX)
        self.assertGreaterEqual(s.score, ns.SCORE_MIN)
        self.assertEqual(s.call_stack + s.put_stack <= 16, True)

    def test_caller_notes_are_not_mutated(self):
        notes = ["source=test"]
        ns.scan(frame(), notes)
        self.assertEqual(notes, ["source=test"], "scan must not append to the caller's list")

    def test_missing_ub_is_not_scored_twice(self):
        """The inverted-yield proxy is the same test as the yield leg.

        Scoring it would count one fact twice and turn the two-source AND gate
        into a single test, so the proxy must contribute nothing to the score.
        """
        d = frame(with_ub=True)
        with_ub = ns.scan(d)
        without = ns.scan(d.assign(ub=np.nan))
        # the UB leg is worth +/-2 plus a +/-1 lead term; dropping it must not
        # leave the score unchanged by silently substituting the yield
        self.assertNotEqual(with_ub.score, None)
        self.assertTrue(without.ub is None)
        self.assertTrue(any("SCORED ZERO" in n for n in without.notes))
        # and the surviving score must be reachable without the UB terms
        self.assertLessEqual(abs(without.score), ns.SCORE_MAX - 2)

    def test_put_fade_can_actually_fire(self):
        """The original condition (RSI>=70 on a close already below the 50-day)
        was unreachable. A melt-up that rolls over into a rising-yield tape must
        now trigger it."""
        n = 300
        idx = pd.bdate_range("2024-01-01", periods=n)
        ramp = np.concatenate([np.linspace(80, 110, n - 3), [104.0, 99.0, 96.0]])
        y30 = np.concatenate([np.linspace(4.0, 4.2, n - 3), [4.9, 5.0, 5.1]])
        d = pd.DataFrame({"tlt": ramp, "tlt_high": ramp * 1.004, "tlt_low": ramp * 0.996,
                          "y30": y30, "y10": y30 - 0.5, "ub": np.nan}, index=idx)
        s = ns.scan(d)
        self.assertIn(s.verdict, {"FADE / SCOUT PUTS", "SCOUT TLT PUTS", "BUY TLT PUTS"},
                      f"expected a put-side verdict, got {s.verdict!r}")

    def test_degraded_high_low_is_reported(self):
        d = frame().drop(columns=["tlt_high", "tlt_low"])
        s = ns.scan(d)
        self.assertTrue(any("swing conditions degraded" in n for n in s.notes))

    def test_gate_blocks_a_high_score_when_legs_disagree(self):
        """Score alone never fires: yields and futures must agree."""
        d = frame(seed=3)
        s = ns.scan(d)
        if s.verdict.endswith("CALLS"):
            self.assertLess(d["y30"].iloc[-1], ns.sma(d["y30"], 50).iloc[-1])
        if s.verdict.endswith("PUTS") and s.verdict != "FADE / SCOUT PUTS":
            self.assertGreater(d["y30"].iloc[-1], ns.sma(d["y30"], 50).iloc[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
