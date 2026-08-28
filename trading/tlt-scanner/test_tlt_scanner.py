"""fetch_duration must fail closed and never present a cached value as live.

Duration is the one input that converts a yield move into an expected TLT move,
so a wrong or silently-stale D produces a confident, wrong number. The contract:
only a value fetched during this scan sets is_live, a dated cache may be shown
but never drives implied P&L, and anything else returns NaN rather than a
fallback constant. The live fetch cannot be exercised offline, so these tests
stand in for yfinance's fund-data object.
"""
import json
import math
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import tlt_scanner as ts


def holdings(duration: float) -> pd.DataFrame:
    """The bond_holdings frame yfinance returns, duration on a labelled row."""
    return pd.DataFrame({"TLT": [duration, 8.4]},
                        index=["Effective Duration", "Effective Maturity"])


def fake_yf(bond_holdings=None, fails: bool = False):
    """A stand-in yfinance module; fetch_duration imports it inside the function."""
    mod = types.ModuleType("yfinance")

    def ticker(symbol):
        if fails:
            raise RuntimeError("fund data endpoint unavailable")
        return types.SimpleNamespace(
            funds_data=types.SimpleNamespace(bond_holdings=bond_holdings))

    mod.Ticker = ticker
    return mod


class DurationContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        p = patch.object(ts, "CACHE_DIR", self.cache_dir)
        p.start()
        self.addCleanup(p.stop)

    def write_cache(self, d, as_of="2026-08-26", age_days=0.0):
        cache = self.cache_dir / "duration.json"
        cache.write_text(json.dumps({"d": d, "as_of": as_of}))
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(cache, (old, old))
        return cache

    def fetch(self, refresh=False, bond_holdings=None, fails=False):
        with patch.dict(sys.modules, {"yfinance": fake_yf(bond_holdings, fails)}):
            return ts.fetch_duration(refresh=refresh)

    def test_fails_closed_with_no_cache_and_no_live_source(self):
        """No fallback constant: an unavailable D is NaN, not 15.0."""
        d, live, source, as_of = self.fetch(refresh=True, fails=True)
        self.assertTrue(math.isnan(d))
        self.assertFalse(live)
        self.assertIsNone(as_of)
        self.assertIn("no current or dated cached", source)

    def test_live_fund_data_is_live_and_is_cached(self):
        d, live, source, as_of = self.fetch(bond_holdings=holdings(14.96))
        self.assertAlmostEqual(d, 14.96)
        self.assertTrue(live)
        self.assertIn("live", source)
        self.assertEqual(as_of, pd.Timestamp.now(tz="UTC").date().isoformat())
        payload = json.loads((self.cache_dir / "duration.json").read_text())
        self.assertAlmostEqual(float(payload["d"]), 14.96)

    def test_cached_value_is_shown_but_never_live(self):
        """A dated cache is display material only -- is_live gates implied P&L."""
        self.write_cache(14.96)
        d, live, source, as_of = self.fetch(bond_holdings=holdings(15.40))
        self.assertAlmostEqual(d, 14.96)
        self.assertFalse(live)
        self.assertIn("STALE", source)
        self.assertEqual(as_of, "2026-08-26")

    def test_refresh_bypasses_the_cache_and_reaches_the_live_source(self):
        """--refresh is why a stale D can flip to live (or to UNAVAILABLE)."""
        self.write_cache(14.96)
        d, live, source, as_of = self.fetch(refresh=True, bond_holdings=holdings(15.40))
        self.assertAlmostEqual(d, 15.40)
        self.assertTrue(live)
        self.assertIn("live", source)

    def test_refresh_over_a_dead_source_reports_unavailable_not_the_cache(self):
        """The failure the tape actually shows: --refresh + a dead endpoint."""
        self.write_cache(14.96)
        d, live, _source, _as_of = self.fetch(refresh=True, fails=True)
        self.assertTrue(math.isnan(d))
        self.assertFalse(live)

    def test_cache_older_than_a_week_is_not_used(self):
        self.write_cache(14.96, age_days=8)
        d, live, _source, _as_of = self.fetch(fails=True)
        self.assertTrue(math.isnan(d))
        self.assertFalse(live)

    def test_out_of_band_values_are_rejected(self):
        """5 < D < 30 -- a parse that lands on maturity or a percentage is not D."""
        for bad in (0.0, 4.9, 30.0, 104.0):
            with self.subTest(bad=bad):
                d, live, _source, _as_of = self.fetch(bond_holdings=holdings(bad))
                self.assertTrue(math.isnan(d))
                self.assertFalse(live)

    def test_corrupt_cache_does_not_crash_the_scan(self):
        (self.cache_dir / "duration.json").write_text("{not json")
        d, live, _source, _as_of = self.fetch(fails=True)
        self.assertTrue(math.isnan(d))
        self.assertFalse(live)


if __name__ == "__main__":
    unittest.main(verbosity=2)
