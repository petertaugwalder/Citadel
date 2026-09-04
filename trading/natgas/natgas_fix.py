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
    -> resolve_settle() finds the same-day settle: the live Schwab index
       stand-ins imply it (futures_last / index ratio), which confirms or
       overrides Schwab's field; settleTime and a persisted closePrice are
       secondary evidence. Every change is labelled "day" or "2-session".
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
blocks for the 2026-09-02 and 2026-09-04 runs, and `python3 -m unittest test_natgas_fix` for
the tests.
"""
from __future__ import annotations

import argparse
import calendar
import math
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

# Measured on Schwab (runs of 2026-09-02 18:55 ET and 2026-09-04 07:59 ET):
#  * $DJCING / $SPGSNG are LIVE quotes and their % change is against the index's
#    SAME-DAY close: 09-04 07:59 ET $DJCING +0.34% = /NGV26 2.923 vs its 09-03
#    settle 2.913; 09-02 18:55 ET +1.79% = 3.009 vs the 09-02 settle 2.956.
#  * the futures closePrice rolls to the new settle the NEXT MORNING (settleTime
#    09-04 07:53 ET carried 2.913). On the evening of the settle it still holds
#    the previous one (09-02 17:38 ET stamp, value 2.904 = the 09-01 settle).
#  * settleTime is therefore a posted-at stamp, not the settlement date.
# Hence the same-day settle implied by a live index is
#     futures_last / (index_last / index_prior_close)
# and Schwab's field is confirmed when that agrees with it, overridden when not.


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
    return round(math.floor(round(x / tick, 6) + 0.5) * tick, 6)


def _index_ratio(iq) -> Tuple[Optional[float], str]:
    """(index_last / index_prior_close, description). The two prices beat the
    2-decimal percent when both are present."""
    if isinstance(iq, (int, float)):
        return 1.0 + float(iq) / 100.0, f"{float(iq):+.2f}%"
    if not isinstance(iq, Mapping):
        return None, "unusable index quote"
    q = iq.get("quote", iq)
    last, close = _num(q.get("lastPrice")), _num(q.get("closePrice"))
    if last and close:
        return last / close, f"{last:.2f}/{close:.2f} ({(last / close - 1) * 100:+.2f}%)"
    for key in ("netPercentChange", "netPercentChangeInDouble", "percentChange", "pct"):
        pct = _num(q.get(key))
        if pct is not None:
            return 1.0 + pct / 100.0, f"{pct:+.2f}%"
    return None, "no percent change in quote"


def _index_timestamp(iq) -> Optional[datetime]:
    if not isinstance(iq, Mapping):
        return None
    q = iq.get("quote", iq)
    stamps = [_epoch_to_et(q.get(k)) for k in ("tradeTime", "quoteTime", "ts")]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def derive_settle_from_index(
    fut_last: Optional[float],
    idx_quote,
    session: date,
    contract: Tuple[int, int],
    symbol: str,
    *,
    mode: str = "live",
    prev_settle: Optional[float] = None,
    tick: float = NG_TICK,
) -> Tuple[Optional[float], str]:
    """Same-day settle of `contract` implied by one index stand-in.

    mode 'live'  (Schwab, measured): the index is live and its base is its
                 same-day close            -> fut_last / (idx_last / idx_close)
    mode 'close': the index value is its official close and its base is the
                 previous close            -> prev_settle * (idx_last / idx_close)
    Returns (price rounded to the tick, reason). Accuracy is about one tick when
    only the rounded percent is available."""
    state, held, bd = index_contract(symbol, session)
    if held != contract:
        what = f"{held[0]}-{held[1]:02d}" if held else "a blend"
        return None, (f"{symbol}: holds {what} on {session} (business day {bd}, {state}); "
                      f"cannot derive {contract[0]}-{contract[1]:02d}")
    ratio, desc = _index_ratio(idx_quote)
    if ratio is None or ratio <= 0:
        return None, f"{symbol}: {desc}"
    ts = _index_timestamp(idx_quote)
    if ts is not None and ts < datetime.combine(session, NG_SETTLE, tzinfo=ET):
        return None, f"{symbol}: value stamped {stamp(ts, utc=False)} predates the {session} settlement"
    if mode == "close":
        if prev_settle is None:
            return None, f"{symbol}: close mode needs the previous settle"
        return _round_tick(prev_settle * ratio, tick), f"{symbol} {desc} x previous settle {prev_settle:.3f} (close mode)"
    if fut_last is None:
        return None, f"{symbol}: live mode needs the futures last price"
    return _round_tick(fut_last / ratio, tick), f"futures {fut_last:.3f} / {symbol} {desc}"


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
    index_mode: str = "live",
    tick: float = NG_TICK,
    published_by: time = NG_SETTLE_PUBLISHED,
) -> Settle:
    """Find the most recent settlement for one contract and say how stale it is.

    payload         Schwab quote payload for the contract: {'quote': {...}, 'reference': {...}}
                    (a bare quote dict also works).
    contract        (year, month) of delivery, e.g. contract_from_symbol('/NGV26').
    known           {session_date: settle} you trust (your own settle log). Highest priority.
    index_quotes    {'$DJCING': quote_dict_or_pct, ...} single-commodity index stand-ins.
                    Their live value implies the same-day settle; it confirms Schwab's
                    field when the two agree and replaces it when they do not.
    prev_close_seen closePrice recorded on a run whose last_settled_session was the
                    previous session. Equal to today's closePrice => Schwab has not rolled;
                    different => it has.
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
    schwab_src = f"schwab:{schwab_field}"
    same_evening = now_et.date() == session and now_et.time() >= published_by

    # evidence 1: settleTime. A posted-at stamp; only the session it maps to is informative.
    st_stale: Optional[int] = None
    if st is not None:
        st_session = st.date() if st.time() == time(0, 0) and is_business_day(st.date()) else last_settled_session(st, NG_SETTLE)
        st_stale = sessions_between(st_session, session)
        notes.append(f"settleTime {stamp(st, utc=False)} (posted-at) -> {st_session} session, {st_stale} behind")

    # evidence 2: did closePrice move since the previous session's run?
    unchanged = prev_close_seen is not None and close is not None and abs(close - prev_close_seen) <= tick / 2
    rolled = prev_close_seen is not None and close is not None and not unchanged
    if unchanged:
        notes.append(f"closePrice {close:.3f} unchanged since the previous session's run: Schwab has not rolled")
    elif rolled:
        notes.append(f"closePrice moved {prev_close_seen:.3f} -> {close:.3f} since the previous session's run: Schwab has rolled")

    # evidence 3: the same-day settle implied by the live index stand-ins
    derived: Optional[float] = None
    derived_src = ""
    if index_quotes:
        found: List[Tuple[str, float]] = []
        for sym, iq in index_quotes.items():
            price, why = derive_settle_from_index(last, iq, session, contract, sym, mode=index_mode, prev_settle=schwab, tick=tick)
            notes.append(why)
            if price is not None:
                found.append((sym, price))
        if found:
            prices = [p for _, p in found]
            if max(prices) - min(prices) <= 2 * tick:
                derived = _round_tick(sum(prices) / len(prices), tick)
                derived_src = "derived:" + "/".join(s for s, _ in found)
            else:
                notes.append("index stand-ins disagree by more than 2 ticks, not used: " + ", ".join(f"{s} {p:.3f}" for s, p in found))

    if derived is not None:
        agree = abs(derived - schwab) <= 2 * tick
        if agree:
            known_stale = unchanged or (st_stale or 0) >= 1
            # On the settle evening an index that lands exactly on closePrice has a base
            # as stale as closePrice (or the evening is dead flat): not a confirmation.
            ambiguous = same_evening and not rolled and (close is None or abs(schwab - close) <= tick / 2)
            if not known_stale and not ambiguous:
                notes.append(f"index-implied {derived:.3f} confirms Schwab {schwab_field} {schwab:.3f} as the {session} settle")
                return Settle(schwab, session, schwab_src, 0, close, st, notes)
            notes.append(f"index-implied {derived:.3f} equals Schwab's value while it is "
                         + ("known" if known_stale else "presumed") + " stale: the index base has not rolled either")
        elif rolled and (st_stale is None or st_stale == 0):
            notes.append(f"index-implied {derived:.3f} disagrees with Schwab {schwab:.3f}, but closePrice rolled since "
                         f"the previous session: keeping Schwab; check {derived_src[8:]} against its roll window")
            return Settle(schwab, session, schwab_src, 0, close, st, notes)
        else:
            prev_session = prev_business_day(session)
            notes.append(f"Schwab {schwab_field} {schwab:.3f} = {prev_session.strftime('%m-%d')} settle (one session behind); "
                         f"using index-implied {derived:.3f}")
            return Settle(derived, session, derived_src, 0, close, st, notes)

    # no usable index reading: decide from the other evidence
    if unchanged:
        stale = max(st_stale or 0, 1)
    elif st_stale is not None:
        stale = st_stale
    elif rolled:
        stale = 0
    else:
        stale = 1 if same_evening else 0
        notes.append("staleness ASSUMED (no settleTime, no prev_close_seen, no index): "
                     + ("same evening -> Schwab one session behind, as measured 2026-09-02" if same_evening
                        else "later session -> Schwab field taken as current, as measured 2026-09-04"))
    if stale == 0:
        return Settle(schwab, session, schwab_src, 0, close, st, notes)
    older = session
    for _ in range(stale):
        older = prev_business_day(older)
    notes.append(f"{stale} session(s) stale: changes vs this value are {stale + 1}-session moves")
    return Settle(schwab, older, schwab_src, stale, close, st, notes)


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
    ng_window: str = "settle-to-settle"

    def render(self) -> str:
        if self.raw is None:
            return f"capture n/a (NG {self.ng_window} {self.ng_pct:+.2f}%, too small to divide by)"
        return (f"capture {self.raw:.2f}x of NG (target {self.leverage:.1f}x)"
                f" = {self.of_target:.2f} of target · NG {self.ng_window} {self.ng_pct:+.2f}%")


def capture(etf_pct: float, ng_pct: float, leverage: float = 1.0, min_abs_ng_pct: float = 0.25,
            ng_window: str = "settle-to-settle") -> Capture:
    """Same-window capture. Both legs must span the same two timestamps: an ETF
    close-to-close (16:00 ET) against the futures over 16:00 -> 16:00 when you have
    that print, else against settle-to-settle (14:30). Never divide an ETF close
    move by a live evening-session futures quote."""
    if abs(ng_pct) < min_abs_ng_pct:
        return Capture(etf_pct, ng_pct, leverage, None, None, ng_window)
    raw = etf_pct / ng_pct
    return Capture(etf_pct, ng_pct, leverage, raw, raw / leverage, ng_window)


# ----------------------------------------------------------------------------
# 5. Formatting
# ----------------------------------------------------------------------------
def ordinal(n) -> str:
    n = int(math.floor(float(n) + 0.5))
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
# 6. Renderers for the blocks that were wrong, and a demo of the two measured runs
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


def render_vehicle(name: str, last: float, chg: float, volume: Optional[int], ng_pct: float, leverage: float,
                   ng_window: str = "settle-to-settle") -> str:
    prev = last - chg
    pct = chg / prev * 100.0
    cap = capture(pct, ng_pct, leverage, ng_window=ng_window)
    vol = f" vol {volume:,}" if volume is not None else ""
    return f"{name:<5} {last:.2f}  {chg:+.2f} ({pct:+.2f}%){vol} · {cap.render()}"


# Run 1: 2026-09-02 18:55 ET. Schwab closePrice still on the 09-01 settle.
DEMO_NOW = datetime(2026, 9, 2, 22, 55, tzinfo=UTC)
DEMO_NGV26 = {
    "quote": {"lastPrice": 3.009, "closePrice": 2.904, "netChange": 0.105, "futurePercentChange": 3.62, "openInterest": 326958},
    "reference": {"futureSettlementPrice": 2.904},
}
DEMO_INDEX = {"$DJCING": {"lastPrice": 160.74, "netPercentChange": 1.79},
              "$SPGSNG": {"lastPrice": 138.40, "netPercentChange": 1.79}}
DEMO_ETFS = [("UNG", 10.81, 0.23, 12_196_777, 1.0), ("BOIL", 21.39, 0.81, 4_072_032, 2.0)]

# Run 2: 2026-09-04 07:59 ET. closePrice rolled to the 09-03 settle at 07:53 ET.
DEMO2_NOW = datetime(2026, 9, 4, 11, 59, tzinfo=UTC)
DEMO2_NGV26 = {
    "quote": {"lastPrice": 2.923, "closePrice": 2.913, "openInterest": 310695,
              "settleTime": int(datetime(2026, 9, 4, 7, 53, tzinfo=ET).timestamp() * 1000)},
    "reference": {"futureSettlementPrice": 2.913},
}
DEMO2_INDEX = {"$DJCING": {"lastPrice": 158.95, "netPercentChange": 0.34},
               "$SPGSNG": {"lastPrice": 136.90, "netPercentChange": 0.38}}
# last completed session 09-02 -> 09-03, both legs 16:00 ET -> 16:00 ET (the tracker's own prints)
DEMO2_CAPTURE = [("UNG", -2.33, 1.0), ("BOIL", -5.16, 2.0)]
DEMO2_NG_1600 = -2.44


def demo() -> str:
    out: List[str] = []
    # run 1
    settle = resolve_settle(DEMO_NGV26, DEMO_NOW, contract=contract_from_symbol("/NGV26"), index_quotes=DEMO_INDEX)
    out += [f"RUN 1 · {session_header(DEMO_NOW)}", "", "  HEADLINE"]
    out += ["     " + l for l in render_headline("/NGV26", 3.009, settle, 326958, DEMO_NOW)]
    out += ["", "  VEHICLES (settle-to-settle window; the 16:00 -> 16:00 futures print is better when you have it)"]
    ng_pct = change(settle.price, settle.schwab_close)[1] if settle.stale_sessions == 0 else float("nan")
    for name, last, chg, vol, lev in DEMO_ETFS:
        out.append("     " + render_vehicle(name, last, chg, vol, ng_pct, lev))
    out += ["", "  DIAGNOSTIC", "     " + settle_field_report(DEMO_NGV26, DEMO_NOW), ""]
    # run 2
    settle2 = resolve_settle(DEMO2_NGV26, DEMO2_NOW, contract=contract_from_symbol("/NGV26"),
                             index_quotes=DEMO2_INDEX, prev_close_seen=2.904)
    out += [f"RUN 2 · {session_header(DEMO2_NOW)}", "", "  HEADLINE"]
    out += ["     " + l for l in render_headline("/NGV26", 2.923, settle2, 310695, DEMO2_NOW)]
    out += ["", "  VEHICLES (last completed session 09-02 -> 09-03, both legs 16:00 ET)"]
    for name, pct, lev in DEMO2_CAPTURE:
        cap = capture(pct, DEMO2_NG_1600, lev, ng_window="16:00->16:00")
        out.append(f"     {name:<5} {pct:+.2f}% · {cap.render()}")
    out += ["", "  DIAGNOSTIC", "     " + settle_field_report(DEMO2_NGV26, DEMO2_NOW)]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--demo", action="store_true", help="render the corrected HEADLINE/VEHICLES blocks for the two measured runs")
    args = ap.parse_args()
    print(demo() if args.demo else ap.format_help())
