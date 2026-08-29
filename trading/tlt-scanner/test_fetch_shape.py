"""fetch_schwab must return raw OHLC in displayed units, and fail closed.

The live API cannot be reached offline, so these feed fetch_schwab the payload
shape Schwab actually returns and assert the parsing, unit scaling and failure
behaviour. Also covers the on-disk cache contract.
"""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import tlt_scanner as ts

DAY_MS = 86_400_000
START_MS = 1_700_000_000_000


def candles(n=120, first=90.0, step=0.05, scale=1.0):
    """A Schwab pricehistory payload, quoted in the instrument's native units."""
    rows = []
    for i in range(n):
        close = (first + i * step) * scale
        rows.append({
            "datetime": START_MS + i * DAY_MS,
            "open": close * 0.995, "high": close * 1.01,
            "low": close * 0.99, "close": close, "volume": 1_000_000,
        })
    return {"candles": rows}


class FetchSchwabTests(unittest.TestCase):
    def fetch(self, payload, symbol="TLT", scale=1.0):
        with patch("schwab_client.price_history", return_value=payload):
            return ts.fetch_schwab(symbol, scale)

    def test_parses_ohlc_and_sets_tr_to_close(self):
        out = self.fetch(candles())
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 120)
        for col in ("Open", "High", "Low", "Close", "TR"):
            self.assertIn(col, out.columns)
        # Schwab publishes no adjusted close, so TR must mirror Close exactly
        np.testing.assert_allclose(out["TR"].to_numpy(), out["Close"].to_numpy(), rtol=0)

    def test_yield_index_points_are_scaled(self):
        """$TYX arrives in index points: 52.0 is a 5.20% yield."""
        out = self.fetch(candles(n=30, first=5.15, step=0.001, scale=10.0),
                         symbol="$TYX", scale=10.0)
        self.assertAlmostEqual(float(out["Close"].iloc[0]), 5.15, places=6)
        self.assertLess(float(out["Close"].max()), 10.0)

    def test_prices_are_not_scaled(self):
        out = self.fetch(candles(n=10, first=83.0, step=0.0))
        self.assertAlmostEqual(float(out["Close"].iloc[0]), 83.0, places=6)

    def test_index_is_naive_normalised_and_sorted(self):
        payload = candles(n=10)
        payload["candles"].reverse()
        out = self.fetch(payload)
        self.assertIsNone(out.index.tz)
        self.assertTrue((out.index == out.index.normalize()).all())
        self.assertTrue(out.index.is_monotonic_increasing)

    def test_duplicate_sessions_collapse(self):
        payload = candles(n=5)
        payload["candles"].append(dict(payload["candles"][-1], close=999.0))
        out = self.fetch(payload)
        self.assertEqual(len(out), 5)
        self.assertAlmostEqual(float(out["Close"].iloc[-1]), 999.0, places=6)

    def test_empty_and_malformed_payloads_return_none(self):
        self.assertIsNone(self.fetch({"candles": []}))
        self.assertIsNone(self.fetch({}))
        self.assertIsNone(self.fetch({"candles": [{"datetime": START_MS, "close": 1.0}]}))

    def test_api_failure_fails_closed(self):
        with patch("schwab_client.price_history",
                   side_effect=RuntimeError("GET /pricehistory failed (401)")):
            self.assertIsNone(ts.fetch_schwab("TLT"))


class CacheTests(unittest.TestCase):
    """The cache must carry TR, and reject caches written before the raw split."""

    def test_tr_survives_cache_and_legacy_cache_is_refetched(self):
        import tempfile
        from pathlib import Path

        calls = {"n": 0}

        def price_history(symbol, start_ms, end_ms):
            calls["n"] += 1
            return candles()

        with tempfile.TemporaryDirectory() as td, \
                patch.object(ts, "CACHE_DIR", Path(td)), \
                patch("schwab_client.price_history", side_effect=price_history):
            first = ts.load_frames()
            after_first = calls["n"]
            self.assertGreater(after_first, 0)
            ts.load_frames()
            self.assertEqual(calls["n"], after_first, "second load should hit the cache")

            cached = pd.read_csv(Path(td) / "TLT.csv", index_col=0, parse_dates=True)
            self.assertIn("TR", cached.columns)
            np.testing.assert_allclose(cached["TR"].to_numpy(),
                                       first["TLT"]["TR"].to_numpy(), rtol=1e-12)

            for csv in Path(td).glob("*.csv"):
                pd.read_csv(csv, index_col=0, parse_dates=True) \
                    .drop(columns=["TR"]).to_csv(csv)
            before = calls["n"]
            again = ts.load_frames()
            self.assertGreater(calls["n"], before, "a cache without TR must be refetched")
            self.assertIn("TR", again["TLT"].columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
