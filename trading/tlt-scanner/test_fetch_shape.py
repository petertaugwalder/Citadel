"""Both market-data providers must return raw OHLC plus a TR column, and fail closed.

Neither live API is reachable offline, so these feed each fetcher the payload shape
its provider actually returns:
  - Yahoo  : a DataFrame with Adj Close (MultiIndex from download(), flat from history())
  - Schwab : a JSON candles payload, yields quoted in index points

Yahoo carries distributions in Adj Close, so TR differs from Close. Schwab publishes
no adjusted close, so TR mirrors Close and backtest returns are price-only.
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
DAY_MS = 86_400_000
START_MS = 1_700_000_000_000


# ----------------------------------------------------------------- Yahoo fixtures

def yahoo_frame(dividend_every=21, rate=0.0035):
    """Raw OHLC plus the back-adjusted Adj Close yfinance pairs with it."""
    close = pd.Series(np.linspace(90.0, 100.0, DAYS), index=IDX)
    d = pd.Series(0.0, index=IDX)
    d.iloc[dividend_every::dividend_every] = rate
    factor = (1.0 + d).cumprod()
    factor = factor / factor.iloc[-1]
    return pd.DataFrame({
        "Open": close * 0.995, "High": close * 1.01,
        "Low": close * 0.99, "Close": close,
        "Adj Close": close * factor, "Volume": 1_000_000,
    }, index=IDX)


def fake_yf(download_frame, history_frame=None):
    mod = types.ModuleType("yfinance")
    mod.download = lambda *a, **k: download_frame
    mod.Ticker = lambda symbol: types.SimpleNamespace(history=lambda *a, **k: history_frame)
    return mod


# ---------------------------------------------------------------- Schwab fixtures

def schwab_payload(n=DAYS, first=90.0, step=0.05, scale=1.0):
    rows = []
    for i in range(n):
        close = (first + i * step) * scale
        rows.append({"datetime": START_MS + i * DAY_MS,
                     "open": close * 0.995, "high": close * 1.01,
                     "low": close * 0.99, "close": close, "volume": 1_000_000})
    return {"candles": rows}


class YahooFetchTests(unittest.TestCase):
    def fetch(self, download_frame, history_frame=None):
        with patch.dict(sys.modules, {"yfinance": fake_yf(download_frame, history_frame)}):
            return ts.fetch_yahoo("TLT")

    def test_multiindex_download_keeps_raw_close_and_tr(self):
        frame = yahoo_frame()
        mi = frame.copy()
        mi.columns = pd.MultiIndex.from_product([frame.columns, ["TLT"]])
        out = self.fetch(mi)
        self.assertIsNotNone(out)
        np.testing.assert_allclose(out["Close"].to_numpy(), frame["Close"].to_numpy(), rtol=1e-12)
        np.testing.assert_allclose(out["TR"].to_numpy(), frame["Adj Close"].to_numpy(), rtol=1e-12)
        self.assertLess(out["TR"].iloc[0], out["Close"].iloc[0])

    def test_raw_ohlc_is_not_scaled(self):
        frame = yahoo_frame()
        out = self.fetch(frame)
        for col in ("Open", "High", "Low"):
            np.testing.assert_allclose(out[col].to_numpy(), frame[col].to_numpy(), rtol=1e-12)

    def test_flat_history_fallback(self):
        frame = yahoo_frame()
        out = self.fetch(pd.DataFrame(), frame)
        np.testing.assert_allclose(out["TR"].to_numpy(), frame["Adj Close"].to_numpy(), rtol=1e-12)

    def test_missing_adj_close_falls_back_to_price_only(self):
        out = self.fetch(yahoo_frame().drop(columns=["Adj Close"]))
        np.testing.assert_allclose(out["TR"].to_numpy(), out["Close"].to_numpy(), rtol=0)

    def test_fetch_failure_returns_none(self):
        mod = types.ModuleType("yfinance")
        def boom(*a, **k):
            raise RuntimeError("CONNECT tunnel failed, response 403")
        mod.download = boom
        mod.Ticker = lambda s: types.SimpleNamespace(history=boom)
        with patch.dict(sys.modules, {"yfinance": mod}):
            self.assertIsNone(ts.fetch_yahoo("TLT"))


class SchwabFetchTests(unittest.TestCase):
    def fetch(self, payload, symbol="TLT", scale=1.0):
        with patch("schwab_client.price_history", return_value=payload):
            return ts.fetch_schwab(symbol, scale)

    def test_parses_ohlc_and_mirrors_tr_to_close(self):
        out = self.fetch(schwab_payload())
        self.assertEqual(len(out), DAYS)
        np.testing.assert_allclose(out["TR"].to_numpy(), out["Close"].to_numpy(), rtol=0)

    def test_yield_index_points_are_scaled(self):
        out = self.fetch(schwab_payload(n=30, first=5.15, step=0.001, scale=10.0),
                         symbol="$TYX", scale=10.0)
        self.assertAlmostEqual(float(out["Close"].iloc[0]), 5.15, places=6)

    def test_duplicate_sessions_collapse(self):
        payload = schwab_payload(n=5)
        payload["candles"].append(dict(payload["candles"][-1], close=999.0))
        out = self.fetch(payload)
        self.assertEqual(len(out), 5)
        self.assertAlmostEqual(float(out["Close"].iloc[-1]), 999.0, places=6)

    def test_index_is_naive_normalised_and_sorted(self):
        payload = schwab_payload(n=10)
        payload["candles"].reverse()
        out = self.fetch(payload)
        self.assertIsNone(out.index.tz)
        self.assertTrue(out.index.is_monotonic_increasing)

    def test_empty_and_malformed_payloads_return_none(self):
        self.assertIsNone(self.fetch({"candles": []}))
        self.assertIsNone(self.fetch({}))
        self.assertIsNone(self.fetch({"candles": [{"datetime": START_MS, "close": 1.0}]}))

    def test_api_failure_fails_closed(self):
        with patch("schwab_client.price_history", side_effect=RuntimeError("401")):
            self.assertIsNone(ts.fetch_schwab("TLT"))


class CacheTests(unittest.TestCase):
    """Caches are per-provider: the feeds disagree on units and on adjustment."""

    def test_providers_do_not_share_cache_files(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td, \
                patch.object(ts, "CACHE_DIR", Path(td)), \
                patch.dict(sys.modules, {"yfinance": fake_yf(yahoo_frame())}), \
                patch("schwab_client.price_history", return_value=schwab_payload()):
            ts.load_frames(source="yahoo")
            ts.load_frames(source="schwab")
            self.assertTrue((Path(td) / "yahoo" / "TLT.csv").exists())
            self.assertTrue((Path(td) / "schwab" / "TLT.csv").exists())

    def test_tr_survives_cache_and_legacy_cache_is_refetched(self):
        import tempfile
        from pathlib import Path

        calls = {"n": 0}

        def download(*a, **k):
            calls["n"] += 1
            return yahoo_frame()

        mod = types.ModuleType("yfinance")
        mod.download = download
        mod.Ticker = lambda s: types.SimpleNamespace(history=lambda *a, **k: yahoo_frame())

        with tempfile.TemporaryDirectory() as td, \
                patch.object(ts, "CACHE_DIR", Path(td)), \
                patch.dict(sys.modules, {"yfinance": mod}):
            first = ts.load_frames(source="yahoo")
            after_first = calls["n"]
            self.assertGreater(after_first, 0)
            ts.load_frames(source="yahoo")
            self.assertEqual(calls["n"], after_first, "second load should hit the cache")

            cached = pd.read_csv(Path(td) / "yahoo" / "TLT.csv", index_col=0, parse_dates=True)
            self.assertIn("TR", cached.columns)
            np.testing.assert_allclose(cached["TR"].to_numpy(),
                                       first["TLT"]["TR"].to_numpy(), rtol=1e-12)

            for csv in (Path(td) / "yahoo").glob("*.csv"):
                pd.read_csv(csv, index_col=0, parse_dates=True).drop(columns=["TR"]).to_csv(csv)
            before = calls["n"]
            again = ts.load_frames(source="yahoo")
            self.assertGreater(calls["n"], before, "a cache without TR must be refetched")
            self.assertIn("TR", again["TLT"].columns)

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            ts.load_frames(source="bloomberg")


if __name__ == "__main__":
    unittest.main(verbosity=2)
