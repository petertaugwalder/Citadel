"""The raw-levels / total-return-accounting split.

Every level the scanner prints or signals on is a raw (unadjusted) price, so the
50/200-day and the EMAs match the chart the trade is placed against. Distributions
are accounted for only in --backtest returns, via the TR column.

These tests inject a known distribution stream into TLT's TR and assert that the
signal side does not move at all while the return side picks it up exactly.
"""
import unittest

import pandas as pd

import tlt_scanner as ts

RATE, EVERY = 0.0035, 21  # 0.35% every 21 sessions ~= TLT's ~4.2%/yr distribution


def frames_with_distributions():
    """Demo frames plus a known distribution stream, and the injected factor."""
    frames = {k: v.copy() for k, v in ts.demo_frames().items()}
    tlt = frames["TLT"]
    d = pd.Series(0.0, index=tlt.index)
    d.iloc[EVERY::EVERY] = RATE
    factor = (1.0 + d).cumprod()
    tlt["TR"] = tlt["Close"] * factor
    return frames, factor


def window_factor(factor, window):
    """Compounded distributions inside a backtest window, which drops warmup bars."""
    lo, hi = (pd.Timestamp(x.strip()) for x in window.replace("→", "->").split("->"))
    seg = factor[(factor.index >= lo) & (factor.index <= hi)]
    return float(seg.iloc[-1] / seg.iloc[0])


class PriceBasisTests(unittest.TestCase):
    def setUp(self):
        self.base = ts.demo_frames()
        self.inj, self.factor = frames_with_distributions()

    def test_enrich_defaults_tr_to_close(self):
        """Frames arriving without a TR column must not crash the replay."""
        df = ts.demo_frames()["TLT"].drop(columns=["TR"])
        self.assertIn("TR", ts.enrich(df).columns)

    def test_levels_and_tape_ignore_distributions(self):
        a, b = ts.analyze(self.base), ts.analyze(self.inj)
        self.assertEqual(a["tape"], b["tape"])
        self.assertEqual(a["plan"]["levels"], b["plan"]["levels"])
        self.assertEqual(a["plan"]["action"], b["plan"]["action"])
        self.assertEqual(a["stack"], b["stack"])
        self.assertEqual(a["exit"], b["exit"])

    def test_entries_and_exits_do_not_move(self):
        a = ts.backtest(self.base)["summary"]["trades"]
        b = ts.backtest(self.inj)["summary"]["trades"]
        for key in ("closed", "open_at_end", "avg_days_held"):
            self.assertEqual(a[key], b[key], key)

    def test_buy_and_hold_is_total_return(self):
        a = ts.backtest(self.base)["summary"]
        b = ts.backtest(self.inj)["summary"]
        price_only = a["buy_and_hold_tlt"]["total_return_pct"]
        f = window_factor(self.factor, a["window"])
        expected = ((1 + price_only / 100) * f - 1) * 100
        self.assertAlmostEqual(b["buy_and_hold_tlt"]["total_return_pct"], expected, delta=0.02)

    def test_strategy_earns_distributions_in_proportion_to_exposure(self):
        a = ts.backtest(self.base)["summary"]
        b = ts.backtest(self.inj)["summary"]
        gain = b["strategy"]["total_return_pct"] - a["strategy"]["total_return_pct"]
        bh_gain = (b["buy_and_hold_tlt"]["total_return_pct"]
                   - a["buy_and_hold_tlt"]["total_return_pct"])
        self.assertGreater(gain, 0, "a long book must earn its distributions")
        self.assertLess(gain, bh_gain, "part-time exposure cannot out-earn holding")
        f = window_factor(self.factor, a["window"])
        mean_w = a["strategy"]["exposure_pct"] / 100 * a["strategy"]["avg_weight_when_in"]
        self.assertAlmostEqual(gain, 100 * mean_w * (f - 1), delta=0.75)

    def test_zero_distributions_leave_returns_untouched(self):
        """TR == Close (futures, yields, demo data) must reproduce price-only results."""
        a = ts.backtest(self.base)["summary"]["strategy"]
        flat = {k: v.copy() for k, v in self.base.items()}
        flat["TLT"]["TR"] = flat["TLT"]["Close"]
        b = ts.backtest(flat)["summary"]["strategy"]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
