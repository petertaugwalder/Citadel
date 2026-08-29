#!/usr/bin/env python3
"""TLT nightly scorecard. Matches the Pine rules without the proxy/adjust bugs.

A standalone one-page read: weighted score, regime, both stacks, one verdict.
It shares no code with tlt_scanner.py and can disagree with it — the weights are
coarser and the verdict vocabulary finer. Use the scanner for levels, stops and
the backtest; use this for a fast nightly call.

Four fixes against the draft, each marked FIX and covered by a test:
  1. `last > 35 <= prev` is a Python chained comparison meaning
     (last > 35) and (35 <= prev) — it demands RSI was ALREADY above 35 and so
     excludes every genuine cross. The bounce hook was inverted.
  2. regime points are normalised to a fixed ±100 scale; without a UB feed the
     raw sum spans only ±75, so the ±25 BULL/BEAR thresholds silently meant
     something stricter.
  3. a missing 30y series emptied the frame via dropna and then crashed on
     .iloc[-1]; it now fails with a clear message.
  4. datetime.utcfromtimestamp is deprecated from Python 3.12.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

CALL_THRESH, PUT_THRESH, DURATION = 5, -5, 15.0
REGIME_TERMS = 8       # the full set: 4 TLT + 2 UB + 2 yield
SCORE_MAX, SCORE_MIN = 11, -12        # with a UB feed; the steepener only subtracts
SCORE_MAX_NOUB, SCORE_MIN_NOUB = 8, -9  # without one, the UB terms drop out


def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def sma(s, n): return s.rolling(n).mean()

def rsi(s, n=14):
    d = s.diff()
    up, down = d.clip(lower=0.0), -d.clip(upper=0.0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = down.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))

def macd_hist(s):
    line = ema(s, 12) - ema(s, 26)
    return line - ema(line, 9)

def _pct_yield(s: pd.Series) -> pd.Series:
    """Yields arrive either as percent (^TYX 5.19) or index points ($TYX 51.9)."""
    s = s.astype(float)
    med = s.dropna().median()
    return s / 10.0 if pd.notna(med) and med > 20 else s


def load_yfinance(start: str):
    import yfinance as yf
    notes = ["source=yfinance"]
    raw = yf.download(
        ["TLT", "UB=F", "^TYX", "^TNX"],
        start=start, auto_adjust=False, progress=False,
        group_by="ticker", threads=True,
    )

    def col(ticker, fld):
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker not in raw.columns.get_level_values(0):
                return pd.Series(dtype=float)
            if fld not in raw[ticker].columns:
                return pd.Series(dtype=float)
            return raw[ticker][fld].dropna()
        return raw[fld].dropna() if fld in raw.columns else pd.Series(dtype=float)

    tlt = col("TLT", "Close")
    if tlt.empty:
        raise RuntimeError("no TLT")
    df = pd.DataFrame({
        "tlt": tlt,
        "tlt_low": col("TLT", "Low").reindex(tlt.index),
        "tlt_high": col("TLT", "High").reindex(tlt.index),
        "ub": col("UB=F", "Close").reindex(tlt.index),
        "y30": _pct_yield(col("^TYX", "Close").reindex(tlt.index)),
        "y10": _pct_yield(col("^TNX", "Close").reindex(tlt.index)),
    })
    if df["ub"].notna().sum() < 60:
        notes.append("UB=F missing — gate uses 30Y only; no proxy double-count")
    # FIX 3: dropna on an all-NaN y30 empties the frame and crashes downstream
    if df["y30"].notna().sum() < 60:
        raise RuntimeError("^TYX unavailable — the 30y yield is the driver, not optional")
    return df.dropna(subset=["tlt", "y30"]), notes


def load_polygon(start: str):
    from polygon import RESTClient
    notes = ["source=polygon", "UB missing — gate uses 30Y only"]
    client = RESTClient()
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    aggs = list(client.list_aggs("TLT", 1, "day", start, end, limit=50000))
    tlt = pd.DataFrame({
        # FIX 4: utcfromtimestamp is deprecated from 3.12
        "date": [datetime.fromtimestamp(a.timestamp / 1000, timezone.utc).date() for a in aggs],
        "tlt": [a.close for a in aggs],
        "tlt_low": [a.low for a in aggs],
        "tlt_high": [a.high for a in aggs],
    }).drop_duplicates("date").set_index("date").sort_index()
    ys = list(client.list_treasury_yields(date_gte=start, date_lte=end, limit=50000,
                                          sort="date", order="asc"))
    ydf = pd.DataFrame({
        "date": [pd.to_datetime(y.date).date() for y in ys],
        "y30": [y.yield_30_year for y in ys],
        "y10": [y.yield_10_year for y in ys],
    }).dropna(subset=["y30"]).drop_duplicates("date").set_index("date").sort_index()
    df = tlt.join(ydf, how="inner")
    df["ub"] = np.nan
    if df.empty:
        raise RuntimeError("polygon: no overlapping TLT/yield sessions")
    return df, notes


def load_data(start: str):
    try:
        import yfinance  # noqa: F401
        return load_yfinance(start)
    except Exception as e1:
        try:
            return load_polygon(start)
        except Exception as e2:
            raise RuntimeError(f"yfinance: {e1}; polygon: {e2}") from e2


@dataclass
class Scan:
    date: str
    tlt: float
    ub: float | None
    y30: float
    y10: float
    score: int
    regime: str
    regime_pts: float
    call_stack: int
    put_stack: int
    verdict: str
    tier: str
    warn: str
    notes: list[str] = field(default_factory=list)


def scan(df: pd.DataFrame, notes: list[str] | None = None) -> Scan:
    notes = list(notes or [])
    d = df.copy()
    if len(d) < 60:
        raise ValueError(f"need at least 60 sessions, got {len(d)}")

    for c in ("tlt_low", "tlt_high"):
        if c not in d.columns:
            d[c] = np.nan
    have_hl = d["tlt_low"].notna().sum() > 60 and not np.allclose(
        d["tlt_low"].fillna(d["tlt"]), d["tlt"], equal_nan=True
    )
    if not have_hl:
        d["tlt_low"] = d["tlt"]
        d["tlt_high"] = d["tlt"]
        notes.append("HIGH/LOW missing — 2 stack bits degraded")

    d["tlt_e9"], d["tlt_e21"] = ema(d["tlt"], 9), ema(d["tlt"], 21)
    d["tlt_s50"], d["tlt_s200"] = sma(d["tlt"], 50), sma(d["tlt"], 200)
    d["tlt_rsi"], d["tlt_hist"] = rsi(d["tlt"]), macd_hist(d["tlt"])

    have_ub = d["ub"].notna().sum() > 60
    if have_ub:
        d["ub_e9"], d["ub_e21"] = ema(d["ub"], 9), ema(d["ub"], 21)
        d["ub_s50"], d["ub_s200"] = sma(d["ub"], 50), sma(d["ub"], 200)
        d["ub_hist"] = macd_hist(d["ub"])
    else:
        d["ub"] = np.nan

    d["y_s50"], d["y_s200"] = sma(d["y30"], 50), sma(d["y30"], 200)
    d["y_hist"] = macd_hist(d["y30"])
    d["spread"] = d["y30"] - d["y10"]
    d["spread_s20"] = sma(d["spread"], 20)

    last, prev = d.iloc[-1], d.iloc[-2]
    look = d.iloc[-16:]
    ub_up = bool(have_ub and ((last.ub_hist > prev.ub_hist) or (last.ub_e9 > last.ub_e21)))
    ub_dn = bool(have_ub and ((last.ub_hist < prev.ub_hist) or (last.ub_e9 < last.ub_e21)))
    ub_gt50 = bool(have_ub and last.ub > last.ub_s50)
    ub_lt50 = bool(have_ub and last.ub < last.ub_s50)

    call_stack = int(sum([
        last.tlt_e9 > last.tlt_e21,
        last.tlt_hist > prev.tlt_hist,
        last.tlt > last.tlt_s50,
        last.tlt_s50 > d["tlt_s50"].iloc[-6],
        last.tlt_low > look["tlt_low"].iloc[:-1].min(),
        ub_up,
        ub_gt50,
        last.y30 < last.y_s50,
    ]))
    put_stack = int(sum([
        last.tlt_e9 < last.tlt_e21,
        last.tlt_hist < prev.tlt_hist,
        last.tlt < last.tlt_s50,
        last.tlt_s50 <= d["tlt_s50"].iloc[-6],
        last.tlt_high < look["tlt_high"].iloc[:-1].max(),
        ub_dn,
        ub_lt50,
        last.y30 > last.y_s50,
    ]))
    if not have_ub:
        notes.append("UB absent — both stacks max out at 6/8, so CONFIRMED (>=5) is harder")

    conds = [last.tlt > last.tlt_s50, last.tlt > last.tlt_s200,
             last.tlt_s50 > d["tlt_s50"].iloc[-6], last.tlt_s50 > last.tlt_s200]
    if have_ub:
        conds += [last.ub > last.ub_s50, last.ub > last.ub_s200]
    conds += [last.y30 < last.y_s50, last.y30 < last.y_s200]
    raw_pts = sum(12.5 if c else -12.5 for c in conds)
    # FIX 2: without UB the raw sum spans only +/-75, so a fixed +/-25 threshold
    # silently became stricter. Normalise to the full-scale equivalent.
    pts = raw_pts * REGIME_TERMS / len(conds)
    regime = "BULL" if pts > 25 else "BEAR" if pts < -25 else "TRANS"

    rsi_was_os = d["tlt_rsi"].iloc[-10:].min() < 32
    # FIX 1: `last > 35 <= prev` chains to (last > 35) and (35 <= prev), which
    # requires RSI to have been ALREADY above 35 and excludes every real cross.
    call_bounce = bool(
        rsi_was_os and last.tlt_rsi > 35 and prev.tlt_rsi <= 35
        and (last.tlt > last.tlt_e9 or last.tlt_hist > prev.tlt_hist > d["tlt_hist"].iloc[-3])
    )
    put_fade = bool(
        d["tlt_rsi"].iloc[-10:].max() >= 70
        and last.tlt < last.tlt_s50 <= prev.tlt
        and last.y30 > last.y_s50
    )
    bear_steep = bool(last.spread > last.spread_s20 and last.y30 > d["y30"].iloc[-6])

    score = 0
    score += (2 if last.y30 < last.y_s50 and last.y30 < prev.y30
              else -2 if last.y30 > last.y_s50 and last.y30 > prev.y30 else 0)
    if have_ub:
        score += 2 if last.ub > last.ub_s50 else -2 if last.ub < last.ub_s50 else 0
        score += (1 if last.ub > prev.ub and last.tlt < prev.tlt
                  else -1 if last.ub < prev.ub and last.tlt > prev.tlt else 0)
    score += 1 if last.tlt > last.tlt_e21 else -1
    score += 2 if last.tlt > last.tlt_s50 else -2
    score += 1 if last.tlt > last.tlt_s200 else -1
    score += 2 if call_bounce else -2 if put_fade else 0
    score += -1 if bear_steep else 0

    agree_call = bool(last.y30 < last.y_s50 and (ub_gt50 if have_ub else True))
    agree_put = bool(last.y30 > last.y_s50 and (ub_lt50 if have_ub else True))

    if score >= CALL_THRESH and agree_call:
        confirmed = call_stack >= 5 or last.tlt > last.tlt_s200
        verdict = "BUY TLT CALLS" if confirmed else "SCOUT TLT CALLS"
        tier = f"{'CONFIRMED' if confirmed else 'SCOUT'} {call_stack}/8"
    elif score <= PUT_THRESH and agree_put:
        confirmed = put_stack >= 5
        verdict = "BUY TLT PUTS" if confirmed else "SCOUT TLT PUTS"
        tier = f"{'CONFIRMED' if confirmed else 'SCOUT'} {put_stack}/8"
    elif call_bounce and regime != "BULL":
        verdict, tier = "RENT CALL BOUNCE", "mean-reversion"
    elif put_fade:
        verdict, tier = "FADE / SCOUT PUTS", "overbought reject"
    else:
        verdict, tier = "STAND ASIDE", "-"

    caution = (int(have_ub and last.ub < last.ub_e21)
               + int(last.y_hist > prev.y_hist > d["y_hist"].iloc[-3])
               + int(bear_steep and "CALL" in verdict)
               + int(not have_ub and verdict != "STAND ASIDE"))
    dy = last.y30 - d["y30"].iloc[-6]
    actual = (last.tlt / d["tlt"].iloc[-6] - 1) * 100
    notes += [
        f"5-session Δy30={dy:+.3f}pp  implied ΔTLT≈{-DURATION * dy:+.1f}%  "
        f"actual ΔTLT={actual:+.1f}%  residual={actual + DURATION * dy:+.1f}%",
        f"10s30s={last.spread:.3f}  vs20={last.spread - last.spread_s20:+.3f}  "
        f"{'BEAR-STEEP' if bear_steep else 'ok'}",
        f"TLT={last.tlt:.2f}  50={last.tlt_s50:.2f}  200={last.tlt_s200:.2f}  RSI={last.tlt_rsi:.1f}",
        f"30Y={last.y30:.3f}  50={last.y_s50:.3f}  10Y={last.y10:.3f}",
        f"score range {SCORE_MIN if have_ub else SCORE_MIN_NOUB:+d}.."
        f"{SCORE_MAX if have_ub else SCORE_MAX_NOUB:+d} (bear-steepener only subtracts)",
    ]
    return Scan(
        date=str(d.index[-1]), tlt=float(last.tlt),
        ub=None if not have_ub else float(last.ub),
        y30=float(last.y30), y10=float(last.y10),
        score=int(score), regime=regime, regime_pts=float(pts),
        call_stack=call_stack, put_stack=put_stack,
        verdict=verdict, tier=tier,
        warn="CAUTION" if caution >= 2 else "—", notes=notes,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--start", default=(datetime.now() - timedelta(days=800)).strftime("%Y-%m-%d"))
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()
    df, notes = load_data(args.start)
    s = scan(df, notes)
    if args.json:
        import json
        from dataclasses import asdict
        print(json.dumps(asdict(s), indent=2, default=str))
        return
    print("=" * 64)
    print(f"TLT DURATION SCANNER   {s.date}")
    print("=" * 64)
    print(f"VERDICT : {s.verdict}")
    print(f"TIER    : {s.tier}")
    print(f"SCORE   : {s.score:+d}   (calls>={CALL_THRESH}  puts<={PUT_THRESH})")
    print(f"REGIME  : {s.regime}  ({s.regime_pts:+.1f} / ±100)")
    print(f"STACK   : calls {s.call_stack}/8   puts {s.put_stack}/8")
    print(f"WARN    : {s.warn}")
    print(f"TLT {s.tlt:.2f}   UB {f'{s.ub:.4f}' if s.ub is not None else 'n/a'}   "
          f"30Y {s.y30:.3f}   10Y {s.y10:.3f}")
    print("-" * 64)
    for n in s.notes:
        print("•", n)
    print("=" * 64)
    print("Decision support only. Daily close; act next session.")


if __name__ == "__main__":
    main()
