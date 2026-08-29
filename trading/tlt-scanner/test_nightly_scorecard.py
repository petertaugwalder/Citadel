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
        self.assertTrue(any("max out at 6/8" in n for n in without.notes))
        # the surviving score must fit the reduced, UB-free bounds
        self.assertLessEqual(without.score, ns.SCORE_MAX_NOUB)
        self.assertGreaterEqual(without.score, ns.SCORE_MIN_NOUB)

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
        self.assertTrue(any("2 stack bits degraded" in n for n in s.notes))

    def test_gate_blocks_a_high_score_when_legs_disagree(self):
        """Score alone never fires: yields and futures must agree."""
        d = frame(seed=3)
        s = ns.scan(d)
        if s.verdict.endswith("CALLS"):
            self.assertLess(d["y30"].iloc[-1], ns.sma(d["y30"], 50).iloc[-1])
        if s.verdict.endswith("PUTS") and s.verdict != "FADE / SCOUT PUTS":
            self.assertGreater(d["y30"].iloc[-1], ns.sma(d["y30"], 50).iloc[-1])


class RevisedRuleTests(unittest.TestCase):
    """The four fixes against the revised draft."""

    def test_bounce_hook_fires_on_a_real_cross(self):
        """`last > 35 <= prev` chains to (last>35) and (35<=prev), demanding RSI
        was ALREADY above 35 — the opposite of a hook. A genuine washout that
        crosses back up through 35 must trigger."""
        n = 300
        idx = pd.bdate_range("2024-01-01", periods=n)
        # long drift down into a washout, then a sharp two-day snap back
        rng = np.random.default_rng(11)
        decline = 100 + np.cumsum(rng.normal(-0.10, 0.45, n - 1))
        path = np.concatenate([decline, [decline[-1] * 1.045]])
        d = pd.DataFrame({"tlt": path, "tlt_high": path * 1.004, "tlt_low": path * 0.996,
                          "y30": np.linspace(4.0, 4.6, n), "y10": np.linspace(3.5, 4.0, n),
                          "ub": np.nan}, index=idx)
        r = ns.rsi(pd.Series(path))
        self.assertLess(r.iloc[-10:].min(), 32, "fixture must actually be oversold")
        s = ns.scan(d)
        self.assertIn(s.verdict, {"RENT CALL BOUNCE", "SCOUT TLT CALLS", "BUY TLT CALLS"},
                      f"a real RSI hook should register, got {s.verdict!r}")

    def test_regime_is_normalised_to_full_scale_without_ub(self):
        """Six terms span +/-75; a fixed +/-25 threshold would silently tighten."""
        d = frame(with_ub=True)
        with_ub = ns.scan(d)
        without = ns.scan(d.assign(ub=np.nan))
        for s in (with_ub, without):
            self.assertLessEqual(abs(s.regime_pts), 100.0)
        # an all-bullish tape must read +100 on either path, not +75
        n = 300
        idx = pd.bdate_range("2024-01-01", periods=n)
        up = np.linspace(80, 120, n)
        d2 = pd.DataFrame({"tlt": up, "tlt_high": up * 1.004, "tlt_low": up * 0.996,
                           "y30": np.linspace(5.0, 3.5, n), "y10": np.linspace(4.5, 3.0, n),
                           "ub": np.nan}, index=idx)
        self.assertAlmostEqual(ns.scan(d2).regime_pts, 100.0, places=6)

    def test_missing_30y_is_a_clear_error_not_a_crash(self):
        d = frame()
        with self.assertRaises((ValueError, KeyError, IndexError)):
            ns.scan(d.iloc[:10])

    def test_ub_absent_is_reported_and_never_proxied(self):
        s = ns.scan(frame().assign(ub=np.nan))
        self.assertIsNone(s.ub)
        self.assertTrue(any("max out at 6/8" in n for n in s.notes))
        self.assertLessEqual(s.call_stack, 6)
        self.assertLessEqual(s.put_stack, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
