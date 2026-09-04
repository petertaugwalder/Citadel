#!/usr/bin/env python3
"""
natgas_fix.py : settlement-basis, session and formatting fixes for natgas_tracker.py

Drop this file next to natgas_tracker.py and import from it. Pure functions,
standard library only (Python 3.9+), no Schwab client dependency: pass in the
quote dicts the tracker already holds.

What it fixes (measured on the 2026-09-02 18:55 ET run and checked against
Sprague Energy's NYMEX close notes: October settled 2.904 on 09-01 and 2.956
on 09-02):

 1. Schwab's futures closePrice still held the 09-01 settle (2.904) hours after
    the 09-02 settle (2.956) was published. The tracker printed live-vs-2.904
    (+3.62%) as "the day's change". That is a TWO-session move: the 09-02
    session was +1.79% settle-to-settle and the evening session added +1.79%.
    -> resolve_settle() finds the same-day settle (Schwab settleTime, a
       persisted previous closePrice, or the Schwab index stand-ins) and
       labels every change with its basis ("day" or "2-session").
 2. UNG/BOIL "capture" divided a close-to-close ETF move by that two-session
    futures move -> 0.60x / 0.54x. Against the settle-to-settle move both
    vehicles tracked normally (UNG 1.21x, BOIL 2.20x = 1.10 of its 2x target).
    -> capture() uses the settle-to-settle move and reports raw AND of-target.
 3. BOIL capture was leverage-normalised (/2) but labelled "expected ~2.0x".
    -> Capture.render() prints both numbers with their own targets.
 4. Futures prints after 18:00 ET belong to the NEXT Globex trade date while
    the ETF prints are the previous NY close.
    -> globex_trade_date() / globex_session() / ny_session().
 5. Header stamped in UTC, BCOMNG probe stamped in local time.
    -> stamp() everywhere, ET first with UTC in brackets.
 6. "42th"; "at the BOTTOM of its range" for a value BELOW the range;
    weekdays-to-expiry counted Labor Day as a trading day.
    -> ordinal(), range_position(), days_to_expiry().

Run `python3 natgas_fix.py --demo` to see the corrected HEADLINE / VEHICLES
block for the 2026-09-02 run, and `python3 -m unittest test_natgas_fix` for
the tests.
"""
from __future__ import annotations

import argparse
import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Dict, List, Mapping, Optional, Tuple

# ----------------------------------------------------------------------------
# 1. Clocks. One stamp format for every line: ET first, UTC in brackets.
# ----------------------------------------------------------------------------
UTC = timezone.utc


class _USEasternFallback(tzinfo):
    """Used only if the platform has no tz database. US rules since 2007."""

    def _dst_window(self, year: int) -> Tuple[datetime, datetime]:
        march = date(year, 3, 1)
        start = march + timedelta(days=(6 - march.weekday()) % 7 + 7)  # 2nd Sunday
        nov = date(year, 11, 1)
        end = nov + timedelta(days=(6 - nov.weekday()) % 7)  # 1st Sunday
        return datetime.combine(start, time(2)), datetime.combine(end, time(2))

    def dst(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return timedelta(0)
        start, end = self._dst_window(dt.year)
        return timedelta(hours=1) if start <= dt.replace(tzinfo=None) < end else timedelta(0)

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        return timedelta(hours=-5) + self.dst(dt)

    def tzname(self, dt: Optional[datetime]) -> str:
        return "EDT" if self.dst(dt) else "EST"


try:  # pragma: no cover - which branch runs depends on the platform
    from zoneinfo import ZoneInfo

    ET: tzinfo = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = _USEasternFallback()


def to_et(dt: datetime) -> datetime:
    """Aware datetime in New York time. A naive input is taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def stamp(dt: datetime, utc: bool = True) -> str:
    """'09-02 18:55 ET (22:55 UTC)'; the UTC date is shown only when it differs."""
    et = to_et(dt)
    out = et.strftime("%m-%d %H:%M ET")
    if utc:
        u = et.astimezone(UTC)
        out += u.strftime(" (%H:%M UTC)") if u.date() == et.date() else u.strftime(" (%m-%d %H:%M UTC)")
    return out


def stamp_date(dt: datetime) -> str:
    """ISO date in ET, for 'probed on ...' lines."""
    return to_et(dt).date().isoformat()


def _epoch_to_et(value) -> Optional[datetime]:
    """Schwab epoch-millisecond fields (settleTime, quoteTime, tradeTime) -> aware ET."""
    if value is None or value == 0:
        return None
    if isinstance(value, datetime):
        return to_et(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        try:
            return to_et(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except ValueError:
            return None
    if v > 1e11:  # milliseconds
        v /= 1000.0
    return datetime.fromtimestamp(v, tz=UTC).astimezone(ET)


# ----------------------------------------------------------------------------
# 2. Calendar: CME energy holidays, business days, NG expiry, Globex trade date
# ----------------------------------------------------------------------------
GLOBEX_OPEN = time(18, 0)    # ET, opens the NEXT trade date
GLOBEX_CLOSE = time(17, 0)   # ET, daily 60-minute break follows
NG_SETTLE = time(14, 30)     # ET, settlement window 14:28-14:30
NG_SETTLE_PUBLISHED = time(14, 45)  # ET, treat the day's settle as available from here
NY_PRE, NY_OPEN, NY_CLOSE, NY_LATE = time(4, 0), time(9, 30), time(16, 0), time(20, 0)
NG_TICK = 0.001
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(d: date, saturday_to_friday: bool = True) -> Optional[date]:
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def cme_energy_holidays(year: int) -> set:
    """Days with no NYMEX energy settlement (Globex may still run a short session)."""
    raw = [
        _observed(date(year, 1, 1), saturday_to_friday=False),  # New Year's (Sat: not observed)
        _nth_weekday(year, 1, 0, 3),                            # Martin Luther King Jr.
        _nth_weekday(year, 2, 0, 3),                            # Presidents' Day
        _easter(year) - timedelta(days=2),                      # Good Friday
        _last_weekday(year, 5, 0),                              # Memorial Day
        _observed(date(year, 6, 19)),                           # Juneteenth
        _observed(date(year, 7, 4)),                            # Independence Day
        _nth_weekday(year, 9, 0, 1),                            # Labor Day
        _nth_weekday(year, 11, 3, 4),                           # Thanksgiving
        _observed(date(year, 12, 25)),                          # Christmas
    ]
    return {d for d in raw if d is not None and d.year == year}


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in cme_energy_holidays(d.year)


def next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def sessions_between(a: date, b: date) -> int:
    """Number of business days in (a, b]. 0 when b <= a."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if is_business_day(d):
            n += 1
    return n


def business_day_of_month(d: date) -> int:
    """How many business days of d's month have occurred through d (inclusive)."""
    return sum(1 for day in range(1, d.day + 1) if is_business_day(date(d.year, d.month, day)))


def _add_months(year: int, month: int, k: int) -> Tuple[int, int]:
    m0 = month - 1 + k
    return year + m0 // 12, m0 % 12 + 1


def contract_from_symbol(symbol: str) -> Tuple[int, int]:
    """'/NGV26' or 'NGV26' -> (2026, 10)."""
    s = symbol.strip().lstrip("/").upper()
    code, yy = s[-3], s[-2:]
    if code not in MONTH_CODES or not yy.isdigit():
        raise ValueError(f"not a dated NG symbol: {symbol!r}")
    return 2000 + int(yy), MONTH_CODES.index(code) + 1


def ng_expiry(year: int, month: int) -> date:
    """NYMEX NG last trade day: 3 business days before the 1st of the delivery month."""
    d, n = date(year, month, 1), 0
    while n < 3:
        d -= timedelta(days=1)
        if is_business_day(d):
            n += 1
    return d


def ng_expiry_for(symbol: str) -> date:
    return ng_expiry(*contract_from_symbol(symbol))


@dataclass
class DaysLeft:
    weekdays: int
    trading_days: int
    holidays: List[date]

    def render(self) -> str:
        if not self.holidays:
            return f"{self.trading_days} trading days left"
        skipped = ", ".join(h.strftime("%m-%d") for h in self.holidays)
        return f"{self.trading_days} trading days left ({self.weekdays} weekdays, {skipped} excluded)"


def days_to_expiry(as_of: date, expiry: date) -> DaysLeft:
    """Sessions after as_of through expiry inclusive, as weekdays and as trading days."""
    weekdays, trading, holidays = 0, 0, []
    d = as_of
    while d < expiry:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        weekdays += 1
        if is_business_day(d):
            trading += 1
        else:
            holidays.append(d)
    return DaysLeft(weekdays, trading, holidays)


def globex_trade_date(now_et: datetime) -> date:
    """CME trade date of a futures print at now_et. 18:00 ET opens the next date;
    weekend and holiday sessions book to the next business day."""
    d, t, wd = now_et.date(), now_et.time(), now_et.weekday()
    if t >= GLOBEX_OPEN or (wd == 4 and t >= GLOBEX_CLOSE):
        d += timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def globex_session(now_et: datetime) -> str:
    """'OPEN' | 'BREAK' (17:00-18:00 ET) | 'WEEKEND'."""
    t, wd = now_et.time(), now_et.weekday()
    if wd == 5 or (wd == 6 and t < GLOBEX_OPEN) or (wd == 4 and t >= GLOBEX_CLOSE):
        return "WEEKEND"
    if GLOBEX_CLOSE <= t < GLOBEX_OPEN:
        return "BREAK"
    return "OPEN"


def ny_session(now_et: datetime) -> str:
    """Equity session label: 'PRE' | 'RTH' | 'AFTERHOURS' | 'CLOSED'."""
    if not is_business_day(now_et.date()):
        return "CLOSED"
    t = now_et.time()
    if NY_PRE <= t < NY_OPEN:
        return "PRE"
    if NY_OPEN <= t < NY_CLOSE:
        return "RTH"
    if NY_CLOSE <= t < NY_LATE:
        return "AFTERHOURS"
    return "CLOSED"


def last_settled_session(now_et: datetime, published_by: time = NG_SETTLE_PUBLISHED) -> date:
    """Most recent trade date whose NG settlement is out at now_et."""
    d = now_et.date()
    if not is_business_day(d) or now_et.time() < published_by:
        d = prev_business_day(d)
    return d


def session_header(now: datetime, etf_close_date: Optional[date] = None) -> str:
    """One line that says which session each print belongs to."""
    et = to_et(now)
    g = globex_session(et)
    parts = [stamp(et), f"NY {ny_session(et)}"]
    if g == "OPEN":
        parts.append(f"GLOBEX OPEN, trade date {globex_trade_date(et).strftime('%m-%d')}")
    else:
        parts.append(f"GLOBEX {g}")
    etf_day = etf_close_date or last_settled_session(et)
    parts.append(f"ETF prints = {etf_day.strftime('%m-%d')} close")
    return " · ".join(parts)


# ----------------------------------------------------------------------------
# 3. Settlement resolution
# ----------------------------------------------------------------------------
# Business days of the month over which the single-commodity index stand-ins
# roll from the M+1 to the M+2 delivery contract (S&P GSCI: 5th-9th). DJCI is
# set to the same window; confirm against the S&P DJI methodology sheet.
INDEX_ROLL_WINDOW: Dict[str, Tuple[int, int]] = {"$SPGSNG": (5, 9), "$DJCING": (5, 9)}


def index_contract(symbol: str, d: date) -> Tuple[str, Optional[Tuple[int, int]], int]:
    """Which NG delivery month the index holds on d: ('pre-roll', (y, m), bd),
    ('post-roll', (y, m), bd) or ('rolling', None, bd)."""
    lo, hi = INDEX_ROLL_WINDOW.get(symbol, (5, 9))
    bd = business_day_of_month(d)
    if bd < lo:
        return "pre-roll", _add_months(d.year, d.month, 1), bd
    if bd > hi:
        return "post-roll", _add_months(d.year, d.month, 2), bd
    return "rolling", None, bd


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _round_tick(x: float, tick: float = NG_TICK) -> float:
    return round(round(x / tick) * tick, 6)


def _index_pct(iq) -> Optional[float]:
    if isinstance(iq, (int, float)):
        return float(iq)
    for key in ("netPercentChange", "netPercentChangeInDouble", "percentChange", "pct"):
        v = _num(iq.get(key)) if isinstance(iq, Mapping) else None
        if v is not None:
            return v
    return None


def _index_timestamp(iq) -> Optional[datetime]:
    if not isinstance(iq, Mapping):
        return None
    stamps = [_epoch_to_et(iq.get(k)) for k in ("tradeTime", "quoteTime", "ts")]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def derive_settle_from_index(
    prev_settle: float,
    fut_last: Optional[float],
    idx_quote,
    session: date,
    contract: Tuple[int, int],
    symbol: str,
    *,
    mode: str = "auto",
    tick: float = NG_TICK,
    published_by: time = NG_SETTLE_PUBLISHED,
) -> Tuple[Optional[float], str]:
    """Same-day settle of `contract` implied by a single-commodity index quote.

    mode 'close': the index value is its official close (14:30 settlement mark)
                  and its % change is settle-to-settle  -> prev_settle * (1 + pct)
    mode 'live' : the index value is live and its % change is vs today's close
                  -> fut_last / (1 + pct)
    mode 'auto' : pick by the index quote's own timestamp.
    Returns (price or None, reason)."""
    state, held, bd = index_contract(symbol, session)
    if held != contract:
        what = f"{held[0]}-{held[1]:02d}" if held else "a blend"
        return None, f"{symbol}: holds {what} on {session} (business day {bd}, {state}); cannot derive {contract[0]}-{contract[1]:02d}"
    pct = _index_pct(idx_quote)
    if pct is None:
        return None, f"{symbol}: no percent change in quote"
    ts = _index_timestamp(idx_quote)
    if mode == "auto":
        if ts is None:
            mode, why = "close", "no timestamp, assumed official close"
        elif ts.date() < session:
            return None, f"{symbol}: value stamped {stamp(ts)} predates the {session} settlement"
        elif ts.date() == session and ts.time() <= (datetime.combine(session, published_by) + timedelta(hours=1)).time():
            mode, why = "close", f"stamped {stamp(ts, utc=False)}"
        else:
            mode, why = "live", f"stamped {stamp(ts, utc=False)}"
    else:
        why = "mode forced"
    if mode == "close":
        derived = _round_tick(prev_settle * (1 + pct / 100.0), tick)
        if fut_last is not None and abs(derived - fut_last) <= tick:
            return None, f"{symbol}: {pct:+.2f}% equals the live futures move; its base is as stale as closePrice"
        return derived, f"{symbol} {pct:+.2f}% x previous settle {prev_settle:.3f} ({why})"
    if fut_last is None:
        return None, f"{symbol}: live mode needs the futures last price"
    derived = _round_tick(fut_last / (1 + pct / 100.0), tick)
    if abs(derived - prev_settle) <= tick:
        return None, f"{symbol}: live {pct:+.2f}% spans the same two-session window as closePrice"
    return derived, f"live {fut_last:.3f} / (1 {pct:+.2f}%) via {symbol} ({why})"


@dataclass
class Settle:
    price: Optional[float]
    session: Optional[date]          # trade date the settlement belongs to
    source: str                      # 'known' | 'schwab:<field>' | 'derived:<index>' | 'none'
    stale_sessions: int              # 0 = the last published settle; 1 = one session older ...
    schwab_close: Optional[float] = None
    schwab_settle_time: Optional[datetime] = None
    notes: List[str] = field(default_factory=list)

    @property
    def basis(self) -> str:
        return "day" if self.stale_sessions == 0 else f"{self.stale_sessions + 1}-session"

    def label(self) -> str:
        if self.price is None:
            return "settle unavailable"
        when = self.session.strftime("%m-%d") if self.session else "?"
        return f"{when} settle {self.price:.3f} [{self.source}, {self.basis} basis]"


def resolve_settle(
    payload: Mapping,
    now: datetime,
    *,
    contract: Tuple[int, int],
    known: Optional[Mapping[date, float]] = None,
    index_quotes: Optional[Mapping[str, object]] = None,
    prev_close_seen: Optional[float] = None,
    index_mode: str = "auto",
    tick: float = NG_TICK,
    published_by: time = NG_SETTLE_PUBLISHED,
) -> Settle:
    """Find the most recent settlement for one contract and say how stale it is.

    payload         Schwab quote payload for the contract: {'quote': {...}, 'reference': {...}}
                    (a bare quote dict also works).
    contract        (year, month) of delivery, e.g. contract_from_symbol('/NGV26').
    known           {session_date: settle} you trust (your own settle log). Highest priority.
    index_quotes    {'$DJCING': quote_dict_or_pct, ...} single-commodity index stand-ins,
                    used to derive the same-day settle when Schwab's field is a session behind.
    prev_close_seen closePrice recorded on a run whose last_settled_session was the
                    previous session. Equal to today's closePrice => Schwab has not rolled.
    """
    now_et = to_et(now)
    session = last_settled_session(now_et, published_by)
    q = payload.get("quote", payload) if isinstance(payload, Mapping) else {}
    r = payload.get("reference", {}) if isinstance(payload, Mapping) else {}
    last = _num(q.get("lastPrice"))
    close = _num(q.get("closePrice"))
    fsp = _num(r.get("futureSettlementPrice"))
    st = _epoch_to_et(q.get("settleTime"))
    notes: List[str] = []

    if known and session in known:
        return Settle(float(known[session]), session, "known", 0, close, st, ["settlement supplied by caller"])

    schwab = fsp if fsp is not None else close
    schwab_field = "futureSettlementPrice" if fsp is not None else "closePrice"
    if fsp is not None and close is not None and abs(fsp - close) > tick / 2:
        notes.append(f"futureSettlementPrice {fsp:.3f} != closePrice {close:.3f}; using {schwab_field}")
    if schwab is None:
        return Settle(None, None, "none", 0, close, st, notes + ["no closePrice/futureSettlementPrice in payload"])

    stale: Optional[int] = None
    if st is not None:
        st_session = st.date() if st.time() == time(0, 0) and is_business_day(st.date()) else last_settled_session(st, NG_SETTLE)
        stale = sessions_between(st_session, session)
        notes.append(f"settleTime {stamp(st, utc=False)} -> {st_session} settle ({stale} session(s) behind)")
    if prev_close_seen is not None and close is not None and abs(close - prev_close_seen) <= tick / 2:
        if stale == 0:
            notes.append("settleTime claims same-day but closePrice is unchanged since the previous session; treating as stale")
        stale = max(stale or 0, 1)
        notes.append(f"closePrice {close:.3f} unchanged since the previous session's run")
    if stale is None:
        same_evening = now_et.date() == session and now_et.time() >= published_by
        stale = 1 if same_evening else 0
        notes.append("staleness ASSUMED (no settleTime, no prev_close_seen): "
                     + ("same evening -> Schwab one session behind, as measured 2026-09-02" if same_evening
                        else "later session -> Schwab field taken as current"))

    if stale == 0:
        return Settle(schwab, session, f"schwab:{schwab_field}", 0, close, st, notes)

    if stale == 1 and index_quotes:
        derived: List[Tuple[str, float]] = []
        for sym, iq in index_quotes.items():
            price, why = derive_settle_from_index(schwab, last, iq, session, contract, sym,
                                                  mode=index_mode, tick=tick, published_by=published_by)
            notes.append(why)
            if price is not None:
                derived.append((sym, price))
        if derived:
            prices = [p for _, p in derived]
            if max(prices) - min(prices) <= 2 * tick:
                price = _round_tick(sum(prices) / len(prices), tick)
                src = "derived:" + "/".join(s for s, _ in derived)
                prev_session = prev_business_day(session)
                notes.append(f"Schwab {schwab_field} {schwab:.3f} = {prev_session.strftime('%m-%d')} settle (1 session stale)")
                return Settle(price, session, src, 0, close, st, notes)
            notes.append("index stand-ins disagree by more than 2 ticks: " + ", ".join(f"{s} {p:.3f}" for s, p in derived))

    older = session
    for _ in range(stale):
        older = prev_business_day(older)
    notes.append(f"{stale} session(s) stale: changes vs this value are {stale + 1}-session moves")
    return Settle(schwab, older, f"schwab:{schwab_field}", stale, close, st, notes)


def settle_field_report(payload: Mapping, now: datetime) -> str:
    """One log line per run. Append it for a few sessions to see which Schwab
    field rolls to the new settlement, and when."""
    q = payload.get("quote", payload)
    r = payload.get("reference", {})
    last, close, fsp = _num(q.get("lastPrice")), _num(q.get("closePrice")), _num(r.get("futureSettlementPrice"))
    nc, fpc = _num(q.get("netChange")), _num(q.get("futurePercentChange"))
    base = None if last is None or nc is None else last - nc

    def f(x: Optional[float]) -> str:
        return "-" if x is None else f"{x:.3f}"

    def t(k: str) -> str:
        s = _epoch_to_et(q.get(k))
        return "-" if s is None else stamp(s, utc=False)

    return (f"{stamp(now)} · last {f(last)} · closePrice {f(close)} · futureSettlementPrice {f(fsp)}"
            f" · settleTime {t('settleTime')} · netChange {f(nc)} -> base {f(base)}"
            f" · futurePercentChange {'-' if fpc is None else f'{fpc:+.2f}%'}"
            f" · quoteTime {t('quoteTime')} · tradeTime {t('tradeTime')}")


# ----------------------------------------------------------------------------
# 4. Changes and vehicle capture
# ----------------------------------------------------------------------------
def change(last: float, base: float) -> Tuple[float, float]:
    """(absolute, percent) of last vs base."""
    return last - base, (last / base - 1.0) * 100.0


def fmt_change(last: float, base: float, basis: str = "day") -> str:
    d, p = change(last, base)
    return f"{d:+.3f} ({p:+.2f}%, {basis})"


@dataclass
class Capture:
    etf_pct: float
    ng_pct: float
    leverage: float
    raw: Optional[float]        # ETF move / NG move
    of_target: Optional[float]  # raw / leverage, 1.0 = tracked its target exactly

    def render(self) -> str:
        if self.raw is None:
            return f"capture n/a (NG settle-to-settle {self.ng_pct:+.2f}%, too small to divide by)"
        return (f"capture {self.raw:.2f}x of NG (target {self.leverage:.1f}x)"
                f" = {self.of_target:.2f} of target · NG settle-to-settle {self.ng_pct:+.2f}%")


def capture(etf_pct: float, ng_settle_pct: float, leverage: float = 1.0, min_abs_ng_pct: float = 0.25) -> Capture:
    """Same-window capture: ETF close-to-close vs futures settle-to-settle.
    Never divide a 16:00 ET ETF move by a live evening-session futures quote."""
    if abs(ng_settle_pct) < min_abs_ng_pct:
        return Capture(etf_pct, ng_settle_pct, leverage, None, None)
    raw = etf_pct / ng_settle_pct
    return Capture(etf_pct, ng_settle_pct, leverage, raw, raw / leverage)


# ----------------------------------------------------------------------------
# 5. Formatting
# ----------------------------------------------------------------------------
def ordinal(n) -> str:
    n = int(round(float(n)))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def rank_pctile(value: float, history: List[float]) -> float:
    """Percent of history rows strictly below value (the tracker's 'pctl')."""
    if not history:
        return 0.0
    return 100.0 * sum(1 for h in history if h < value) / len(history)


def range_position(value: float, lo: float, hi: float, rank_pct: float, n_rows: int, tick: float = NG_TICK) -> str:
    rng = f"{lo:+.3f}..{hi:+.3f}"
    if value < lo - tick / 2:
        return f"NEW LOW, below its {n_rows}-row range ({rng})"
    if value > hi + tick / 2:
        return f"NEW HIGH, above its {n_rows}-row range ({rng})"
    if rank_pct <= 5:
        return f"at the BOTTOM of its {n_rows}-row range ({rng})"
    if rank_pct >= 95:
        return f"at the TOP of its {n_rows}-row range ({rng})"
    return f"{ordinal(rank_pct)} rank-pctile of its {n_rows}-row range ({rng})"


# ----------------------------------------------------------------------------
# 6. Renderers for the two blocks that were wrong, and a demo of the 09-02 run
# ----------------------------------------------------------------------------
def render_headline(symbol: str, last: float, settle: Settle, oi: Optional[int], now: datetime) -> List[str]:
    y, m = contract_from_symbol(symbol)
    exp = ng_expiry(y, m)
    left = days_to_expiry(to_et(now).date(), exp)
    month = f"{calendar.month_abbr[m]}-{y}"
    lines = [f"NG1! -> {symbol} ({month}, the active front)  {last:.3f}"]
    if settle.price is not None:
        lines[0] += f"  {fmt_change(last, settle.price, settle.basis)}  vs {settle.label()}"
        if settle.stale_sessions == 0 and settle.schwab_close is not None and abs(settle.schwab_close - settle.price) > NG_TICK / 2:
            prev = prev_business_day(settle.session) if settle.session else None
            when = prev.strftime("%m-%d") if prev else "prev"
            lines.append(f"since {when} settle {settle.schwab_close:.3f}: {fmt_change(last, settle.schwab_close, '2-session')}"
                         f"  (Schwab closePrice, one session behind)")
    if oi is not None:
        lines[0] += f"  OI {oi:,}"
    lines.append(f"expires {exp.isoformat()} · {left.render()}")
    for n in settle.notes:
        lines.append(f"note: {n}")
    return lines


def render_vehicle(name: str, last: float, chg: float, volume: Optional[int], ng_settle_pct: float, leverage: float) -> str:
    prev = last - chg
    pct = chg / prev * 100.0
    cap = capture(pct, ng_settle_pct, leverage)
    vol = f" vol {volume:,}" if volume is not None else ""
    return f"{name:<5} {last:.2f}  {chg:+.2f} ({pct:+.2f}%){vol} · {cap.render()}"


DEMO_NOW = datetime(2026, 9, 2, 22, 55, tzinfo=UTC)
DEMO_NGV26 = {
    "quote": {"lastPrice": 3.009, "closePrice": 2.904, "netChange": 0.105, "futurePercentChange": 3.62, "openInterest": 326958},
    "reference": {"futureSettlementPrice": 2.904},
}
DEMO_INDEX = {"$DJCING": {"lastPrice": 160.74, "netPercentChange": 1.79},
              "$SPGSNG": {"lastPrice": 138.40, "netPercentChange": 1.79}}
DEMO_ETFS = [("UNG", 10.81, 0.23, 12_196_777, 1.0), ("BOIL", 21.39, 0.81, 4_072_032, 2.0)]


def demo() -> str:
    settle = resolve_settle(DEMO_NGV26, DEMO_NOW, contract=contract_from_symbol("/NGV26"), index_quotes=DEMO_INDEX)
    out = [f"NATGAS TRACKER (fixed basis) · {session_header(DEMO_NOW)}", "", "  HEADLINE"]
    out += ["     " + l for l in render_headline("/NGV26", 3.009, settle, 326958, DEMO_NOW)]
    out += ["", "  VEHICLES"]
    if settle.price is not None and settle.schwab_close is not None and settle.stale_sessions == 0:
        _, ng_pct = change(settle.price, settle.schwab_close)
    else:
        ng_pct = float("nan")
    for name, last, chg, vol, lev in DEMO_ETFS:
        out.append("     " + render_vehicle(name, last, chg, vol, ng_pct, lev))
    out.append("     window: ETF close-to-close (16:00 ET) vs NG settle-to-settle (14:30 ET); the live quote is not in the ratio")
    out += ["", "  DIAGNOSTIC (append one per run to learn when Schwab rolls the settle field)",
            "     " + settle_field_report(DEMO_NGV26, DEMO_NOW)]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--demo", action="store_true", help="render the corrected 2026-09-02 HEADLINE/VEHICLES block")
    args = ap.parse_args()
    print(demo() if args.demo else ap.format_help())
