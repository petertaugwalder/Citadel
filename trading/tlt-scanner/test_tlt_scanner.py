import math
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import tlt_scanner as ts


class SchwabOnlyDataTests(unittest.TestCase):
    def test_schwab_yield_history_is_scaled_from_index_points(self):
        payload = {"candles": [
            {"datetime": 1_700_000_000_000, "open": 51.0, "high": 52.0,
             "low": 50.0, "close": 51.5, "volume": 0},
            {"datetime": 1_700_086_400_000, "open": 51.5, "high": 52.5,
             "low": 51.0, "close": 52.0, "volume": 0},
        ]}
        with patch("schwab_client.price_history", return_value=payload):
            df = ts.fetch_schwab("$TYX", scale=10.0)
        self.assertIsNotNone(df)
        self.assertAlmostEqual(float(df["Close"].iloc[-1]), 5.2)

    def test_empirical_duration_recovers_known_sensitivity(self):
        idx = pd.bdate_range("2026-01-01", periods=80)
        moves = np.array(([.02, -.03, .01, -.02, .04, -.01, .03, -.04] * 10)[:79])
        yields = [5.0]
        prices = [85.0]
        for move in moves:
            yields.append(yields[-1] + move)
            prices.append(prices[-1] * (1 - 15.0 * move / 100.0))
        frames = {
            "TLT": pd.DataFrame({"Close": prices}, index=idx),
            "TYX": pd.DataFrame({"Close": yields}, index=idx),
        }
        d, live, source = ts.fetch_duration(frames)
        self.assertTrue(live)
        self.assertAlmostEqual(d, 15.0, places=6)
        self.assertIn("Schwab", source)

    def test_duration_fails_closed_without_both_schwab_series(self):
        d, live, source = ts.fetch_duration({})
        self.assertTrue(math.isnan(d))
        self.assertFalse(live)
        self.assertIn("unavailable", source)


if __name__ == "__main__":
    unittest.main()
