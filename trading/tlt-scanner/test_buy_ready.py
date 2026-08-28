import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import schwab_client as sc
import tlt_scanner as ts


NOW = datetime(2026, 8, 27, 21, 0, tzinfo=timezone.utc)  # 17:00 ET, post-close
QUOTE_MS = int(datetime(2026, 8, 27, 19, 59, tzinfo=timezone.utc).timestamp() * 1000)


def contract(strike=33.0, dte=63, delta=.70, bid=2.00, ask=2.10,
             oi=500, volume=20, theta=-.002, quote_ms=QUOTE_MS, volatility=15.0):
    return {
        "daysToExpiration": dte, "strikePrice": strike, "delta": delta,
        "bid": bid, "ask": ask, "mark": (bid + ask) / 2,
        "theta": theta, "volatility": volatility, "openInterest": oi,
        "totalVolume": volume, "quoteTimeInLong": quote_ms,
        "nonStandard": False, "mini": False,
    }


def chain(*contracts, truncated=False, delayed=False, status="SUCCESS"):
    by_strike = {str(c["strikePrice"]): [c] for c in contracts}
    return {
        "status": status, "isDelayed": delayed, "isChainTruncated": truncated,
        "underlyingPrice": 35.0,
        "callExpDateMap": {"2026-10-29:63": by_strike},
        "putExpDateMap": {"2026-10-29:63": by_strike},
    }


class BuyReadySelectorTests(unittest.TestCase):
    def test_better_execution_quality_wins_ranking(self):
        good = contract()
        wide = contract(strike=34, bid=1.5, ask=2.0, oi=1000, volume=100)
        with patch.object(sc, "option_chain", return_value=chain(good, wide)):
            out = sc.pick_call(spot=35.0, now=NOW)
        self.assertTrue(out["contract_qualified"])
        self.assertEqual(out["strike"], 33.0)
        self.assertLessEqual(out["spread_pct"], out["preferences"]["preferred_max_spread_pct"])
        self.assertGreaterEqual(out["open_interest"], out["preferences"]["preferred_min_open_interest"])
        self.assertGreaterEqual(out["volume"], out["preferences"]["preferred_min_volume"])

    def test_wide_thin_contract_is_ranked_with_warnings_not_blocked(self):
        bad = contract(bid=2.55, ask=2.90, oi=38, volume=3)
        with patch.object(sc, "option_chain", return_value=chain(bad)):
            out = sc.pick_call(spot=34.83, now=NOW)
        self.assertTrue(out["contract_selected"])
        self.assertEqual(out["status"], "TOP_RANKED_CONTRACT")
        self.assertIn("wide_spread", out["warnings"])
        self.assertIn("low_open_interest", out["warnings"])
        self.assertIn("low_volume", out["warnings"])

    def test_delayed_or_truncated_chain_is_rejected_before_ranking(self):
        with patch.object(sc, "option_chain", return_value=chain(contract(), truncated=True)):
            out = sc.pick_call(spot=35.0, now=NOW)
        self.assertFalse(out["contract_qualified"])
        self.assertEqual(out["status"], "CHAIN_REJECTED")

    def test_stale_contract_quote_is_rejected(self):
        stale = contract(quote_ms=int(datetime(2026, 8, 26, 19, 59,
                                               tzinfo=timezone.utc).timestamp() * 1000))
        with patch.object(sc, "option_chain", return_value=chain(stale)):
            out = sc.pick_call(spot=35.0, now=NOW)
        self.assertFalse(out["contract_qualified"])
        self.assertEqual(out["status"], "NO_EXECUTABLE_CONTRACT")
        self.assertEqual(out["rejection_counts"]["stale_quote"], 1)

    def test_contract_horizon_is_a_preference_not_a_block(self):
        too_short = contract(strike=32.0, dte=49)
        lower = contract(strike=33.0, dte=50)
        upper = contract(strike=34.0, dte=75)
        too_long = contract(strike=35.0, dte=76)
        with patch.object(sc, "option_chain", return_value=chain(too_short, lower, upper, too_long)):
            out = sc.pick_call(spot=35.0, now=NOW)
        self.assertTrue(out["contract_qualified"])
        self.assertEqual(out["ranked_candidates"], 4)
        self.assertNotIn("dte_out_of_range", out["rejection_counts"])
        self.assertEqual(out["preferences"]["days_to_expiry"], [50, 75])

    def test_signal_controls_alert_not_selection(self):
        qualified = {"source": "schwab", "contract_selected": True,
                     "contract_qualified": True,
                     "chain_health": {"ok": True}, "thresholds": {}}
        with patch.object(sc, "pick_call", return_value=qualified):
            off = ts.schd_options(35.0, entry_signal_confirmed=False)
            on = ts.schd_options(35.0, entry_signal_confirmed=True)
        self.assertTrue(off["recommendable"])
        self.assertTrue(on["recommendable"])
        self.assertFalse(off["alert_eligible"])
        self.assertTrue(on["alert_eligible"])
        self.assertEqual(on["buy_ready_status"], "TOP-RANKED")

    def test_atm_iv_uses_actual_nearest_strike_not_only_qualified_rows(self):
        qualified_itm = contract(strike=33.0, delta=.70, volatility=15.0)
        unqualified_atm = contract(strike=35.0, delta=.50, volatility=22.0)
        with patch.object(sc, "option_chain", return_value=chain(qualified_itm, unqualified_atm)):
            out = sc.pick_call(spot=35.0, now=NOW)
        self.assertTrue(out["contract_qualified"])
        self.assertEqual(out["strike"], 33.0)
        self.assertEqual(out["atm_iv_pct"], 22.0)

    def test_puts_are_ranked_with_signed_delta(self):
        put = contract(delta=-.72)
        with patch.object(sc, "option_chain", return_value=chain(put)):
            out = sc.pick_put("TLT", spot=35.0, now=NOW)
        self.assertTrue(out["contract_selected"])
        self.assertEqual(out["side"], "PUT")
        self.assertEqual(out["delta"], -.72)


if __name__ == "__main__":
    unittest.main()
