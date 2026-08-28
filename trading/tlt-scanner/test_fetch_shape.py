"""fetch_yahoo must return raw OHLC and a separate total-return column.

The live fetch cannot be exercised offline, so these tests feed fetch_yahoo the
response shapes yfinance actually produces (MultiIndex from download(), flat from
Ticker.history(), and a series with no Adj Close) and assert the raw/TR split
survives each one.
"""
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import tlt_scanner as ts

DAYS = 120
IDX = pd.bdate_range("2026-01-02", periods=DAYS, tz="America/New_York")


def raw_and_adjusted(dividend_every=21, rate=0.0035):
    """A raw OHLC frame plus the Adj Close yfinance would pair with it."""
    close = pd.Series(np.linspace(90.0, 100.0, DAYS), index=IDX)
    d = pd.Series(0.0, index=IDX)
    d.iloc[dividend_every::dividend_every] = rate
    # back-adjustment: older bars scaled down by the distributions still to come
    factor = (1.0 + d).cumprod()
    factor = factor / factor.iloc[-1]
    frame = pd.DataFrame({
        "Open": close * 0.995, "High": close * 1.01,
        "Low": close * 0.99, "Close": close,
        "Adj Close": close * factor, "Volume": 1_000_000,
    }, index=IDX)
    return frame, factor


def fake_yf(download_frame, history_frame=None):
    """A stand-in yfinance module; fetch_yahoo imports it inside the function."""
    mod = types.ModuleType("yfinance")
    mod.download = lambda *a, **k: download_frame
    mod.Ticker = lambda symbol: types.SimpleNamespace(
        history=lambda *a, **k: history_frame)
    return mod


class FetchShapeTests(unittest.TestCase):
    def fetch(self, download_frame, history_frame=None):
        with patch.dict(sys.modules,
                        {"yfinance": fake_yf(download_frame, history_frame)}):
            return ts.fetch_yahoo("TLT")

    def test_multiindex_download_keeps_raw_close_and_tr(self):
        """download() returns (Price, Ticker) columns for a single ticker."""
        frame, factor = raw_and_adjusted()
        mi = frame.copy()
        mi.columns = pd.MultiIndex.from_product([frame.columns, ["TLT"]])
        out = self.fetch(mi)

        self.assertIsNotNone(out)
        self.assertIn("TR", out.columns)
        self.assertEqual(len(out), DAYS)
        # Close is the raw print, untouched by back-adjustment
        np.testing.assert_allclose(out["Close"].to_numpy(),
                                   frame["Close"].to_numpy(), rtol=1e-12)
        # TR carries the adjustment, and only TR
        np.testing.assert_allclose(out["TR"].to_numpy(),
                                   frame["Adj Close"].to_numpy(), rtol=1e-12)
        self.assertLess(out["TR"].iloc[0], out["Close"].iloc[0])
        self.assertAlmostEqual(out["TR"].iloc[-1], out["Close"].iloc[-1], places=6)

    def test_raw_ohlc_is_not_scaled(self):
        """Open/High/Low must be raw too -- the backtest fills at the open."""
        frame, _ = raw_and_adjusted()
        mi = frame.copy()
        mi.columns = pd.MultiIndex.from_product([frame.columns, ["TLT"]])
        out = self.fetch(mi)
        for col in ("Open", "High", "Low"):
            np.testing.assert_allclose(out[col].to_numpy(),
                                       frame[col].to_numpy(), rtol=1e-12)

    def test_flat_history_fallback(self):
        """Empty download() falls through to Ticker.history(), flat columns."""
        frame, _ = raw_and_adjusted()
        out = self.fetch(pd.DataFrame(), frame)
        self.assertIsNotNone(out)
        np.testing.assert_allclose(out["Close"].to_numpy(),
                                   frame["Close"].to_numpy(), rtol=1e-12)
        np.testing.assert_allclose(out["TR"].to_numpy(),
                                   frame["Adj Close"].to_numpy(), rtol=1e-12)

    def test_missing_adj_close_falls_back_to_price_only(self):
        """Yields and futures have no distributions; TR must equal Close."""
        frame, _ = raw_and_adjusted()
        out = self.fetch(frame.drop(columns=["Adj Close"]))
        self.assertIsNotNone(out)
        np.testing.assert_allclose(out["TR"].to_numpy(),
                                   out["Close"].to_numpy(), rtol=1e-12)

    def test_index_is_naive_and_normalised(self):
        frame, _ = raw_and_adjusted()
        out = self.fetch(frame)
        self.assertIsNone(out.index.tz)
        self.assertTrue((out.index == out.index.normalize()).all())

    def test_fetch_failure_returns_none(self):
        """A blocked or failing fetch must fail closed, not half-populate."""
        mod = types.ModuleType("yfinance")
        def boom(*a, **k):
            raise RuntimeError("CONNECT tunnel failed, response 403")
        mod.download = boom
        mod.Ticker = lambda s: types.SimpleNamespace(history=boom)
        with patch.dict(sys.modules, {"yfinance": mod}):
            self.assertIsNone(ts.fetch_yahoo("TLT"))


class CacheTests(unittest.TestCase):
    """The cache must carry TR, and must reject caches written before the split."""

    def setUp(self):
        self.frame, _ = raw_and_adjusted()
        self.calls = 0

    def _module(self):
        def download(*a, **k):
            self.calls += 1
            return self.frame
        mod = types.ModuleType("yfinance")
        mod.download = download
        mod.Ticker = lambda s: types.SimpleNamespace(
            history=lambda *a, **k: self.frame)
        return mod

    def test_tr_survives_cache_and_legacy_cache_is_refetched(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td, \
                patch.object(ts, "CACHE_DIR", Path(td)), \
                patch.dict(sys.modules, {"yfinance": self._module()}):
            first = ts.load_frames()
            after_first = self.calls
            ts.load_frames()
            self.assertEqual(self.calls, after_first, "second load should hit the cache")

            cached = pd.read_csv(Path(td) / "TLT.csv", index_col=0, parse_dates=True)
            self.assertIn("TR", cached.columns)
            np.testing.assert_allclose(cached["TR"].to_numpy(),
                                       first["TLT"]["TR"].to_numpy(), rtol=1e-12)

            for csv in Path(td).glob("*.csv"):
                df = pd.read_csv(csv, index_col=0, parse_dates=True).drop(columns=["TR"])
                df.to_csv(csv)
            before = self.calls
            again = ts.load_frames()
            self.assertGreater(self.calls, before, "a cache without TR must be refetched")
            self.assertIn("TR", again["TLT"].columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
