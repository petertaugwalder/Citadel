#!/usr/bin/env python3
"""
tlt_scanner.py — terminal scanner for swing-trading TLT (iShares 20+ Year Treasury ETF).

Three-layer model:
  1. REGIME   — where are we in the bond cycle? Scored -100..+100 from moving-average
                structure on TLT, ZB futures (30y T-Bond), and the 30y yield (^TYX, inverted).
  2. TRIGGERS — two buy families:
                  * BOUNCE  — mean-reversion long off an oversold hook (valid in any regime,
                              rented back to the moving-average band in a bear regime).
                  * TREND-TURN STACK — 8 confirmation conditions across TLT / ZB / ^TYX that
                              light up as a downtrend actually reverses. Tiers: SCOUT >= 3,
                              CONFIRMED >= 5, REGIME FLIP = close over the 200-day.
  3. CROSS-CHECKS — futures/cash divergence, yield-exhaustion divergence, and the
                commodity (DBA) inflation tape as a structural headwind flag.
  4. EXIT ENGINE — the sell signal, evaluated "as if long": trail breaks (21-EMA for
                rentals/swings, 50-day once the regime flipped), a hard structure stop
                (close under the prior 15-day low), trim-into-strength triggers, and
                early warnings from ZB futures and the 30y yield. Pass --entry to see
                your open P&L and R-multiple against the current stop.

Data: Yahoo Finance daily bars via yfinance (cached locally). This is an end-of-day /
swing tool, not an intraday one. Nothing here is financial advice.

Usage:
  python tlt_scanner.py                 # one-shot scan, live data
  python tlt_scanner.py --demo          # synthetic data (no network) to see the output
  python tlt_scanner.py --history 15    # what the scanner said each of the last 15 sessions
  python tlt_scanner.py --watch 900     # re-scan every 15 min; --notify adds macOS alerts
  python tlt_scanner.py --explain       # print the trading logic
  python tlt_scanner.py --json          # machine-readable output
  python tlt_scanner.py --account 50000 --risk 1.0   # position sizing
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

TICKERS = {
    "TLT": "TLT",     # trade vehicle: 20+yr Treasury ETF — the only thing traded
    "ZB": "ZB=F",     # 30y T-Bond futures: the leading tape
    "TYX": "^TYX",    # 30y yield index (drives TLT, inverted)
    "DBA": "DBA",     # ag/commodity tape: inflation-pressure flag
}
CACHE_DIR = Path.home() / ".cache" / "tlt-scanner"
CACHE_TTL_SEC = 4 * 3600
HISTORY_PERIOD = "2y"

# ----------------------------------------------------------------------------- indicators


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    avg_up = up.ewm(alpha=1 / n, adjust=False).mean()
    avg_dn = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_up / avg_dn.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Attach every indicator series the analysis layers need."""
    out = df.copy()
    c = out["Close"]
    out["sma50"] = c.rolling(50).mean()
    out["sma200"] = c.rolling(200).mean()
    out["ema9"] = ema(c, 9)
    out["ema21"] = ema(c, 21)
    out["rsi14"] = rsi(c, 14)
    macd_line = ema(c, 12) - ema(c, 26)
    out["macd"] = macd_line
    out["macd_sig"] = ema(macd_line, 9)
    out["macd_hist"] = macd_line - out["macd_sig"]
    out["atr14"] = atr(out, 14)
    out["roc20"] = c.pct_change(20) * 100
    out["chg1"] = c.pct_change(1) * 100
    return out


def pivot_points(series: pd.Series, w: int = 4, kind: str = "low") -> pd.Series:
    """Confirmed swing pivots: bar is the extreme of a +/- w window (last w bars unconfirmed)."""
    roll = series.rolling(2 * w + 1, center=True)
    extreme = roll.min() if kind == "low" else roll.max()
    piv = series[series == extreme].dropna()
    if len(piv) and w > 0:
        piv = piv.iloc[: len(series) if len(series) <= w else None]
        piv = piv[piv.index <= series.index[-(w + 1)]]
    # collapse pivots closer than w bars (flat bottoms) to one
    keep, last_pos = [], -10**9
    positions = series.index.get_indexer(piv.index)
    for pos, (idx, val) in zip(positions, piv.items()):
        if pos - last_pos <= w and keep:
            if (kind == "low" and val <= keep[-1][1]) or (kind == "high" and val >= keep[-1][1]):
                keep[-1] = (idx, val)
        else:
            keep.append((idx, val))
        last_pos = pos
    return pd.Series({k: v for k, v in keep})


def to_32nds(x: float) -> str:
    whole = int(x)
    ticks = int(round((x - whole) * 32))
    if ticks == 32:
        whole, ticks = whole + 1, 0
    return f"{whole}'{ticks:02d}"


# ----------------------------------------------------------------------------- data layer


def fetch_yahoo(symbol: str) -> pd.DataFrame | None:
    import logging

    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    try:
        df = yf.download(symbol, period=HISTORY_PERIOD, interval="1d",
                         auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty:
            df = yf.Ticker(symbol).history(period=HISTORY_PERIOD, interval="1d", auto_adjust=True)
    except Exception as exc:  # network / proxy / API failures degrade per-ticker
        print(f"  ! fetch failed for {symbol}: {exc}", file=sys.stderr)
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    cols = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
    if "Close" not in cols:
        return None
    df = df[cols].dropna(subset=["Close"])
    for c in ("Open", "High", "Low"):
        if c not in df.columns:
            df[c] = df["Close"]
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def load_frames(refresh: bool = False) -> dict[str, pd.DataFrame]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    frames: dict[str, pd.DataFrame] = {}
    for name, symbol in TICKERS.items():
        cache = CACHE_DIR / f"{name}.csv"
        df = None
        if cache.exists() and not refresh and (time.time() - cache.stat().st_mtime) < CACHE_TTL_SEC:
            df = pd.read_csv(cache, index_col=0, parse_dates=True)
        if df is None or df.empty:
            df = fetch_yahoo(symbol)
            if df is not None:
                df.to_csv(cache)
            elif cache.exists():  # stale cache beats nothing
                df = pd.read_csv(cache, index_col=0, parse_dates=True)
        if df is not None and len(df) >= 60:
            frames[name] = enrich(df)
    return frames


def _demo_walk(rng, n, start, segments, vol):
    """Random walk stitched from (length_fraction, total_drift) segments."""
    lengths = [max(1, int(n * frac)) for frac, _ in segments]
    lengths[-1] += n - sum(lengths)
    rets = []
    for m, (_, drift) in zip(lengths, segments):
        noise = rng.normal(0, vol, m)
        noise -= noise.mean()  # keep each segment's total drift exact
        rets.append(np.full(m, drift / m) + noise)
    r = np.concatenate(rets)[:n]
    close = start * np.exp(np.cumsum(r))
    noise = np.abs(rng.normal(0, vol, n))
    df = pd.DataFrame(
        {
            "Open": np.r_[start, close[:-1]],
            "High": close * (1 + noise),
            "Low": close * (1 - noise),
            "Close": close,
        },
        index=pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n),
    )
    return df


def demo_frames() -> dict[str, pd.DataFrame]:
    """Synthetic 2y of data shaped like the Aug-2026 tape: bond bear market + fresh bounce."""
    rng = np.random.default_rng(7)
    n = 520
    shapes = {
        "TLT": (102.0, [(0.55, -0.13), (0.25, -0.05), (0.17, -0.055), (0.03, 0.028)], 0.0055),
        "ZB": (128.0, [(0.55, -0.10), (0.25, -0.04), (0.17, -0.045), (0.03, 0.018)], 0.0045),
        "TYX": (4.10, [(0.55, 0.16), (0.25, 0.05), (0.17, 0.055), (0.03, -0.021)], 0.0065),
        "DBA": (26.0, [(0.70, 0.02), (0.15, -0.03), (0.15, 0.105)], 0.0060),
    }
    return {k: enrich(_demo_walk(rng, n, s, seg, v)) for k, (s, seg, v) in shapes.items()}


# ----------------------------------------------------------------------------- analysis


def _last(df: pd.DataFrame, col: str) -> float:
    v = df[col].iloc[-1]
    return float(v) if pd.notna(v) else float("nan")


def regime_components(frames: dict) -> list[dict]:
    """MA-structure conditions, weighted. TYX is inverted (falling yields = bond bullish)."""
    comps = []

    def add(name, value, weight):
        comps.append({"name": name, "bullish": bool(value) if value is not None else None, "weight": weight})

    for key, label, invert in (("TLT", "TLT", False), ("ZB", "ZB futures", False), ("TYX", "30y yield", True)):
        df = frames.get(key)
        if df is None or pd.isna(_last(df, "sma200")):
            for suffix, w in ((" vs 200d", 2), (" vs 50d", 1), (" 50d slope", 1), (" 50d vs 200d", 1)):
                add(label + suffix, None, w)
            continue
        c, s50, s200 = _last(df, "Close"), _last(df, "sma50"), _last(df, "sma200")
        s50_prev = float(df["sma50"].iloc[-11]) if len(df) > 11 and pd.notna(df["sma50"].iloc[-11]) else s50
        cond = [c > s200, c > s50, s50 > s50_prev, s50 > s200]
        if invert:
            cond = [not x for x in cond]
        add(f"{label} vs 200d", cond[0], 2)
        add(f"{label} vs 50d", cond[1], 1)
        add(f"{label} 50d slope", cond[2], 1)
        add(f"{label} 50d vs 200d", cond[3], 1)
    return comps


def regime_score(comps: list[dict]) -> tuple[float, str]:
    avail = [c for c in comps if c["bullish"] is not None]
    if not avail:
        return 0.0, "UNKNOWN"
    total = sum(c["weight"] for c in avail)
    score = sum(c["weight"] if c["bullish"] else -c["weight"] for c in avail) / total * 100
    label = "BULLISH" if score > 33 else "BEARISH" if score < -33 else "TRANSITION"
    return round(score, 1), label


def bounce_series(tlt: pd.DataFrame) -> pd.Series:
    """Mean-reversion long trigger: oversold within 10 bars + RSI hook + price/momentum recovery."""
    r = tlt["rsi14"]
    oversold_recent = (r < 32).rolling(10, min_periods=1).max() == 1
    hook = (r > r.shift(1)) & (r > 35)
    price_ok = tlt["Close"] > tlt["ema9"]
    mom_up = (tlt["macd_hist"] > tlt["macd_hist"].shift(1)) & (
        tlt["macd_hist"].shift(1) > tlt["macd_hist"].shift(2)
    )
    return (oversold_recent & hook & (price_ok | mom_up)).fillna(False)


def bounce_state(tlt: pd.DataFrame) -> dict:
    sig = bounce_series(tlt)
    fresh = sig & ~sig.shift(1, fill_value=False)
    fresh_dates = list(sig.index[fresh])
    last_trigger = fresh_dates[-1] if fresh_dates else None
    days_since = int((len(sig) - 1) - sig.index.get_loc(last_trigger)) if last_trigger is not None else None
    managing = (
        last_trigger is not None
        and days_since is not None
        and days_since <= 15
        and _last(tlt, "Close") > _last(tlt, "ema21")
    )
    return {
        "triggered_today": bool(sig.iloc[-1]),
        "last_trigger": str(last_trigger.date()) if last_trigger is not None else None,
        "days_since": days_since,
        "active": bool(sig.iloc[-1] or managing),
    }


def trend_stack(frames: dict) -> list[dict]:
    """The 8-condition regime-turn checklist across TLT, ZB and the 30y yield."""
    tlt, zb, tyx = frames.get("TLT"), frames.get("ZB"), frames.get("TYX")
    items: list[dict] = []

    def add(name, val):
        items.append({"name": name, "on": bool(val) if val is not None else None})

    if tlt is not None:
        add("TLT 9-EMA > 21-EMA", _last(tlt, "ema9") > _last(tlt, "ema21"))
        add("TLT MACD > signal", _last(tlt, "macd") > _last(tlt, "macd_sig"))
        add("TLT close > 50-day", _last(tlt, "Close") > _last(tlt, "sma50"))
        s50 = tlt["sma50"].dropna()
        add("TLT 50-day rising", len(s50) > 10 and float(s50.iloc[-1]) > float(s50.iloc[-11]))
        lows = pivot_points(tlt["Low"], w=4, kind="low")
        add("TLT higher swing low", len(lows) >= 2 and float(lows.iloc[-1]) > float(lows.iloc[-2]))
    else:
        for name in ("TLT 9-EMA > 21-EMA", "TLT MACD > signal", "TLT close > 50-day",
                     "TLT 50-day rising", "TLT higher swing low"):
            add(name, None)
    add(
        "ZB 9>21 EMA + MACD cross",
        None if zb is None else (_last(zb, "ema9") > _last(zb, "ema21") and _last(zb, "macd") > _last(zb, "macd_sig")),
    )
    add("ZB close > 50-day", None if zb is None else _last(zb, "Close") > _last(zb, "sma50"))
    add("30y yield < its 50-day", None if tyx is None else _last(tyx, "Close") < _last(tyx, "sma50"))
    return items


def stack_tier(items: list[dict], tlt: pd.DataFrame | None) -> tuple[int, str]:
    lit = sum(1 for i in items if i["on"])
    if tlt is not None and pd.notna(_last(tlt, "sma200")) and _last(tlt, "Close") > _last(tlt, "sma200") and lit >= 5:
        return lit, "REGIME FLIP"
    if lit >= 5:
        return lit, "CONFIRMED"
    if lit >= 3:
        return lit, "SCOUT"
    return lit, "NONE"


def divergences(frames: dict) -> list[str]:
    notes = []
    tlt, tyx, zb = frames.get("TLT"), frames.get("TYX"), frames.get("ZB")
    if tlt is not None:
        lows = pivot_points(tlt["Low"], w=4, kind="low")
        if len(lows) >= 2:
            d1, d2 = lows.index[-2], lows.index[-1]
            if float(lows.iloc[-1]) < float(lows.iloc[-2]) and float(tlt["rsi14"].loc[d2]) > float(tlt["rsi14"].loc[d1]):
                notes.append("TLT bullish divergence: lower price low, higher RSI low (selling exhausting)")
    if tyx is not None:
        highs = pivot_points(tyx["High"], w=4, kind="high")
        if len(highs) >= 2:
            d1, d2 = highs.index[-2], highs.index[-1]
            if float(highs.iloc[-1]) > float(highs.iloc[-2]) and float(tyx["rsi14"].loc[d2]) < float(tyx["rsi14"].loc[d1]):
                notes.append("30y yield exhaustion: higher yield high on weaker RSI (bond bullish)")
    if tlt is not None and zb is not None:
        r_tlt, r_zb = _last(tlt, "roc20"), _last(zb, "roc20")
        if not (np.isnan(r_tlt) or np.isnan(r_zb)) and np.sign(r_tlt) != np.sign(r_zb):
            leader = "futures leading UP" if r_zb > r_tlt else "futures leading DOWN"
            notes.append(f"ZB / TLT 20-day momentum disagree ({leader}) — futures usually resolve it")
    return notes


def macro_checks(frames: dict) -> list[str]:
    notes = []
    dba = frames.get("DBA")
    if dba is not None:
        r = _last(dba, "roc20")
        if not np.isnan(r):
            if r > 4:
                notes.append(f"Inflation tape: DBA +{r:.1f}%/20d — hot commodities pressure yields UP; cap bounce targets")
            elif r < -4:
                notes.append(f"Inflation tape: DBA {r:.1f}%/20d — commodity disinflation is a TLT tailwind")
            else:
                notes.append(f"Inflation tape: DBA {r:+.1f}%/20d — neutral")
    return notes


def action_and_levels(frames: dict, res: dict, account: float | None, risk_pct: float) -> dict:
    tlt = frames["TLT"]
    close = _last(tlt, "Close")
    a = _last(tlt, "atr14")
    swing_low = float(tlt["Low"].rolling(15).min().iloc[-1])
    stop = swing_low - 0.5 * a
    s50, s200 = _last(tlt, "sma50"), _last(tlt, "sma200")
    risk_per_share = max(close - stop, 0.01)

    tier = res["stack"]["tier"]
    bounce = res["bounce"]
    regime = res["regime"]["label"]
    e21 = _last(tlt, "ema21")
    e21_txt = f" ({e21:.2f})" if pd.notna(e21) else ""
    s50_txt = f" ({s50:.2f})" if pd.notna(s50) else ""
    if tier == "REGIME FLIP":
        action, size, hold = "BUY — regime flip", "full size", f"position trade: trail the 50-day{s50_txt}"
    elif tier == "CONFIRMED":
        action, size, hold = "BUY — trend turn confirmed", "2/3 size, add over 200-day", f"swing→position: trail the 21-EMA{e21_txt}"
    elif tier == "SCOUT":
        action, size, hold = "BUY — scout the turn", "1/3 size", "add at CONFIRMED (5/8), stop under swing low"
    elif bounce["active"]:
        action = "BUY — tactical bounce" if bounce["triggered_today"] else "HOLD bounce — manage"
        size = "small (bear-regime rental)" if regime == "BEARISH" else "half size"
        hold = f"take profits into the 50/200-day band; exit on a close under the 21-EMA{e21_txt}"
    elif regime == "BEARISH":
        action, size, hold = "STAND ASIDE — bear regime, no trigger", "—", "wait for an oversold hook or 3/8 stack"
    else:
        action, size, hold = "WAIT — no trigger", "—", "let a setup form"

    targets = [t for t in (s50, s200) if pd.notna(t) and t > close]
    levels = {
        "entry": round(close, 2),
        "stop": round(stop, 2),
        "risk_per_share": round(risk_per_share, 2),
        "targets": [round(t, 2) for t in targets] + [round(close + 2 * risk_per_share, 2)],
        "r_to_first_target": round((targets[0] - close) / risk_per_share, 2) if targets else None,
    }
    if account:
        risk_amt = account * risk_pct / 100.0
        levels["shares_for_risk"] = int(risk_amt // risk_per_share)
        levels["risk_amount"] = round(risk_amt, 2)

    zb, tyx = frames.get("ZB"), frames.get("TYX")
    flips = []
    if pd.notna(s50) and close < s50:
        flips.append(f"TLT closes over {s50:.2f} (50-day) — first trend-turn add")
    if pd.notna(s200) and close < s200:
        flips.append(f"TLT closes over {s200:.2f} (200-day) — regime flip")
    if zb is not None and _last(zb, "Close") < _last(zb, "sma50"):
        flips.append(f"ZB closes over {to_32nds(_last(zb, 'sma50'))} (50-day) — futures confirm")
    if tyx is not None and _last(tyx, "Close") > _last(tyx, "sma50"):
        flips.append(f"30y yield closes under {_last(tyx, 'sma50'):.2f}% — yield trend cracks")
    flips.append(f"TLT closes under {swing_low:.2f} (15-day low) — bounce dead, bear leg resumes")
    return {"action": action, "size": size, "management": hold, "levels": levels, "what_flips_it": flips}


def exit_engine(frames: dict, res: dict, entry: float | None) -> dict:
    """The sell signal, evaluated as if long TLT. Mode-aware: rentals exit fast
    (21-EMA trail), a flipped regime gets room (50-day trail); the prior 15-day
    low is the structure stop in every mode."""
    tlt = frames["TLT"]
    zb, tyx = frames.get("ZB"), frames.get("TYX")
    close = _last(tlt, "Close")
    e21, s50 = _last(tlt, "ema21"), _last(tlt, "sma50")
    rsi_now = _last(tlt, "rsi14")
    prior_low = tlt["Low"].rolling(15).min().shift(1)
    hard_stop = float(prior_low.iloc[-1]) if pd.notna(prior_low.iloc[-1]) else float("nan")
    tier = res["stack"]["tier"]
    trail_name, trail = ("50-day", s50) if tier == "REGIME FLIP" else ("21-EMA", e21)

    def fmt(x):
        return f"{x:.2f}" if pd.notna(x) else "n/a"

    conds: list[dict] = []

    def add(kind, name, active, short=None):
        conds.append({"kind": kind, "name": name, "on": bool(active), "short": short or name})

    add("exit", f"Close under the prior 15-day low ({fmt(hard_stop)}) — structure broken",
        pd.notna(hard_stop) and close < hard_stop, "structure broken")
    add("exit", f"Close under the {trail_name} trail ({fmt(trail)})",
        pd.notna(trail) and close < trail, f"{trail_name} trail broken")
    in_bear_rental = tier in ("NONE", "SCOUT") and res["regime"]["label"] == "BEARISH"
    tagged = (in_bear_rental and pd.notna(s50)
              and float(tlt["High"].iloc[-1]) >= s50 and close <= s50)
    add("trim", f"Tagged the 50-day target ({fmt(s50)}) and rejected — sell into the band", tagged)
    add("trim", f"RSI {rsi_now:.0f} ≥ 70 — overbought, sell-into-strength zone", rsi_now >= 70)
    add("warn", "ZB futures closed under their 21-EMA — futures lead, cash follows",
        zb is not None and _last(zb, "Close") < _last(zb, "ema21"))
    add("warn", "30y yield momentum turning back up (9-EMA > 21-EMA on ^TYX)",
        tyx is not None and _last(tyx, "ema9") > _last(tyx, "ema21"))
    bear_div = False
    highs = pivot_points(tlt["High"], w=4, kind="high")
    if len(highs) >= 2:
        d1, d2 = highs.index[-2], highs.index[-1]
        bear_div = (float(highs.iloc[-1]) > float(highs.iloc[-2])
                    and float(tlt["rsi14"].loc[d2]) < float(tlt["rsi14"].loc[d1]))
    add("warn", "Bearish divergence: higher TLT high on weaker RSI — rally exhausting", bear_div)

    exits = [c for c in conds if c["kind"] == "exit" and c["on"]]
    trims = [c for c in conds if c["kind"] == "trim" and c["on"]]
    warns = [c for c in conds if c["kind"] == "warn" and c["on"]]
    if exits:
        verdict = "EXIT — " + exits[0]["short"]
    elif trims:
        verdict = "TRIM — take partial profits"
    elif len(warns) >= 2:
        verdict = "CAUTION — tighten the stop"
    else:
        verdict = "HOLD — no exit signals"

    out = {
        "verdict": verdict,
        "conditions": conds,
        "trail": {"name": trail_name, "level": round(trail, 2) if pd.notna(trail) else None},
        "hard_stop": round(hard_stop, 2) if pd.notna(hard_stop) else None,
    }
    if entry:
        pnl_pct = (close - entry) / entry * 100
        r_den = max(entry - hard_stop, 0.01) if pd.notna(hard_stop) else None
        r_mult = round((close - entry) / r_den, 2) if r_den else None
        out["position"] = {
            "entry": entry,
            "pnl_pct": round(pnl_pct, 2),
            "r_multiple": r_mult,
            "note": "≥ +1R open — move stop to breakeven" if r_mult is not None and r_mult >= 1 else None,
        }
    return out


def analyze(frames: dict, account: float | None = None, risk_pct: float = 1.0,
            entry: float | None = None) -> dict:
    comps = regime_components(frames)
    score, label = regime_score(comps)
    res: dict = {
        "as_of": str(frames["TLT"].index[-1].date()),
        "regime": {"score": score, "label": label, "components": comps},
        "bounce": bounce_state(frames["TLT"]),
        "stack": {},
        "divergences": divergences(frames),
        "macro": macro_checks(frames),
        "tape": {},
    }
    items = trend_stack(frames)
    lit, tier = stack_tier(items, frames.get("TLT"))
    res["stack"] = {"items": items, "lit": lit, "of": len(items), "tier": tier}
    for name, df in frames.items():
        res["tape"][name] = {
            "close": round(_last(df, "Close"), 3),
            "chg1": round(_last(df, "chg1"), 2),
            "rsi14": round(_last(df, "rsi14"), 1),
            "sma50": round(_last(df, "sma50"), 3) if pd.notna(_last(df, "sma50")) else None,
            "sma200": round(_last(df, "sma200"), 3) if pd.notna(_last(df, "sma200")) else None,
            "macd_up": bool(_last(df, "macd") > _last(df, "macd_sig")),
            "roc20": round(_last(df, "roc20"), 1),
        }
    res["plan"] = action_and_levels(frames, res, account, risk_pct)
    res["exit"] = exit_engine(frames, res, entry)
    return res


# ----------------------------------------------------------------------------- history mode


def history_frame(frames: dict, n: int) -> pd.DataFrame:
    """Re-evaluate the signal state for each of the last n sessions (vectorized)."""
    tlt = frames["TLT"]
    idx = tlt.index

    def aligned(key, col):
        df = frames.get(key)
        return df[col].reindex(idx).ffill() if df is not None else pd.Series(np.nan, index=idx)

    conds = pd.DataFrame(index=idx)
    conds["tlt_ema"] = tlt["ema9"] > tlt["ema21"]
    conds["tlt_macd"] = tlt["macd"] > tlt["macd_sig"]
    conds["tlt_50"] = tlt["Close"] > tlt["sma50"]
    conds["tlt_50rise"] = tlt["sma50"] > tlt["sma50"].shift(10)
    lows = pivot_points(tlt["Low"], w=4, kind="low")
    hsl = pd.Series(False, index=idx)
    if len(lows) >= 2:
        state, prev, cur = pd.Series(pd.NA, index=idx, dtype="boolean"), None, None
        for d, v in lows.items():
            prev, cur = cur, v
            if prev is not None:
                state.loc[d] = bool(cur > prev)
        hsl = state.ffill().fillna(False).astype(bool)
    conds["tlt_hsl"] = hsl
    conds["zb_mom"] = (aligned("ZB", "ema9") > aligned("ZB", "ema21")) & (aligned("ZB", "macd") > aligned("ZB", "macd_sig"))
    conds["zb_50"] = aligned("ZB", "Close") > aligned("ZB", "sma50")
    conds["tyx_50"] = aligned("TYX", "Close") < aligned("TYX", "sma50")
    stack_n = conds.fillna(False).astype(int).sum(axis=1)
    bounce = bounce_series(tlt)

    prior_low = tlt["Low"].rolling(15).min().shift(1)
    exit_now = (tlt["Close"] < tlt["ema21"]) | (tlt["Close"] < prior_low)
    out = pd.DataFrame(
        {
            "close": tlt["Close"].round(2),
            "chg%": tlt["chg1"].round(2),
            "rsi": tlt["rsi14"].round(1),
            "stack": stack_n.astype(str) + "/8",
            "bounce": np.where(bounce, "BUY", ""),
            "tier": np.select([stack_n >= 5, stack_n >= 3], ["CONFIRMED", "SCOUT"], ""),
            "exit": np.where(exit_now, "EXIT", ""),
        }
    ).tail(n)
    out.index = [str(d.date()) for d in out.index]
    return out


# ----------------------------------------------------------------------------- rendering

GREEN, RED, YEL, DIM, BOLD, END = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def paint(cond: bool | None, txt_true="YES", txt_false="no", txt_na="n/a") -> str:
    if cond is None:
        return f"{DIM}{txt_na}{END}"
    return f"{GREEN}{txt_true}{END}" if cond else f"{RED}{txt_false}{END}"


def render_plain(res: dict, demo: bool) -> None:
    src = " (DEMO DATA — synthetic)" if demo else ""
    reg = res["regime"]
    color = GREEN if reg["label"] == "BULLISH" else RED if reg["label"] == "BEARISH" else YEL
    print(f"\n{BOLD}TLT DURATION SCANNER{END}  as of {res['as_of']}{src}")
    print("=" * 74)
    print(f"\n{BOLD}TAPE{END}")
    hdr = f"  {'':5} {'last':>9} {'chg%':>7} {'RSI':>6} {'50d':>9} {'200d':>9} {'MACD':>6} {'20d%':>7}"
    print(DIM + hdr + END)
    for name, t in res["tape"].items():
        last = to_32nds(t["close"]) if name == "ZB" else f"{t['close']:.2f}"
        s50 = to_32nds(t["sma50"]) if name == "ZB" and t["sma50"] else (f"{t['sma50']:.2f}" if t["sma50"] else "—")
        s200 = to_32nds(t["sma200"]) if name == "ZB" and t["sma200"] else (f"{t['sma200']:.2f}" if t["sma200"] else "—")
        chg = f"{t['chg1']:+.2f}"
        chg = (GREEN if t["chg1"] >= 0 else RED) + f"{chg:>7}" + END
        macd_s = paint(t["macd_up"], "up", "down")
        print(f"  {name:5} {last:>9} {chg} {t['rsi14']:>6} {s50:>9} {s200:>9} {macd_s:>15} {t['roc20']:>+7.1f}")
    print(f"\n{BOLD}REGIME{END}  {color}{reg['label']}{END}  score {reg['score']:+.0f} / ±100")
    neg = [c["name"] for c in reg["components"] if c["bullish"] is False]
    pos = [c["name"] for c in reg["components"] if c["bullish"]]
    if pos:
        print(f"  {GREEN}+{END} " + ", ".join(pos))
    if neg:
        print(f"  {RED}−{END} " + ", ".join(neg))
    st = res["stack"]
    print(f"\n{BOLD}TREND-TURN STACK{END}  {st['lit']}/{st['of']} lit → tier: {st['tier']}")
    for i in st["items"]:
        print(f"  [{paint(i['on'], 'x', ' ', '?')}] {i['name']}")
    b = res["bounce"]
    b_txt = "TRIGGERED TODAY" if b["triggered_today"] else ("active — managing" if b["active"] else "idle")
    b_col = GREEN if b["active"] else DIM
    since = f" (last trigger {b['last_trigger']}, {b['days_since']}d ago)" if b["last_trigger"] else ""
    print(f"\n{BOLD}BOUNCE SIGNAL{END}  {b_col}{b_txt}{END}{since}")
    if res["divergences"]:
        print(f"\n{BOLD}DIVERGENCES{END}")
        for d in res["divergences"]:
            print(f"  * {d}")
    print(f"\n{BOLD}MACRO CROSS-CHECKS{END}")
    for m in res["macro"]:
        print(f"  * {m}")
    p = res["plan"]
    act_col = GREEN if p["action"].startswith(("BUY", "HOLD")) else YEL
    print(f"\n{BOLD}ACTION{END}  {act_col}{BOLD}{p['action']}{END}   size: {p['size']}")
    print(f"  manage: {p['management']}")
    lv = p["levels"]
    tgt = " / ".join(f"{t:.2f}" for t in lv["targets"])
    r1 = f"  (first target = {lv['r_to_first_target']}R)" if lv.get("r_to_first_target") else ""
    print(f"  entry ~{lv['entry']:.2f}   stop {lv['stop']:.2f}   risk/share {lv['risk_per_share']:.2f}   targets {tgt}{r1}")
    if "shares_for_risk" in lv:
        print(f"  size for {lv['risk_amount']:.0f} risk: {BOLD}{lv['shares_for_risk']} shares{END}")
    ex = res["exit"]
    v = ex["verdict"]
    v_col = RED if v.startswith("EXIT") else YEL if v.startswith(("TRIM", "CAUTION")) else GREEN
    print(f"\n{BOLD}EXIT ENGINE (as if long TLT){END}  {v_col}{BOLD}{v}{END}")
    for c in ex["conditions"]:
        mark_col = RED if c["kind"] == "exit" else YEL
        mark = f"{mark_col}!{END}" if c["on"] else " "
        print(f"  [{mark}] {c['name']}")
    if ex.get("position"):
        pos = ex["position"]
        r_txt = f" ({pos['r_multiple']:+.2f}R)" if pos.get("r_multiple") is not None else ""
        note = f"   ← {pos['note']}" if pos.get("note") else ""
        print(f"  position: entry {pos['entry']:.2f} → {pos['pnl_pct']:+.2f}%{r_txt}{note}")
    print(f"\n{BOLD}WHAT FLIPS IT{END}")
    for f in p["what_flips_it"]:
        print(f"  -> {f}")
    print()


def render_rich(res: dict, demo: bool) -> None:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()
    reg, st, b, p = res["regime"], res["stack"], res["bounce"], res["plan"]
    reg_style = {"BULLISH": "bold green", "BEARISH": "bold red"}.get(reg["label"], "bold yellow")

    tape = Table(box=box.SIMPLE_HEAVY, pad_edge=False)
    for col, justify in (("", "left"), ("last", "right"), ("chg%", "right"), ("RSI", "right"),
                         ("50d", "right"), ("200d", "right"), ("MACD", "center"), ("20d %", "right")):
        tape.add_column(col, justify=justify)
    for name, t in res["tape"].items():
        fmt = to_32nds if name == "ZB" else (lambda x: f"{x:.2f}")
        tape.add_row(
            Text(name, style="bold"),
            fmt(t["close"]),
            Text(f"{t['chg1']:+.2f}", style="green" if t["chg1"] >= 0 else "red"),
            f"{t['rsi14']:.1f}",
            fmt(t["sma50"]) if t["sma50"] else "—",
            fmt(t["sma200"]) if t["sma200"] else "—",
            Text("▲" if t["macd_up"] else "▼", style="green" if t["macd_up"] else "red"),
            Text(f"{t['roc20']:+.1f}", style="green" if t["roc20"] >= 0 else "red"),
        )

    stack_t = Table(box=None, pad_edge=False, show_header=False)
    stack_t.add_column(width=3)
    stack_t.add_column()
    for i in st["items"]:
        mark = Text("[x]", style="green") if i["on"] else Text("[?]", style="dim") if i["on"] is None else Text("[ ]", style="red")
        stack_t.add_row(mark, i["name"])

    b_txt = "TRIGGERED TODAY" if b["triggered_today"] else ("active — managing" if b["active"] else "idle")
    b_line = Text(f"BOUNCE: {b_txt}", style="green" if b["active"] else "dim")
    if b["last_trigger"]:
        b_line.append(f"   last trigger {b['last_trigger']} ({b['days_since']}d ago)", style="dim")

    notes = Text()
    for d in res["divergences"]:
        notes.append(f"◆ {d}\n", style="cyan")
    for m in res["macro"]:
        notes.append(f"• {m}\n")

    lv = p["levels"]
    plan = Text()
    plan.append(f"{p['action']}\n", style="bold green" if p["action"].startswith(("BUY", "HOLD")) else "bold yellow")
    plan.append(f"size: {p['size']}   |   {p['management']}\n")
    tgt = " / ".join(f"{t:.2f}" for t in lv["targets"])
    plan.append(f"entry ~{lv['entry']:.2f}   stop {lv['stop']:.2f}   risk/share {lv['risk_per_share']:.2f}   targets {tgt}")
    if lv.get("r_to_first_target"):
        plan.append(f"   ({lv['r_to_first_target']}R to first)")
    if "shares_for_risk" in lv:
        plan.append(f"\nsize for ${lv['risk_amount']:.0f} risk: {lv['shares_for_risk']} shares", style="bold")
    flips = Text()
    for f in p["what_flips_it"]:
        flips.append(f"→ {f}\n", style="dim")

    ex = res["exit"]
    v = ex["verdict"]
    ex_style = "bold red" if v.startswith("EXIT") else "bold yellow" if v.startswith(("TRIM", "CAUTION")) else "bold green"
    ex_t = Table(box=None, pad_edge=False, show_header=False)
    ex_t.add_column(width=3)
    ex_t.add_column()
    for c in ex["conditions"]:
        style = ("red" if c["kind"] == "exit" else "yellow") if c["on"] else "dim"
        ex_t.add_row(Text("[!]" if c["on"] else "[ ]", style=style), Text(c["name"], style=None if c["on"] else "dim"))
    ex_group = [Text(v, style=ex_style), ex_t]
    if ex.get("position"):
        pos = ex["position"]
        r_txt = f" ({pos['r_multiple']:+.2f}R)" if pos.get("r_multiple") is not None else ""
        note = f"   ← {pos['note']}" if pos.get("note") else ""
        ex_group.append(Text(f"position: entry {pos['entry']:.2f} → {pos['pnl_pct']:+.2f}%{r_txt}{note}", style="bold"))

    title = f"TLT DURATION SCANNER — {res['as_of']}" + ("  [DEMO DATA]" if demo else "")
    console.print(Panel(tape, title=title, border_style="blue"))
    console.print(
        Panel(
            Group(Text(f"REGIME: {reg['label']}  ({reg['score']:+.0f}/±100)", style=reg_style),
                  Text(f"TREND-TURN STACK: {st['lit']}/{st['of']} — tier {st['tier']}",
                       style="bold" if st["tier"] != "NONE" else "dim"),
                  stack_t, b_line),
            title="signal engine", border_style="magenta",
        )
    )
    if notes.plain:
        console.print(Panel(notes, title="cross-checks", border_style="cyan"))
    console.print(Panel(Group(plan, Text(), flips), title="plan", border_style="green"))
    console.print(Panel(Group(*ex_group), title="exit engine — as if long TLT",
                        border_style="red" if v.startswith("EXIT") else "green"))


def notify_macos(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Glass"'],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ----------------------------------------------------------------------------- logic doc

LOGIC = """
THE LOGIC BEHIND THE SCANNER
============================
What TLT is: packaged long-duration risk. Effective duration ~15-16, so
  TLT %change ~= -16 x (change in long yields, in %). 30y +10bp => TLT ~ -1.6%.
  You are not trading "bonds", you are trading the DIRECTION OF LONG YIELDS.

Why three instruments:
  ^TYX (30y yield)  - the actual driver. TLT charts are just its mirror.
  ZB futures        - trades ~23h/day, reacts to auctions, CPI, overnight macro
                      FIRST. Futures lead the cash ETF; TLT gaps to catch up.
  TLT               - what you can actually buy; where entries/stops live.

Layer 1 - REGIME (should you even be hunting longs?)
  Moving-average structure (price vs 50d/200d, 50d slope, 50d vs 200d) scored
  across TLT + ZB, with the same tests INVERTED on ^TYX. Score -100..+100.
  In a BEARISH regime, longs are countertrend RENTALS. In TRANSITION you scout.
  In BULLISH you hold and add. The regime decides SIZE and HOLDING PERIOD,
  not entries.

Layer 2 - TRIGGERS (when do you actually buy?)
  BOUNCE (mean reversion): RSI(14) was <32 in the last 10 bars, has hooked up
  through 35, and price is back over the 9-EMA (or MACD histogram rising 2 bars).
  Logic: capitulation -> exhaustion -> first sign of demand. In a bear regime
  you sell it into the 50/200-day band, because that is where rallies die.
  TREND-TURN STACK (8 conditions): 9/21-EMA cross, MACD cross, 50-day reclaim,
  50-day slope up, higher swing low, ZB momentum cross, ZB 50-day reclaim,
  30y yield losing its 50-day. Bottoms are processes: each condition is one
  brick. 3/8 = scout with 1/3 size, 5/8 = confirmed, over the 200-day = regime
  flip, full position. You never need to predict the low - you pay a slightly
  worse price for a much better probability.

Layer 3 - CROSS-CHECKS (is the signal honest?)
  * Bullish divergence on TLT lows / RSI - sellers exhausting.
  * Yield exhaustion: ^TYX higher high on weaker RSI - the uptrend in yields
    thinning out. Yield-down is the only durable TLT fuel.
  * DBA / commodity tape: food & commodity inflation feeds breakevens ->
    long yields. A hot ag tape (like Aug 2026) argues for renting bounces,
    not marrying them.

Layer 4 - EXITS (the sell signal, evaluated "as if long"):
  EXIT    - close under the trail (21-EMA for rentals and swings, 50-day once
            the regime has flipped), or close under the PRIOR 15-day low --
            the structure stop that overrides everything else.
  TRIM    - tagged the 50-day band from below and got rejected in a bear
            regime, or RSI >= 70: sell strength, don't admire it.
  CAUTION - two or more early warnings: ZB futures lose their 21-EMA first
            (futures lead down too), 30y-yield momentum turns back up
            (9>21 EMA on ^TYX), bearish RSI divergence on the highs.
  Exits are mode-aware on purpose: a bear-regime rental dies at the first
  momentum crack; a confirmed trend gets room to breathe. Selling is a
  process like buying -- trim into strength, exit on the trail, and never
  argue with the structure stop.

Risk (non-negotiable):
  Stop = 15-day swing low minus 0.5 ATR. Size = (account x risk%) / (entry-stop).
  First target = 50-day, second = 200-day / +2R. Never average down a rental.

Failure modes this design accepts:
  - You will buy some bounces that fail (stopped at ~ -1R by design).
  - You will be late to the exact low (deliberate - confirmation costs price).
  - EOD data: signals fire at the close, you act next session.
"""


# ----------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="TLT duration scanner — regime + triggers + futures confirmation")
    ap.add_argument("--demo", action="store_true", help="run on synthetic data (no network)")
    ap.add_argument("--refresh", action="store_true", help="ignore cache, force re-download")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the dashboard")
    ap.add_argument("--plain", action="store_true", help="force plain ANSI output (no rich)")
    ap.add_argument("--history", type=int, metavar="N", help="show signal state for the last N sessions")
    ap.add_argument("--watch", type=int, metavar="SEC", help="re-scan every SEC seconds until Ctrl-C")
    ap.add_argument("--notify", action="store_true", help="macOS notification when a BUY is on")
    ap.add_argument("--alert-exit", action="store_true", help="exit code 2 when action is a BUY (for scripting)")
    ap.add_argument("--account", type=float, help="account size for position sizing")
    ap.add_argument("--risk", type=float, default=1.0, help="risk %% of account per trade (default 1.0)")
    ap.add_argument("--entry", type=float, help="your TLT entry price — adds open P&L and R-multiple to the exit engine")
    ap.add_argument("--explain", action="store_true", help="print the trading logic and exit")
    args = ap.parse_args()

    if args.explain:
        print(LOGIC)
        return 0

    def one_scan(prev: tuple[str, str] | None = None) -> tuple[dict | None, tuple[str, str] | None]:
        # --watch must see fresh bars every cycle; the 4h cache would otherwise serve stale data
        force = args.refresh or bool(args.watch)
        frames = demo_frames() if args.demo else load_frames(refresh=force)
        if "TLT" not in frames:
            print("ERROR: could not load TLT data (network blocked?). Try --demo to test the pipeline.", file=sys.stderr)
            return None, None
        res = analyze(frames, account=args.account, risk_pct=args.risk, entry=args.entry)
        action, exitv = res["plan"]["action"], res["exit"]["verdict"]
        if args.history:
            df = history_frame(frames, args.history)
            print(f"\nSignal history — last {args.history} sessions (TLT)\n")
            print(df.to_string())
            print()
            return res, (action, exitv)
        if args.json:
            print(json.dumps(res, indent=2, default=str))
        else:
            use_rich = not args.plain
            if use_rich:
                try:
                    render_rich(res, args.demo)
                except ImportError:
                    use_rich = False
            if not use_rich:
                render_plain(res, args.demo)
        if args.notify:
            prev_action, prev_exit = prev if prev else (None, None)
            if action.startswith("BUY") and action != prev_action:
                notify_macos("TLT scanner", action)
            if exitv.startswith(("EXIT", "TRIM")) and exitv != prev_exit:
                notify_macos("TLT scanner", exitv)
        return res, (action, exitv)

    if args.watch:
        prev = None
        try:
            while True:
                print("\033[2J\033[H", end="")
                _, prev = one_scan(prev)
                time.sleep(max(args.watch, 30))
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0

    res, sig = one_scan()
    if res is None or sig is None:
        return 1
    action, exitv = sig
    if args.alert_exit:
        if exitv.startswith("EXIT"):
            return 3
        if action.startswith("BUY"):
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
