#!/usr/bin/env python3
"""AG CALL SCANNER -- CORN WEAT SOYB CANE DBA

Live terminal scanner for ag-ETF call entries: breakout triggers plus
pullback-to-moving-average buy zones, with in-session sound alerts.

    python3 trading/ag_call_scanner.py             live loop (yfinance)
    python3 trading/ag_call_scanner.py --once      one poll, print, exit
    python3 trading/ag_call_scanner.py --no-sound

Runtime files (in --state-dir, default: this file's directory):
    alerts.log           every alert, dated, newest last
    scanner_state.json   armed/fired flags, so a restart does not re-fire old signals

The five fixes from the flow review are tagged FIX 1..5 in the code:
    FIX 1  fetch failures are visible: every ticker always gets a row, errors on screen
    FIX 2  stale data: quote age shown, STALE flag, a wall-clock gap over one poll forces an immediate re-poll
    FIX 3  BUY-PULLBACK alert when price enters a buy zone from above
    FIX 4  dynamic-MA stops sit 0.5 ATR under the average; inverted zones are flagged, not traded
    FIX 5  alerts and states carry dates; hysteresis re-arm instead of re-firing on every re-cross
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
TICKERS = ["CORN", "WEAT", "SOYB", "CANE", "DBA"]

POLL_S = 60
COOLDOWN_MIN = 25
SESSION_OPEN = dtime(9, 30)
SESSION_CLOSE = dtime(16, 0)

STOP_BUFFER_ATR = 0.5   # FIX 4: dynamic-MA stop = average - 0.5 * ATR(14); per-ticker "stop_buffer_atr" overrides
ZONE_TOL_PCT = 0.25     # FIX 3: a tag up to 0.25% above the entry still counts as in-zone
REARM_PCT = 0.5         # FIX 5: price must move 0.5% back through a level before that alert re-arms
KEEP_ALERTS = 8
WIDTH = 100

# entry/stop: a number is a fixed level; "ema20" / "sma50" / "sma200" track live.
# A trailing "^" on the entry is display-only: buy the pullback from above.
PLAN: Dict[str, dict] = {
    "CORN": dict(entry="ema20^", stop="ema20", trig=None,
                 notes="price discovery · most stretched (RSI ~79) · close <19.20 = failed breakout · golden cross pending"),
    "WEAT": dict(entry=26.40, stop="ema20", trig=None,
                 notes="double-top breakout · measured move ~28.5-29 · trail = 20-EMA"),
    "SOYB": dict(entry="ema20^", stop="ema20", trig=None,
                 notes="orderly staircase to new highs · least fragile"),
    "CANE": dict(entry="ema20", stop="sma200", trig=11.50,
                 notes="11.0-11.4 flag above reclaimed 200-day · >11.50 resumes trend · India-ban/ethanol trade"),
    "DBA": dict(entry="ema20", stop="sma50", trig=28.80,
                notes="sector tell · 28.80 = May-high trigger · most room to run (RSI 65)"),
}


# ----------------------------------------------------------------------------- data


@dataclass
class Bar:
    day: date
    close: float
    high: float
    low: float


@dataclass
class Quote:
    last: float
    prev_close: float
    day_low: float
    day_high: float


@dataclass
class Indicators:
    ema20: float
    sma50: float
    sma200: float
    atr14: float


@dataclass
class Levels:
    entry: float
    entry_src: str
    stop: float
    stop_src: str
    trig: Optional[float]

    @property
    def inverted(self) -> bool:          # FIX 4: stop at or above entry means no tradeable zone
        return self.stop >= self.entry

    @property
    def risk_pct(self) -> float:
        return (self.entry - self.stop) / self.entry * 100.0

    @property
    def zone_hi(self) -> float:          # FIX 3: top of the buy zone, entry plus a small tag tolerance
        return self.entry * (1.0 + ZONE_TOL_PCT / 100.0)

    def in_zone(self, price: float) -> bool:
        return (not self.inverted) and self.stop < price <= self.zone_hi


@dataclass
class TickerState:
    ticker: str
    bars: List[Bar] = field(default_factory=list)
    hist_day: Optional[date] = None
    hist_error: Optional[str] = None
    quote: Optional[Quote] = None        # last GOOD quote, kept across failures (FIX 1)
    ok_at: Optional[datetime] = None     # when that quote arrived (FIX 2)
    error: Optional[str] = None          # last fetch error, cleared on success (FIX 1)
    error_at: Optional[datetime] = None
    fail_count: int = 0                  # consecutive failures
    # persisted in scanner_state.json
    breakout_armed: bool = True
    breakout_fired_at: Optional[datetime] = None
    pullback_armed: bool = True
    pullback_fired_at: Optional[datetime] = None
    last_alert_at: Dict[str, datetime] = field(default_factory=dict)


def now_ny() -> datetime:
    return datetime.now(NY)


def fmt_age(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def short_err(e: BaseException) -> str:
    return " ".join(f"{type(e).__name__}: {e}".split())[:70]


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


# ----------------------------------------------------------------------------- indicators


def ema(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1.0 - k)
    return e


def sma(values: List[float], n: int) -> Optional[float]:
    if not values:
        return None
    w = values[-n:]
    return sum(w) / len(w)


def atr(bars: List[Bar], n: int = 14) -> Optional[float]:
    if len(bars) < 2:
        return None
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(bars, bars[1:])]
    if len(trs) < n:
        return sum(trs) / len(trs)
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def compute_indicators(bars: List[Bar], quote: Optional[Quote], today: date,
                       session_started_today: bool) -> Optional[Indicators]:
    """Daily bars with the live quote folded into today's bar, so the averages track live."""
    if not bars:
        return None
    work = list(bars)
    if quote is not None:
        last = work[-1]
        if last.day == today:
            work[-1] = Bar(today, quote.last, max(last.high, quote.day_high), min(last.low, quote.day_low))
        elif session_started_today and today > last.day:
            work.append(Bar(today, quote.last, quote.day_high, quote.day_low))
    closes = [b.close for b in work]
    e20, s50, s200, a14 = ema(closes, 20), sma(closes, 50), sma(closes, 200), atr(work, 14)
    if e20 is None or s50 is None or s200 is None or a14 is None:
        return None
    return Indicators(e20, s50, s200, a14)


def resolve_levels(plan: dict, ind: Indicators) -> Levels:
    buf = float(plan.get("stop_buffer_atr", STOP_BUFFER_ATR))

    def level(spec, is_stop: bool) -> Tuple[float, str]:
        if isinstance(spec, (int, float)):
            return float(spec), ""
        name = str(spec).rstrip("^")
        val = getattr(ind, name, None)
        if val is None:
            raise ValueError(f"unknown level {spec!r}")
        if is_stop and buf > 0:              # FIX 4: never park the stop exactly on the average
            return val - buf * ind.atr14, f"{name}-{buf:g}atr"
        return val, str(spec)

    entry, entry_src = level(plan["entry"], False)
    stop, stop_src = level(plan["stop"], True)
    trig = plan.get("trig")
    return Levels(entry, entry_src, stop, stop_src, None if trig is None else float(trig))


def classify(price: float, lv: Levels, st: TickerState, now: datetime) -> str:
    if lv.trig is not None and price >= lv.trig:
        fired = st.breakout_fired_at
        if fired is not None and fired.date() == now.date():
            return ">> BREAKOUT <<"
        if fired is not None:                # FIX 5: an old event is history, not a live signal
            return f"ABOVE TRIG · fired {fired:%m-%d}"
        return "ABOVE TRIG"
    if lv.inverted:
        return "INVERTED"
    if price < lv.stop:
        return "BELOW STOP"
    if lv.in_zone(price):
        fired = st.pullback_fired_at
        if fired is not None and fired.date() == now.date():
            return ">> IN ZONE <<"
        return "IN ZONE"
    return "WATCH"


# ----------------------------------------------------------------------------- providers


class YFProvider:
    """yfinance-backed quotes; one request per ticker so one failure cannot hide the others."""

    def __init__(self):
        import yfinance as yf  # imported lazily so tests run without it
        self.yf = yf

    def history(self, ticker: str) -> List[Bar]:
        df = self.yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=False)
        if df is None or len(df) == 0:
            raise RuntimeError("empty history")
        bars = []
        for idx, r in df.iterrows():
            c, h, l = _num(r["Close"]), _num(r["High"]), _num(r["Low"])
            if c is None or h is None or l is None:
                continue
            bars.append(Bar(idx.date(), c, h, l))
        if not bars:
            raise RuntimeError("history has no usable bars")
        return bars

    def quote(self, ticker: str) -> Quote:
        fi = self.yf.Ticker(ticker).fast_info
        last = _num(fi.last_price)
        if last is None:
            raise RuntimeError("no last price")
        prev = _num(fi.regular_market_previous_close) or _num(fi.previous_close) or last
        hi = _num(fi.day_high) or last
        lo = _num(fi.day_low) or last
        return Quote(last, prev, min(lo, last), max(hi, last))


def play_sound(kind: str) -> None:
    if sys.platform == "darwin" and shutil.which("afplay"):
        name = "Glass.aiff" if kind.startswith("BUY") else "Pop.aiff"
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{name}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        sys.stdout.write("\a")
        sys.stdout.flush()


# ----------------------------------------------------------------------------- scanner

_ALERT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) NY {2}(\S+)\s+(.*)$")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s).astimezone(NY) if s else None
    except ValueError:
        return None


class Scanner:
    def __init__(self, provider, *, clock: Callable[[], datetime] = now_ny, poll_s: int = POLL_S,
                 cooldown_min: int = COOLDOWN_MIN, state_dir: Optional[str] = None,
                 sound: Optional[Callable[[str], None]] = None, out=None, once: bool = False,
                 plan: Optional[Dict[str, dict]] = None, tickers: Optional[List[str]] = None):
        self.provider = provider
        self.clock = clock
        self.poll_s = poll_s
        self.cooldown = timedelta(minutes=cooldown_min)
        self.stale_after_s = 2 * poll_s          # FIX 2
        self.sound = sound or (lambda kind: None)
        self.out = out or sys.stdout
        self.once = once
        self.plan = plan or PLAN
        self.tickers = list(tickers or self.plan.keys())
        self.states: Dict[str, TickerState] = {t: TickerState(t) for t in self.tickers}
        self.alerts: List[Tuple[Optional[datetime], str, str]] = []
        self.alerts_path = os.path.join(state_dir, "alerts.log") if state_dir else None
        self.state_path = os.path.join(state_dir, "scanner_state.json") if state_dir else None
        self.session_open: Optional[bool] = None
        self.last_tick: Optional[datetime] = None
        self.next_poll_at: Optional[datetime] = None
        self.poll_seq = 0
        self.printed_seq = -1
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        self._load_alerts()
        self._load_state()

    # -- session ---------------------------------------------------------------

    @staticmethod
    def in_session(now: datetime) -> bool:
        return now.weekday() < 5 and SESSION_OPEN <= now.time() < SESSION_CLOSE

    @staticmethod
    def session_started_today(now: datetime) -> bool:
        return now.weekday() < 5 and now.time() >= SESSION_OPEN

    def _update_session(self, now: datetime) -> None:
        open_now = self.in_session(now)
        if self.session_open is not None and open_now != self.session_open:
            if open_now:
                self.alert(now, "CHIME", None,
                           f"Market open — scanner armed ({SESSION_OPEN:%H:%M}–{SESSION_CLOSE:%H:%M} NY).",
                           force_sound=True)
            else:
                self.alert(now, "CHIME", None, "Market closed — alerts muted until next open.", force_sound=True)
        self.session_open = open_now

    # -- loop ------------------------------------------------------------------

    def tick(self) -> str:
        """One iteration: detect gaps, update session, poll when due, return the frame."""
        now = self.clock()
        if self.last_tick is not None:
            gap = (now - self.last_tick).total_seconds()
            if gap > self.poll_s:                # FIX 2: laptop slept or process was suspended
                self._log_alert(now, "INFO",
                                f"Resumed after {fmt_age(gap)} gap — rows stale until re-polled, polling now.")
                self.next_poll_at = now
        self._update_session(now)
        if self.next_poll_at is None or now >= self.next_poll_at:
            self.poll(now)
            self.next_poll_at = now + timedelta(seconds=self.poll_s)
            self._save_state()
        self.last_tick = self.clock()
        return self.render(now)

    def run(self) -> None:
        tty = hasattr(self.out, "isatty") and self.out.isatty()
        while True:
            frame = self.tick()
            if tty:
                self.out.write("\x1b[2J\x1b[H" + frame)
            elif self.poll_seq != self.printed_seq:   # not a terminal: one frame per poll, not per second
                self.out.write(frame)
                self.printed_seq = self.poll_seq
            self.out.flush()
            if self.once:
                return
            time.sleep(max(0.05, 1.0 - (time.time() % 1.0)))   # tick on the wall-clock second, no drift

    def poll(self, now: datetime) -> None:
        self.poll_seq += 1
        today = now.date()
        for st in self.states.values():
            if st.hist_day != today:                 # daily bars once per NY day, retried until it works
                try:
                    st.bars = self.provider.history(st.ticker)
                    st.hist_day, st.hist_error = today, None
                except Exception as e:               # FIX 1: keep old bars, show the error
                    st.hist_error, st.error_at = short_err(e), now
            try:
                st.quote = self.provider.quote(st.ticker)
                st.ok_at, st.error, st.fail_count = now, None, 0
            except Exception as e:                   # FIX 1: keep the last good quote, surface the error
                st.error, st.error_at, st.fail_count = short_err(e), now, st.fail_count + 1
        if self.in_session(now):
            for st in self.states.values():
                if st.quote is None or st.ok_at != now:
                    continue                         # never evaluate a stale or missing quote
                lv = self.levels_for(st, now)
                if lv is not None:
                    self.evaluate(st, st.quote.last, lv, now)

    def levels_for(self, st: TickerState, now: datetime) -> Optional[Levels]:
        if not st.bars:
            return None
        ind = compute_indicators(st.bars, st.quote, now.date(), self.session_started_today(now))
        if ind is None:
            return None
        try:
            return resolve_levels(self.plan[st.ticker], ind)
        except ValueError:
            return None

    def is_stale(self, st: TickerState, now: datetime) -> bool:   # FIX 2
        return st.ok_at is not None and (now - st.ok_at).total_seconds() > self.stale_after_s

    # -- signals ---------------------------------------------------------------

    def evaluate(self, st: TickerState, price: float, lv: Levels, now: datetime) -> None:
        if lv.trig is not None:
            if price >= lv.trig:
                if st.breakout_armed:
                    st.breakout_armed, st.breakout_fired_at = False, now
                    self.alert(now, "BUY-BREAKOUT", st,
                               f"{st.ticker} {price:.2f} >= {lv.trig:.2f} — breakout trigger. Call-entry signal.")
            elif price < lv.trig * (1.0 - REARM_PCT / 100.0):
                st.breakout_armed = True             # FIX 5: hysteresis, no re-fire on a wiggle around the level
        if lv.inverted:
            return                                   # FIX 4: an inverted zone is not tradeable
        if price < lv.stop:
            st.pullback_armed = False                # below the stop: must come back from above to re-arm
        elif lv.in_zone(price):                      # FIX 3
            if st.pullback_armed:
                st.pullback_armed, st.pullback_fired_at = False, now
                src = lv.entry_src.rstrip("^") or "entry"
                self.alert(now, "BUY-PULLBACK", st,
                           f"{st.ticker} {price:.2f} in zone {lv.entry:.2f} > {lv.stop:.2f} — pullback to {src}. "
                           f"Call-entry signal.")
        elif price > lv.zone_hi * (1.0 + REARM_PCT / 100.0):
            st.pullback_armed = True

    def alert(self, now: datetime, kind: str, st: Optional[TickerState], msg: str, force_sound: bool = False) -> None:
        muted = False
        if st is not None:
            last = st.last_alert_at.get(kind)
            muted = last is not None and now - last < self.cooldown
            if not muted:
                st.last_alert_at[kind] = now
        if muted:
            msg += " [cooldown]"
        self._log_alert(now, kind, msg)
        if not muted and (force_sound or self.in_session(now)):
            try:
                self.sound(kind)
            except Exception:
                pass

    def _log_alert(self, now: datetime, kind: str, msg: str) -> None:
        self.alerts.append((now, kind, msg))
        if self.alerts_path:
            with open(self.alerts_path, "a", encoding="utf-8") as f:
                f.write(f"{now:%Y-%m-%d %H:%M:%S} NY  {kind:<12} {msg}\n")    # FIX 5: dated

    # -- persistence -----------------------------------------------------------

    def _load_alerts(self) -> None:
        if not self.alerts_path or not os.path.exists(self.alerts_path):
            return
        with open(self.alerts_path, encoding="utf-8") as f:
            lines = f.read().splitlines()[-KEEP_ALERTS * 2:]
        for ln in lines:
            m = _ALERT_RE.match(ln)
            if m:
                self.alerts.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY),
                                    m.group(2), m.group(3)))
            elif ln.strip():
                self.alerts.append((None, "", ln.strip()))     # pre-fix lines without a date

    def _save_state(self) -> None:
        if not self.state_path:
            return
        data = {t: {"breakout_armed": st.breakout_armed, "breakout_fired_at": _iso(st.breakout_fired_at),
                    "pullback_armed": st.pullback_armed, "pullback_fired_at": _iso(st.pullback_fired_at),
                    "last_alert_at": {k: _iso(v) for k, v in st.last_alert_at.items()}}
                for t, st in self.states.items()}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.state_path)

    def _load_state(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for t, d in data.items():
            st = self.states.get(t)
            if st is None or not isinstance(d, dict):
                continue
            st.breakout_armed = bool(d.get("breakout_armed", True))
            st.breakout_fired_at = _parse_iso(d.get("breakout_fired_at"))
            st.pullback_armed = bool(d.get("pullback_armed", True))
            st.pullback_fired_at = _parse_iso(d.get("pullback_fired_at"))
            st.last_alert_at = {k: v for k, v in ((k, _parse_iso(v)) for k, v in d.get("last_alert_at", {}).items()) if v}

    # -- render ----------------------------------------------------------------

    def render(self, now: datetime) -> str:
        open_now = self.in_session(now)
        nxt = 0 if self.next_poll_at is None else max(0, math.ceil((self.next_poll_at - now).total_seconds()))
        fresh = [st for st in self.states.values() if st.quote is not None and not self.is_stale(st, now)]
        hdr = (f"AG CALL SCANNER  {' '.join(self.tickers)}   NY {now:%H:%M:%S} {now:%a %m-%d}   "
               f"{'OPEN' if open_now else 'CLOSED'}   poll {self.poll_s}s · next {nxt}s   "
               f"data {len(fresh)}/{len(self.tickers)}")
        ages = [(now - st.ok_at).total_seconds() for st in self.states.values() if st.ok_at is not None]
        if ages:
            hdr += f" · age {fmt_age(max(ages))}"
        lines = [hdr, "─" * WIDTH,
                 self._cells("TKR", "LAST", "CHG%", "DAY RANGE", "TRIGGER", "BUY ZONE", "STATE")]
        for t in self.tickers:
            lines.append(self._row(self.states[t], now))
        lines.append("─" * WIDTH)
        problems = self._problems(now)
        if problems:
            lines.append("DATA  " + " · ".join(problems))          # FIX 1
        lines.append(f"Session {SESSION_OPEN:%H:%M}-{SESSION_CLOSE:%H:%M} NY · sounds in-session only · "
                     f"cooldown {int(self.cooldown.total_seconds() // 60)}m · zone = entry > stop "
                     f"(dynamic ema/sma track live) · stop = MA-{STOP_BUFFER_ATR:g}ATR · "
                     f"zone tol {ZONE_TOL_PCT:g}% · re-arm {REARM_PCT:g}%")
        lines += ["", "PLAN"]
        for t in self.tickers:
            lines.append(self._plan_line(self.states[t], now))
        lines += ["", "ALERTS  (newest first, also in alerts.log)"]
        for dt, kind, msg in reversed(self.alerts[-KEEP_ALERTS:]):
            lines.append(f"  {dt:%m-%d %H:%M:%S} NY  {kind:<12} {msg}" if dt else f"  {msg}")
        return "\n".join(lines) + "\n"

    def _problems(self, now: datetime) -> List[str]:
        out = []
        failed = [st.ticker for st in self.states.values() if st.error]
        if failed:
            out.append("fetch failed " + " ".join(failed))
        stale = [st.ticker for st in self.states.values() if st.quote is not None and self.is_stale(st, now)]
        if stale:
            out.append("stale " + " ".join(stale))
        nohist = [st.ticker for st in self.states.values() if st.hist_error and not st.bars]
        if nohist:
            out.append("no history " + " ".join(nohist))
        errs = [(st.error_at, st.ticker, st.error or st.hist_error) for st in self.states.values()
                if st.error_at is not None and (st.error or st.hist_error)]
        if errs:
            at, t, msg = max(errs, key=lambda x: x[0])
            out.append(f"last error {at:%H:%M:%S} {t}: {msg}")
        return out

    @staticmethod
    def _cells(*c: str) -> str:
        widths = (6, 9, 9, 16, 16, 30)
        return "".join(f"{v:<{w}}" for v, w in zip(c, widths)) + c[6]

    def _row(self, st: TickerState, now: datetime) -> str:
        q = st.quote
        if q is None:                                            # FIX 1: the row stays, with the reason
            err = st.error or st.hist_error or "waiting for first poll"
            state = f"NO DATA ×{st.fail_count} {err}" if st.fail_count else f"NO DATA {err}"
            return self._cells(st.ticker, "—", "—", "—", "—", "—", state)
        chg = (q.last / q.prev_close - 1.0) * 100.0 if q.prev_close else 0.0
        lv = self.levels_for(st, now)
        if lv is None:
            trig, zone, state = "—", "—", "NO LEVELS " + (st.hist_error or "not enough history")
        else:
            if lv.trig is None:
                trig = "—"
            elif q.last >= lv.trig:
                trig = f"{lv.trig:.2f} HIT"
            else:
                trig = f"{lv.trig:.2f} {(lv.trig / q.last - 1.0) * 100.0:+.2f}%"
            if lv.inverted:                                      # FIX 4
                zone = f"{lv.entry:.2f} > {lv.stop:.2f} ! stop>=entry"
            else:
                zone = (f"{lv.entry:.2f} > {lv.stop:.2f} {(lv.entry / q.last - 1.0) * 100.0:+.2f}% "
                        f"r{lv.risk_pct:.1f}%")
            state = classify(q.last, lv, st, now)
        if self.is_stale(st, now):                               # FIX 2
            state += f"  STALE {fmt_age((now - st.ok_at).total_seconds())}"
            if st.error:
                state += f" · {st.error}"
        return self._cells(st.ticker, f"{q.last:.2f}", f"{chg:+.2f}%", f"{q.day_low:.2f}-{q.day_high:.2f}",
                           trig, zone, state)

    def _plan_line(self, st: TickerState, now: datetime) -> str:
        p = self.plan[st.ticker]
        lv = self.levels_for(st, now)

        def show(spec, val, src):
            if val is None:
                return f"?({spec})" if isinstance(spec, str) else f"{spec:.2f}"
            return f"{val:.2f}({src})" if src else f"{val:.2f}"

        parts = [f"entry {show(p['entry'], lv.entry if lv else None, lv.entry_src if lv else '')}",
                 f"stop {show(p['stop'], lv.stop if lv else None, lv.stop_src if lv else '')}"]
        if p.get("trig") is not None:
            parts.append(f"trig {float(p['trig']):.2f}")
        if lv is not None and lv.inverted:
            parts.append("! stop>=entry")
        return f"{st.ticker:<6}{'  '.join(parts):<60}{p['notes']}"


# ----------------------------------------------------------------------------- cli


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AG call scanner: breakout triggers and pullback buy zones.")
    ap.add_argument("--poll", type=int, default=POLL_S, help="seconds between quote polls")
    ap.add_argument("--cooldown", type=int, default=COOLDOWN_MIN, help="minutes between audible repeats")
    ap.add_argument("--state-dir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="where alerts.log and scanner_state.json live")
    ap.add_argument("--no-sound", action="store_true")
    ap.add_argument("--once", action="store_true", help="poll once, print one frame, exit")
    args = ap.parse_args(argv)
    sc = Scanner(YFProvider(), poll_s=args.poll, cooldown_min=args.cooldown, state_dir=args.state_dir,
                 sound=None if args.no_sound else play_sound, once=args.once)
    try:
        sc.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
