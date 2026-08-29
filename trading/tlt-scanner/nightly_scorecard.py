#!/usr/bin/env python3
"""TLT duration scanner — nightly CALLS / PUTS / STAND ASIDE scorecard.

A standalone, single-file scorecard: weighted score, regime, both stacks, one
verdict. It shares no code with tlt_scanner.py and can disagree with it — the
weights here are coarser and the verdict vocabulary is finer. Use tlt_scanner.py
for levels, stops and the backtest; use this for a fast nightly read.

Four fixes against the original draft, each marked FIX below:
  1. raw prices, not auto_adjust — back-adjustment drags a moving average low in
     proportion to its lookback (~1.7% on TLT's 200-day), moving regime flips
  2. real High/Low, so the swing-low / swing-high stack conditions mean something
  3. no double-count when UB is missing — the inverted-yield proxy is
     mathematically identical to the yield test, so scoring both counted one fact
     twice and turned the two-source AND gate into a single test
  4. put_fade could not fire: it required RSI >= 70 on a close already below the
     50-day, which is near-impossible (0 hits in 3000 synthetic bars)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

CALL_THRESH = 5
PUT_THRESH = -5
DURATION = 15.0  # first-order TLT effective duration fallback
SCORE_MAX = 11   # +2 yield +2 UB +1 e21 +2 s50 +1 s200 +2 trigger +1 lead
SCORE_MIN = -12  # the same, plus -1 for a bear steepener: the scale is asymmetric


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0.0)
    down = -d.clip(upper=0.0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd_hist(s: pd.Series) -> pd.Series:
    line = ema(s, 12) - ema(s, 26)
    return line - ema(line, 9)


def load_yfinance(start: str) -> tuple[pd.DataFrame, list[str]]:
    import yfinance as yf

    notes = ["source=yfinance (raw prices)"]
    # FIX 1: auto_adjust=False. Back-adjustment rewrites every historical bar for
    # dividends, so the 50/200-day sit low by roughly half the payout across the
    # window and a regime flip prints before any chart shows it.
    raw = yf.download(
        ["TLT", "UB=F", "^TYX", "^TNX"],
        start=start,
        auto_adjust=False,
        progress=False,
        group_by="ticker",
        threads=True,
    )

    def field(ticker: str, col: str) -> pd.Series:
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker not in raw.columns.get_level_values(0):
                return pd.Series(dtype=float)
            df = raw[ticker]
        else:
            df = raw
        if col not in df.columns:
            return pd.Series(dtype=float)
        s = df[col].copy()
        s.name = f"{ticker}_{col}"
        return s.dropna()

    tlt = field("TLT", "Close")
    if tlt.empty:
        raise RuntimeError("yfinance returned no TLT data")
    ub, y30, y10 = field("UB=F", "Close"), field("^TYX", "Close"), field("^TNX", "Close")
    if ub.empty:
        notes.append("UB=F missing")
    if y30.empty:
        notes.append("^TYX missing")
    # FIX 2: carry real highs and lows; the swing conditions are meaningless
    # when both are approximated by the close.
    df = pd.DataFrame({
        "tlt": tlt,
        "tlt_high": field("TLT", "High"),
        "tlt_low": field("TLT", "Low"),
        "ub": ub, "y30": y30, "y10": y10,
    }).dropna(subset=["tlt"])
    return df, notes


def load_polygon(start: str) -> tuple[pd.DataFrame, list[str]]:
    from polygon import RESTClient

    notes = ["source=polygon", "UB unavailable on this plan — yield used as rates tape only"]
    client = RESTClient()
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    aggs = list(client.list_aggs("TLT", 1, "day", start, end, limit=50000))
    tlt = pd.DataFrame({
        "date": [datetime.fromtimestamp(a.timestamp / 1000, timezone.utc).date() for a in aggs],
        "tlt": [a.close for a in aggs],
        "tlt_low": [a.low for a in aggs],
        "tlt_high": [a.high for a in aggs],
    }).drop_duplicates("date").set_index("date").sort_index()

    ys = list(client.list_treasury_yields(
        date_gte=start, date_lte=end, limit=50000, sort="date", order="asc"))
    ydf = pd.DataFrame({
        "date": [pd.to_datetime(y.date).date() for y in ys],
        "y30": [y.yield_30_year for y in ys],
        "y10": [y.yield_10_year for y in ys],
    }).dropna(subset=["y30"]).drop_duplicates("date").set_index("date").sort_index()
    df = tlt.join(ydf, how="inner")
    df["ub"] = np.nan
    return df, notes


def load_data(start: str) -> tuple[pd.DataFrame, list[str]]:
    try:
        import yfinance  # noqa: F401

        return load_yfinance(start)
    except Exception as exc:
        try:
            return load_polygon(start)
        except Exception as exc2:
            raise RuntimeError(f"yfinance failed ({exc}); polygon failed ({exc2})") from exc2


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
    notes: list[str]


def scan(df: pd.DataFrame, notes: list[str] | None = None) -> Scan:
    notes = list(notes or [])  # never mutate the caller's list
    d = df.copy()
    for col in ("tlt_low", "tlt_high"):
        if col not in d.columns or d[col].isna().all():
            d[col] = d["tlt"]
            notes.append(f"{col} unavailable — swing conditions degraded to closes")

    d["tlt_e9"] = ema(d["tlt"], 9)
    d["tlt_e21"] = ema(d["tlt"], 21)
    d["tlt_s50"] = sma(d["tlt"], 50)
    d["tlt_s200"] = sma(d["tlt"], 200)
    d["tlt_rsi"] = rsi(d["tlt"])
    d["tlt_hist"] = macd_hist(d["tlt"])

    have_ub = bool(d["ub"].notna().sum() > 60)
    if have_ub:
        base = d["ub"]
    else:
        # FIX 3: the proxy stays for display, but scores nothing. -y30 vs its own
        # 50-day is algebraically the same test as y30 vs its 50-day, so scoring
        # both counted one fact twice and made the two-source gate tautological.
        base = -d["y30"]
        notes.append("UB missing — inverted-30Y proxy shown but SCORED ZERO "
                     "(it is the same test as the yield leg, not a second source)")
    d["ub"] = base
    d["ub_e9"], d["ub_e21"] = ema(base, 9), ema(base, 21)
    d["ub_s50"], d["ub_s200"] = sma(base, 50), sma(base, 200)
    d["ub_hist"] = macd_hist(base)

    d["y_s50"] = sma(d["y30"], 50)
    d["y_s200"] = sma(d["y30"], 200)
    d["y_hist"] = macd_hist(d["y30"])
    d["spread"] = d["y30"] - d["y10"]
    d["spread_s20"] = sma(d["spread"], 20)

    last, prev = d.iloc[-1], d.iloc[-2]
    look = d.iloc[-16:]

    call_stack = int(sum([
        last.tlt_e9 > last.tlt_e21,
        last.tlt_hist > prev.tlt_hist,
        last.tlt > last.tlt_s50,
        last.tlt_s50 > d["tlt_s50"].iloc[-6],
        last.tlt_low > look["tlt_low"].iloc[:-1].min(),
        (last.ub_hist > prev.ub_hist) or (last.ub_e9 > last.ub_e21),
        last.ub > last.ub_s50,
        last.y30 < last.y_s50,
    ]))
    put_stack = int(sum([
        last.tlt_e9 < last.tlt_e21,
        last.tlt_hist < prev.tlt_hist,
        last.tlt < last.tlt_s50,
        last.tlt_s50 <= d["tlt_s50"].iloc[-6],
        last.tlt_high < look["tlt_high"].iloc[:-1].max(),
        (last.ub_hist < prev.ub_hist) or (last.ub_e9 < last.ub_e21),
        last.ub < last.ub_s50,
        last.y30 > last.y_s50,
    ]))

    pts = 0.0
    for cond in (last.tlt > last.tlt_s50, last.tlt > last.tlt_s200,
                 last.tlt_s50 > d["tlt_s50"].iloc[-6], last.tlt_s50 > last.tlt_s200,
                 last.ub > last.ub_s50, last.ub > last.ub_s200,
                 last.y30 < last.y_s50, last.y30 < last.y_s200):
        pts += 12.5 if cond else -12.5
    regime = "BULL" if pts > 25 else "BEAR" if pts < -25 else "TRANS"

    rsi_was_os = d["tlt_rsi"].iloc[-10:].min() < 32
    call_bounce = bool(
        rsi_was_os and last.tlt_rsi > 35 and prev.tlt_rsi <= 35
        and (last.tlt > last.tlt_e9 or (last.tlt_hist > prev.tlt_hist > d["tlt_hist"].iloc[-3]))
    )
    # FIX 4: a melt-up fade is "was overbought, now rejecting the 50-day, with
    # yields trending up" — not "overbought while already below the 50-day",
    # which never happens.
    rsi_was_ob = bool(d["tlt_rsi"].iloc[-10:].max() >= 70)
    fresh_reject = bool(last.tlt < last.tlt_s50 <= prev.tlt)
    put_fade = bool(rsi_was_ob and fresh_reject and last.y30 > last.y_s50)

    bear_steep = bool(last.spread > last.spread_s20 and last.y30 > d["y30"].iloc[-6])

    score = 0
    score += (2 if last.y30 < last.y_s50 and last.y30 < prev.y30
              else -2 if last.y30 > last.y_s50 and last.y30 > prev.y30 else 0)
    if have_ub:  # FIX 3: the proxy contributes nothing
        score += 2 if last.ub > last.ub_s50 else -2
    score += 1 if last.tlt > last.tlt_e21 else -1
    score += 2 if last.tlt > last.tlt_s50 else -2
    score += 1 if last.tlt > last.tlt_s200 else -1
    score += 2 if call_bounce else -2 if put_fade else 0
    score += -1 if bear_steep else 0
    if have_ub:
        score += (1 if last.ub > prev.ub and last.tlt < prev.tlt
                  else -1 if last.ub < prev.ub and last.tlt > prev.tlt else 0)

    # FIX 3: without a real futures leg the gate has one source, and says so
    agree_call = bool(last.y30 < last.y_s50 and (not have_ub or last.ub > last.ub_s50))
    agree_put = bool(last.y30 > last.y_s50 and (not have_ub or last.ub < last.ub_s50))

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

    caution = 0
    caution += int(have_ub and last.ub < last.ub_e21)
    caution += int(last.y_hist > prev.y_hist > d["y_hist"].iloc[-3])
    caution += int(bear_steep and "CALL" in verdict)
    caution += int(not have_ub and verdict != "STAND ASIDE")
    warn = "CAUTION" if caution >= 2 else "—"

    dy = last.y30 - d["y30"].iloc[-6]  # 30y yield is quoted in percent: 5.19 == 5.19%
    implied = -DURATION * dy           # so dy is already in percentage points
    actual = (last.tlt / d["tlt"].iloc[-6] - 1) * 100
    notes.append(f"5-session Δy30={dy:+.3f}pp  duration-implied ΔTLT≈{implied:+.1f}%  "
                 f"actual ΔTLT={actual:+.1f}%  residual={actual - implied:+.1f}%")
    notes.append(f"10s30s={last.spread:.3f}  vs20={last.spread - last.spread_s20:+.3f}  "
                 f"{'BEAR-STEEP' if bear_steep else 'ok'}")
    notes.append(f"TLT={last.tlt:.2f}  50={last.tlt_s50:.2f}  200={last.tlt_s200:.2f}  "
                 f"RSI={last.tlt_rsi:.1f}")
    notes.append(f"30Y={last.y30:.3f}  50={last.y_s50:.3f}  10Y={last.y10:.3f}")

    return Scan(
        date=str(d.index[-1]), tlt=float(last.tlt),
        ub=float(df["ub"].iloc[-1]) if have_ub else None,
        y30=float(last.y30), y10=float(last.y10), score=int(score),
        regime=regime, regime_pts=float(pts), call_stack=call_stack, put_stack=put_stack,
        verdict=verdict, tier=tier, warn=warn, notes=notes,
    )


def main() -> None:
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
    print(f"SCORE   : {s.score:+d}   (calls>={CALL_THRESH}  puts<={PUT_THRESH}  "
          f"range {SCORE_MIN:+d}..{SCORE_MAX:+d})")
    print(f"REGIME  : {s.regime}  ({s.regime_pts:+.1f})")
    print(f"STACK   : calls {s.call_stack}/8   puts {s.put_stack}/8")
    print(f"WARN    : {s.warn}")
    print(f"TLT {s.tlt:.2f}   UB {f'{s.ub:.4f}' if s.ub is not None else 'n/a'}   "
          f"30Y {s.y30:.3f}   10Y {s.y10:.3f}")
    print("-" * 64)
    for n in s.notes:
        print("•", n)
    print("=" * 64)
    print("Decision support only. Signals on daily close; act next session.")


if __name__ == "__main__":
    main()
