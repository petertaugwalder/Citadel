"""Deterministic checks for the five flow fixes in ag_call_scanner. No network, no yfinance.

    python3 -m unittest trading/test_ag_call_scanner.py -v
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ag_call_scanner as s  # noqa: E402

D1 = date(2026, 9, 3)                       # Thursday
D2 = D1 + timedelta(days=1)                 # Friday
BASE = dict(CORN=19.0, WEAT=27.0, SOYB=26.5, CANE=11.0, DBA=27.5)        # flat daily closes
QUOTES = dict(CORN=19.60, WEAT=27.80, SOYB=27.30, CANE=11.35, DBA=28.30)  # live prints above the zones


def ny(day, hh, mm, ss=0):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=s.NY)


def flat_bars(close, rng=0.4, n=260, end=date(2026, 9, 2)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(s.Bar(d, close, close + rng / 2, close - rng / 2))
        d -= timedelta(days=1)
    return out[::-1]


class Mock:
    def __init__(self, closes):
        self.bars = {t: flat_bars(c) for t, c in closes.items()}
        self.price = dict(QUOTES)
        self.fail = set()
        self.calls = 0

    def set(self, **prices):
        self.price.update(prices)

    def history(self, t):
        if t in self.fail:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return self.bars[t]

    def quote(self, t):
        self.calls += 1
        if t in self.fail:
            raise RuntimeError("HTTP 429 Too Many Requests")
        p = self.price[t]
        return s.Quote(p, self.bars[t][-1].close, p - 0.1, p + 0.1)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def make(t, **kw):
    clock, mock, sounds = Clock(t), Mock(BASE), []
    return s.Scanner(mock, clock=clock, sound=sounds.append, **kw), mock, clock, sounds


def kinds(sc):
    return [k for _, k, _ in sc.alerts]


def row(frame, t):
    return next(l for l in frame.splitlines() if l.startswith(t + " "))


def plan_line(frame, t):
    lines = frame.splitlines()
    return next(l for l in lines[lines.index("PLAN"):] if l.startswith(t + " "))


class Fix1FailuresVisible(unittest.TestCase):
    def test_failed_tickers_keep_a_row_and_the_header_counts_them(self):
        sc, mock, clock, _ = make(ny(D1, 12, 33, 7))
        mock.fail = {"SOYB", "CANE", "DBA"}
        frame = sc.tick()
        self.assertIn("data 2/5", frame)
        self.assertIn("DATA  fetch failed SOYB CANE DBA", frame)
        self.assertIn("last error 12:33:07", frame)
        for t in ("SOYB", "CANE", "DBA"):
            self.assertIn("NO DATA ×1 RuntimeError: HTTP 429", row(frame, t))
        self.assertIn("WATCH", row(frame, "CORN"))
        mock.fail = set()
        clock.advance(seconds=60)
        frame = sc.tick()
        self.assertIn("data 5/5", frame)
        self.assertNotIn("NO DATA", frame)
        self.assertFalse(any(l.startswith("DATA") for l in frame.splitlines()))

    def test_failure_after_success_keeps_last_good_quote_and_shows_error(self):
        sc, mock, clock, _ = make(ny(D1, 12, 33, 7))
        sc.tick()
        clock.advance(seconds=60)
        mock.fail = {"DBA"}
        frame = sc.tick()
        dba = row(frame, "DBA")
        self.assertIn("28.30", dba)                       # last good price stays on screen
        self.assertIn("fetch failed DBA", frame)
        self.assertIn("age 1m", frame)


class Fix2StaleData(unittest.TestCase):
    def test_wall_clock_gap_polls_immediately_and_flags_stale_rows(self):
        sc, mock, clock, _ = make(ny(D1, 12, 37, 9))
        frame = sc.tick()
        self.assertIn("next 60s", frame)
        n = mock.calls
        clock.advance(hours=18, minutes=27, seconds=44)    # resumes 07:04:53 next morning
        mock.fail = set(BASE)                              # and Yahoo is not answering yet
        frame = sc.tick()
        self.assertGreater(mock.calls, n)                  # no waiting out the old 57s countdown
        self.assertIn("INFO", kinds(sc))
        self.assertTrue(any("Resumed after 18h gap" in m for _, _, m in sc.alerts))
        self.assertIn("CLOSED", frame)
        self.assertIn("data 0/5 · age 18h", frame)
        self.assertIn("STALE 18h · RuntimeError: HTTP 429", row(frame, "CORN"))
        self.assertIn("stale CORN WEAT SOYB CANE DBA", frame)
        mock.fail = set()
        clock.advance(seconds=60)
        frame = sc.tick()
        self.assertNotIn("STALE", frame)
        self.assertIn("data 5/5 · age 0s", frame)

    def test_countdown_is_time_based(self):
        sc, mock, clock, _ = make(ny(D1, 12, 0, 0))
        sc.tick()
        clock.advance(seconds=37)
        self.assertIn("next 23s", sc.tick())
        clock.advance(seconds=23)
        n = mock.calls
        self.assertIn("next 60s", sc.tick())
        self.assertGreater(mock.calls, n)


class Fix3PullbackAlert(unittest.TestCase):
    def test_zone_entry_fires_once_then_rearms_after_a_bounce(self):
        sc, mock, clock, sounds = make(ny(D1, 10, 0))
        mock.set(CORN=19.80)
        frame = sc.tick()
        self.assertIn("WATCH", row(frame, "CORN"))
        self.assertNotIn("BUY-PULLBACK", kinds(sc))
        clock.advance(seconds=60)
        mock.set(CORN=19.02)                               # tags the 20-EMA from above
        frame = sc.tick()
        self.assertIn(">> IN ZONE <<", row(frame, "CORN"))
        self.assertEqual(kinds(sc).count("BUY-PULLBACK"), 1)
        self.assertEqual(sounds, ["BUY-PULLBACK"])
        self.assertIn("CORN 19.02 in zone", sc.alerts[-1][2])
        self.assertIn("pullback to ema20", sc.alerts[-1][2])
        clock.advance(seconds=60)
        mock.set(CORN=19.01)                               # still inside: no repeat
        sc.tick()
        self.assertEqual(kinds(sc).count("BUY-PULLBACK"), 1)
        clock.advance(seconds=60)
        mock.set(CORN=19.30)                               # bounce out above the zone: re-arms
        sc.tick()
        clock.advance(seconds=60)
        mock.set(CORN=19.02)
        sc.tick()
        self.assertEqual(kinds(sc).count("BUY-PULLBACK"), 2)
        self.assertIn("[cooldown]", sc.alerts[-1][2])       # inside 25m: logged, not sounded
        self.assertEqual(len(sounds), 1)
        clock.advance(minutes=30)
        mock.set(CORN=19.30)
        sc.tick()
        clock.advance(seconds=60)
        mock.set(CORN=19.02)
        sc.tick()
        self.assertEqual(len(sounds), 2)

    def test_no_pullback_alert_out_of_session_or_from_below(self):
        sc, mock, clock, sounds = make(ny(D1, 7, 4, 53))
        mock.set(CORN=19.02)
        sc.tick()
        self.assertNotIn("BUY-PULLBACK", kinds(sc))         # pre-market: watch only
        sc, mock, clock, sounds = make(ny(D1, 10, 0))
        mock.set(CORN=18.50)                               # starts below the stop
        frame = sc.tick()
        self.assertIn("BELOW STOP", row(frame, "CORN"))
        clock.advance(seconds=60)
        mock.set(CORN=19.02)                               # reclaim from below is not a pullback
        sc.tick()
        self.assertNotIn("BUY-PULLBACK", kinds(sc))


class Fix4StopBuffer(unittest.TestCase):
    def test_dynamic_stop_sits_half_atr_under_the_average(self):
        sc, mock, clock, _ = make(ny(D1, 10, 0))
        mock.set(CORN=19.80)
        frame = sc.tick()
        st = sc.states["CORN"]
        lv = sc.levels_for(st, clock())
        ind = s.compute_indicators(st.bars, st.quote, D1, True)
        self.assertLess(lv.stop, lv.entry)
        self.assertAlmostEqual(lv.stop, ind.ema20 - 0.5 * ind.atr14, places=9)
        self.assertGreater(lv.risk_pct, 0.5)
        self.assertRegex(row(frame, "CORN"), r"\d+\.\d\d > \d+\.\d\d [+-]\d+\.\d\d% r\d+\.\d%")
        self.assertIn("stop 18.", plan_line(frame, "CORN"))
        self.assertIn("(ema20-0.5atr)", plan_line(frame, "CORN"))
        self.assertIn("entry 19.08(ema20^)", plan_line(frame, "CORN"))

    def test_fixed_entry_under_a_rising_average_is_flagged_and_not_traded(self):
        sc, mock, clock, sounds = make(ny(D1, 10, 0))
        mock.set(WEAT=27.0)                                # fixed 26.40 entry, 20-EMA near 27
        frame = sc.tick()
        weat = row(frame, "WEAT")
        self.assertIn("26.40 > 26.", weat)
        self.assertIn("! stop>=entry", weat)
        self.assertIn("INVERTED", weat)
        self.assertIn("! stop>=entry", plan_line(frame, "WEAT"))
        clock.advance(seconds=60)
        mock.set(WEAT=26.45)                               # would be "in zone" if the zone were valid
        sc.tick()
        self.assertNotIn("BUY-PULLBACK", kinds(sc))
        self.assertEqual(sounds, [])


class Fix5DatesAndHysteresis(unittest.TestCase):
    def test_breakout_carries_a_date_rearms_on_hysteresis_and_ages_out_next_day(self):
        with tempfile.TemporaryDirectory() as d:
            sc, mock, clock, sounds = make(ny(D1, 9, 49, 53), state_dir=d)
            mock.set(DBA=28.80)
            frame = sc.tick()
            self.assertIn("28.80 HIT", row(frame, "DBA"))
            self.assertIn(">> BREAKOUT <<", row(frame, "DBA"))
            self.assertIn("  09-03 09:49:53 NY  BUY-BREAKOUT DBA 28.80 >= 28.80 — breakout trigger.", frame)
            self.assertEqual(sounds, ["BUY-BREAKOUT"])
            clock.advance(seconds=60)
            mock.set(DBA=28.75)                            # 0.17% wiggle under the trigger
            sc.tick()
            clock.advance(seconds=60)
            mock.set(DBA=28.81)
            sc.tick()
            self.assertEqual(kinds(sc).count("BUY-BREAKOUT"), 1)   # no re-fire on the re-cross
            clock.advance(seconds=60)
            mock.set(DBA=28.60)                            # more than 0.5% under: re-arms
            sc.tick()
            clock.advance(seconds=60)
            mock.set(DBA=28.81)
            sc.tick()
            self.assertEqual(kinds(sc).count("BUY-BREAKOUT"), 2)
            self.assertIn("[cooldown]", sc.alerts[-1][2])
            self.assertEqual(len(sounds), 1)

            clock.t = ny(D2, 7, 4, 53)                     # next morning, pre-market
            mock.set(DBA=29.09)
            frame = sc.tick()
            self.assertIn("CLOSED", frame)
            self.assertIn("ABOVE TRIG · fired 09-03", row(frame, "DBA"))
            self.assertNotIn(">> BREAKOUT <<", frame)
            self.assertEqual(kinds(sc).count("BUY-BREAKOUT"), 2)
            self.assertTrue(any("Market closed" in m for _, k, m in sc.alerts if k == "CHIME"))

            with open(os.path.join(d, "alerts.log"), encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertTrue(lines and all(l.startswith("2026-09-0") for l in lines))
            self.assertTrue(any("BUY-BREAKOUT DBA 28.80 >= 28.80" in l for l in lines))

            # a restart in the next session must not re-fire yesterday's breakout
            sc2 = s.Scanner(mock, clock=clock, sound=sounds.append, state_dir=d)
            clock.t = ny(D2, 10, 0)
            frame = sc2.tick()
            self.assertIn("ABOVE TRIG · fired 09-03", row(frame, "DBA"))
            self.assertFalse(any(k == "BUY-BREAKOUT" and dt.date() == D2 for dt, k, _ in sc2.alerts))
            self.assertIn("09-03 09:49:53 NY  BUY-BREAKOUT", frame)   # history reloaded with dates

    def test_open_and_close_chimes(self):
        sc, mock, clock, sounds = make(ny(D1, 9, 29, 59))
        sc.tick()
        self.assertEqual(kinds(sc), [])
        clock.advance(seconds=2)
        sc.tick()
        self.assertEqual(kinds(sc)[-1], "CHIME")
        self.assertIn("Market open", sc.alerts[-1][2])
        self.assertEqual(sounds, ["CHIME"])
        clock.t = ny(D1, 16, 0, 1)
        sc.tick()
        self.assertIn("Market closed", sc.alerts[-1][2])


if __name__ == "__main__":
    unittest.main()
