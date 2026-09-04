"""Tests for natgas_fix.py. Run: python3 -m unittest test_natgas_fix -v"""
import os
import sys
import unittest
from datetime import date, datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import natgas_fix as nf  # noqa: E402
from natgas_fix import ET, UTC  # noqa: E402


def et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def ms(dt):
    return int(dt.timestamp() * 1000)


class CalendarTests(unittest.TestCase):
    def test_holidays_2026(self):
        h = nf.cme_energy_holidays(2026)
        for d in [date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
                  date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)]:
            self.assertIn(d, h, d)
        self.assertEqual(len(h), 10)

    def test_holiday_observance_rules(self):
        h27 = nf.cme_energy_holidays(2027)
        self.assertIn(date(2027, 12, 24), h27)   # Christmas on Saturday -> Friday
        self.assertIn(date(2027, 6, 18), h27)    # Juneteenth on Saturday -> Friday
        self.assertIn(date(2027, 7, 5), h27)     # July 4 on Sunday -> Monday
        self.assertTrue(nf.is_business_day(date(2027, 12, 31)))  # Jan 1 2028 is Saturday: no Friday observance
        self.assertNotIn(date(2028, 1, 1), nf.cme_energy_holidays(2028))

    def test_ng_expiry(self):
        self.assertEqual(nf.ng_expiry(2026, 10), date(2026, 9, 28))
        self.assertEqual(nf.ng_expiry(2026, 11), date(2026, 10, 28))
        self.assertEqual(nf.ng_expiry(2027, 1), date(2026, 12, 29))
        self.assertEqual(nf.ng_expiry(2026, 9), date(2026, 8, 27))
        self.assertEqual(nf.ng_expiry(2026, 2), date(2026, 1, 28))
        self.assertEqual(nf.ng_expiry(2025, 12), date(2025, 11, 25))  # skips Thanksgiving
        self.assertEqual(nf.ng_expiry_for("/NGV26"), date(2026, 9, 28))

    def test_contract_from_symbol(self):
        self.assertEqual(nf.contract_from_symbol("/NGV26"), (2026, 10))
        self.assertEqual(nf.contract_from_symbol("NGF27"), (2027, 1))
        self.assertEqual(nf.contract_from_symbol("/NGH27"), (2027, 3))
        with self.assertRaises(ValueError):
            nf.contract_from_symbol("UNG")

    def test_days_to_expiry_excludes_labor_day(self):
        left = nf.days_to_expiry(date(2026, 9, 2), date(2026, 9, 28))
        self.assertEqual((left.weekdays, left.trading_days, left.holidays), (18, 17, [date(2026, 9, 7)]))
        self.assertEqual(left.render(), "17 trading days left (18 weekdays, 09-07 excluded)")
        self.assertEqual(nf.days_to_expiry(date(2026, 9, 21), date(2026, 9, 28)).render(), "5 trading days left")

    def test_business_day_of_month(self):
        self.assertEqual(nf.business_day_of_month(date(2026, 9, 2)), 2)
        self.assertEqual(nf.business_day_of_month(date(2026, 9, 6)), 4)   # Sunday: count so far
        self.assertEqual(nf.business_day_of_month(date(2026, 9, 7)), 4)   # Labor Day
        self.assertEqual(nf.business_day_of_month(date(2026, 9, 8)), 5)
        self.assertEqual(nf.business_day_of_month(date(2026, 9, 14)), 9)
        self.assertEqual(nf.business_day_of_month(date(2026, 9, 15)), 10)

    def test_sessions_between(self):
        self.assertEqual(nf.sessions_between(date(2026, 9, 1), date(2026, 9, 2)), 1)
        self.assertEqual(nf.sessions_between(date(2026, 9, 4), date(2026, 9, 8)), 1)  # weekend + Labor Day
        self.assertEqual(nf.sessions_between(date(2026, 9, 2), date(2026, 9, 2)), 0)
        self.assertEqual(nf.sessions_between(date(2026, 9, 3), date(2026, 9, 2)), 0)


class SessionTests(unittest.TestCase):
    def test_globex_trade_date(self):
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 2, 18, 55)), date(2026, 9, 3))
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 2, 16, 30)), date(2026, 9, 2))
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 2, 17, 30)), date(2026, 9, 2))
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 3, 2, 0)), date(2026, 9, 3))
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 4, 19, 0)), date(2026, 9, 8))   # Fri night, Mon holiday
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 5, 12, 0)), date(2026, 9, 8))   # Saturday
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 6, 19, 0)), date(2026, 9, 8))   # Sunday open
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 7, 12, 0)), date(2026, 9, 8))   # Labor Day session
        self.assertEqual(nf.globex_trade_date(et(2026, 9, 10, 19, 0)), date(2026, 9, 11))

    def test_globex_session(self):
        self.assertEqual(nf.globex_session(et(2026, 9, 2, 18, 55)), "OPEN")
        self.assertEqual(nf.globex_session(et(2026, 9, 2, 17, 30)), "BREAK")
        self.assertEqual(nf.globex_session(et(2026, 9, 4, 17, 30)), "WEEKEND")
        self.assertEqual(nf.globex_session(et(2026, 9, 5, 12, 0)), "WEEKEND")
        self.assertEqual(nf.globex_session(et(2026, 9, 6, 17, 0)), "WEEKEND")
        self.assertEqual(nf.globex_session(et(2026, 9, 6, 18, 30)), "OPEN")

    def test_ny_session(self):
        self.assertEqual(nf.ny_session(et(2026, 9, 2, 18, 55)), "AFTERHOURS")
        self.assertEqual(nf.ny_session(et(2026, 9, 2, 10, 0)), "RTH")
        self.assertEqual(nf.ny_session(et(2026, 9, 2, 8, 0)), "PRE")
        self.assertEqual(nf.ny_session(et(2026, 9, 2, 21, 0)), "CLOSED")
        self.assertEqual(nf.ny_session(et(2026, 9, 7, 10, 0)), "CLOSED")
        self.assertEqual(nf.ny_session(et(2026, 9, 5, 10, 0)), "CLOSED")

    def test_last_settled_session(self):
        self.assertEqual(nf.last_settled_session(et(2026, 9, 2, 18, 55)), date(2026, 9, 2))
        self.assertEqual(nf.last_settled_session(et(2026, 9, 2, 14, 0)), date(2026, 9, 1))
        self.assertEqual(nf.last_settled_session(et(2026, 9, 2, 14, 45)), date(2026, 9, 2))
        self.assertEqual(nf.last_settled_session(et(2026, 9, 5, 12, 0)), date(2026, 9, 4))
        self.assertEqual(nf.last_settled_session(et(2026, 9, 7, 12, 0)), date(2026, 9, 4))
        self.assertEqual(nf.last_settled_session(et(2026, 9, 8, 9, 0)), date(2026, 9, 4))

    def test_session_header(self):
        self.assertEqual(nf.session_header(nf.DEMO_NOW),
                         "09-02 18:55 ET (22:55 UTC) · NY AFTERHOURS · GLOBEX OPEN, trade date 09-03 · ETF prints = 09-02 close")
        self.assertIn("GLOBEX BREAK", nf.session_header(et(2026, 9, 2, 17, 30)))


class ClockTests(unittest.TestCase):
    def test_stamp(self):
        self.assertEqual(nf.stamp(datetime(2026, 9, 2, 22, 55, tzinfo=UTC)), "09-02 18:55 ET (22:55 UTC)")
        self.assertEqual(nf.stamp(datetime(2026, 9, 2, 22, 55)), "09-02 18:55 ET (22:55 UTC)")  # naive = UTC
        self.assertEqual(nf.stamp(datetime(2026, 9, 3, 1, 5, tzinfo=UTC)), "09-02 21:05 ET (09-03 01:05 UTC)")
        self.assertEqual(nf.stamp(et(2026, 9, 2, 17, 38), utc=False), "09-02 17:38 ET")
        self.assertEqual(nf.stamp_date(nf.DEMO_NOW), "2026-09-02")

    def test_epoch_to_et(self):
        target = et(2026, 9, 2, 17, 38)
        self.assertEqual(nf._epoch_to_et(ms(target)), target)
        self.assertEqual(nf._epoch_to_et(int(target.timestamp())), target)
        self.assertEqual(nf._epoch_to_et("2026-09-02T21:38:00Z"), target)
        self.assertIsNone(nf._epoch_to_et(None))
        self.assertIsNone(nf._epoch_to_et(0))
        self.assertIsNone(nf._epoch_to_et("garbage"))

    def test_fallback_tz_matches_us_rules(self):
        fb = nf._USEasternFallback()
        self.assertEqual(fb.utcoffset(datetime(2026, 7, 1, 12)), timedelta(hours=-4))
        self.assertEqual(fb.utcoffset(datetime(2026, 1, 15, 12)), timedelta(hours=-5))
        self.assertEqual(fb.utcoffset(datetime(2026, 3, 8, 1, 59)), timedelta(hours=-5))
        self.assertEqual(fb.utcoffset(datetime(2026, 3, 8, 2, 0)), timedelta(hours=-4))
        self.assertEqual(fb.utcoffset(datetime(2026, 11, 1, 1, 59)), timedelta(hours=-4))
        self.assertEqual(fb.utcoffset(datetime(2026, 11, 1, 2, 0)), timedelta(hours=-5))
        self.assertEqual(fb.tzname(datetime(2026, 9, 2, 18)), "EDT")


class SettleTests(unittest.TestCase):
    CONTRACT = (2026, 10)

    def payload(self, **quote):
        q = dict(nf.DEMO_NGV26["quote"])
        q.update(quote)
        return {"quote": q, "reference": dict(nf.DEMO_NGV26["reference"])}

    def test_demo_run_derives_same_day_settle_from_index(self):
        s = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes=nf.DEMO_INDEX)
        self.assertEqual((s.price, s.session, s.stale_sessions), (2.956, date(2026, 9, 2), 0))
        self.assertEqual(s.source, "derived:$DJCING/$SPGSNG")
        self.assertEqual(s.basis, "day")
        self.assertEqual(s.schwab_close, 2.904)
        self.assertAlmostEqual(nf.change(3.009, s.price)[0], 0.053, places=6)
        self.assertAlmostEqual(nf.change(3.009, s.price)[1], 1.79, places=2)

    def test_same_day_settle_time_uses_schwab_field(self):
        p = self.payload(settleTime=ms(et(2026, 9, 2, 15, 5)))
        p["reference"]["futureSettlementPrice"] = 2.956
        s = nf.resolve_settle(p, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes=nf.DEMO_INDEX)
        self.assertEqual((s.price, s.session, s.stale_sessions, s.source), (2.956, date(2026, 9, 2), 0, "schwab:futureSettlementPrice"))

    def test_settle_time_from_previous_session_without_index(self):
        p = self.payload(settleTime=ms(et(2026, 9, 1, 15, 5)))
        s = nf.resolve_settle(p, nf.DEMO_NOW, contract=self.CONTRACT)
        self.assertEqual((s.price, s.session, s.stale_sessions), (2.904, date(2026, 9, 1), 1))
        self.assertEqual(s.basis, "2-session")

    def test_stale_despite_same_day_stamp_when_close_unchanged(self):
        p = self.payload(settleTime=ms(et(2026, 9, 2, 17, 38)))  # the stamp the tracker printed
        s = nf.resolve_settle(p, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes=nf.DEMO_INDEX, prev_close_seen=2.904)
        self.assertEqual((s.price, s.stale_sessions), (2.956, 0))
        self.assertTrue(any("claims same-day" in n for n in s.notes))

    def test_known_override_wins(self):
        s = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=self.CONTRACT, known={date(2026, 9, 2): 2.956})
        self.assertEqual((s.price, s.source, s.stale_sessions), (2.956, "known", 0))

    def test_no_evidence_same_evening_assumes_one_session_behind(self):
        s = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=self.CONTRACT)
        self.assertEqual((s.price, s.session, s.stale_sessions), (2.904, date(2026, 9, 1), 1))
        self.assertTrue(any("ASSUMED" in n for n in s.notes))

    def test_no_evidence_next_morning_takes_field_as_current(self):
        s = nf.resolve_settle(nf.DEMO_NGV26, et(2026, 9, 3, 10, 0), contract=self.CONTRACT)
        self.assertEqual((s.price, s.session, s.stale_sessions), (2.904, date(2026, 9, 2), 0))

    def test_index_mid_roll_cannot_derive(self):
        s = nf.resolve_settle(nf.DEMO_NGV26, et(2026, 9, 9, 18, 55), contract=self.CONTRACT, index_quotes=nf.DEMO_INDEX)
        self.assertEqual(s.stale_sessions, 1)
        self.assertTrue(any("rolling" in n for n in s.notes))

    def test_index_post_roll_derives_second_month_only(self):
        now = et(2026, 9, 16, 18, 55)
        nov = {"quote": {"lastPrice": 3.150, "closePrice": 3.034}, "reference": {}}
        s = nf.resolve_settle(nov, now, contract=(2026, 11), index_quotes={"$SPGSNG": 1.79})
        self.assertEqual((s.price, s.stale_sessions, s.source), (3.088, 0, "derived:$SPGSNG"))
        s2 = nf.resolve_settle(nf.DEMO_NGV26, now, contract=self.CONTRACT, index_quotes={"$SPGSNG": 1.79})
        self.assertEqual(s2.stale_sessions, 1)
        self.assertTrue(any("holds 2026-11" in n for n in s2.notes))

    def test_index_live_mode_by_timestamp(self):
        idx = {"$SPGSNG": {"netPercentChange": 2.5, "tradeTime": ms(et(2026, 9, 2, 18, 50))}}
        s = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes=idx)
        self.assertEqual(s.price, 2.936)   # 3.009 / 1.025
        idx_close = {"$SPGSNG": {"netPercentChange": 2.5, "tradeTime": ms(et(2026, 9, 2, 14, 31))}}
        s = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes=idx_close)
        self.assertEqual(s.price, 2.977)   # 2.904 * 1.025

    def test_index_that_merely_mirrors_the_stale_move_is_refused(self):
        s = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes={"$DJCING": 3.62})
        self.assertEqual((s.price, s.stale_sessions), (2.904, 1))
        self.assertTrue(any("as stale as closePrice" in n for n in s.notes))

    def test_disagreeing_indexes_are_refused(self):
        s = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes={"$DJCING": 1.79, "$SPGSNG": 2.5})
        self.assertEqual((s.price, s.stale_sessions), (2.904, 1))
        self.assertTrue(any("disagree" in n for n in s.notes))

    def test_index_stamped_before_settlement_is_refused(self):
        idx = {"$SPGSNG": {"netPercentChange": 1.79, "tradeTime": ms(et(2026, 9, 1, 15, 0))}}
        s = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes=idx)
        self.assertEqual(s.stale_sessions, 1)
        self.assertTrue(any("predates" in n for n in s.notes))

    def test_bare_quote_dict_and_missing_fields(self):
        s = nf.resolve_settle({"lastPrice": 3.009, "closePrice": 2.904}, nf.DEMO_NOW, contract=self.CONTRACT, index_quotes={"$DJCING": 1.79})
        self.assertEqual(s.price, 2.956)
        s = nf.resolve_settle({"lastPrice": 3.009}, nf.DEMO_NOW, contract=self.CONTRACT)
        self.assertEqual((s.price, s.source), (None, "none"))
        self.assertEqual(s.label(), "settle unavailable")

    def test_field_report(self):
        line = nf.settle_field_report(nf.DEMO_NGV26, nf.DEMO_NOW)
        self.assertIn("base 2.904", line)
        self.assertIn("futurePercentChange +3.62%", line)
        self.assertTrue(line.startswith("09-02 18:55 ET"))


class CaptureTests(unittest.TestCase):
    def test_ung_and_boil_against_settle_to_settle(self):
        ung = nf.capture(2.17, 1.79, 1.0)
        self.assertAlmostEqual(ung.raw, 1.212, places=3)
        self.assertAlmostEqual(ung.of_target, 1.212, places=3)
        boil = nf.capture(3.94, 1.79, 2.0)
        self.assertAlmostEqual(boil.raw, 2.201, places=3)
        self.assertAlmostEqual(boil.of_target, 1.101, places=3)
        self.assertIn("2.20x of NG (target 2.0x) = 1.10 of target", boil.render())

    def test_flat_day_has_no_capture(self):
        c = nf.capture(0.5, 0.1)
        self.assertIsNone(c.raw)
        self.assertIn("n/a", c.render())

    def test_render_vehicle(self):
        line = nf.render_vehicle("BOIL", 21.39, 0.81, None, 1.79, 2.0)
        self.assertTrue(line.startswith("BOIL  21.39  +0.81 (+3.94%)"))
        self.assertIn("2.20x of NG (target 2.0x) = 1.10 of target", line)


class FormattingTests(unittest.TestCase):
    def test_ordinal(self):
        expected = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th", 21: "21st",
                    22: "22nd", 42: "42nd", 101: "101st", 111: "111th", 112: "112th", 0: "0th"}
        for n, s in expected.items():
            self.assertEqual(nf.ordinal(n), s)
        self.assertEqual(nf.ordinal(41.7), "42nd")

    def test_rank_pctile(self):
        hist = [0.40] * 10 + [0.42] * 14
        self.assertAlmostEqual(nf.rank_pctile(0.416, hist), 41.67, places=2)
        self.assertEqual(nf.rank_pctile(0.1, []), 0.0)

    def test_range_position(self):
        self.assertTrue(nf.range_position(0.124, 0.129, 0.246, 0, 24).startswith("NEW LOW, below its 24-row range"))
        self.assertTrue(nf.range_position(0.416, 0.394, 0.483, 42, 24).startswith("42nd rank-pctile"))
        self.assertTrue(nf.range_position(0.459, 0.450, 0.689, 4, 24).startswith("at the BOTTOM"))
        self.assertTrue(nf.range_position(0.300, 0.100, 0.200, 100, 24).startswith("NEW HIGH"))
        self.assertTrue(nf.range_position(0.200, 0.100, 0.200, 100, 24).startswith("at the TOP"))

    def test_fmt_change(self):
        self.assertEqual(nf.fmt_change(3.009, 2.956), "+0.053 (+1.79%, day)")
        self.assertEqual(nf.fmt_change(3.009, 2.904, "2-session"), "+0.105 (+3.62%, 2-session)")


class RenderTests(unittest.TestCase):
    def test_headline(self):
        settle = nf.resolve_settle(nf.DEMO_NGV26, nf.DEMO_NOW, contract=(2026, 10), index_quotes=nf.DEMO_INDEX)
        lines = nf.render_headline("/NGV26", 3.009, settle, 326958, nf.DEMO_NOW)
        self.assertIn("+0.053 (+1.79%, day)", lines[0])
        self.assertIn("vs 09-02 settle 2.956", lines[0])
        self.assertIn("OI 326,958", lines[0])
        self.assertIn("+0.105 (+3.62%, 2-session)", lines[1])
        self.assertIn("expires 2026-09-28 · 17 trading days left", lines[2])

    def test_demo_runs(self):
        out = nf.demo()
        self.assertIn("2.956", out)
        self.assertIn("capture 1.21x of NG (target 1.0x) = 1.21 of target", out)
        self.assertIn("capture 2.20x of NG (target 2.0x) = 1.10 of target", out)


if __name__ == "__main__":
    unittest.main()
