#!/usr/bin/env python3
# =============================================================================
# ag_etf_backtest.py - Agricultural commodity ETF dual-momentum backtest
# -----------------------------------------------------------------------------
# INSTALL:  python -m pip install "pandas>=2.0" numpy yfinance matplotlib pyarrow
#           (pyarrow optional: without it the price cache falls back to CSV)
# RUN:      python ag_etf_backtest.py            # download (or reuse ./data cache)
#           python ag_etf_backtest.py --offline  # cache only, never hit the network
#           python ag_etf_backtest.py --refresh  # ignore cache, force re-download
#           Optional: --data-dir DIR (default ./data), --outdir DIR (default .)
# OUTPUTS:  data/<TICKER>.parquet  cached raw OHLCV+Adj Close, one file per ticker
#           trades.csv    every fill: date, ticker, side, qty, price, notional,
#                         commission, slippage (plus book + unadjusted raw price)
#           weights.csv   daily actual (drifted) weights per ticker + CASH
#           equity.csv    date, strategy, ew4, DBA, WEAT, CORN, SOYB, CANE
#           metrics.csv   every book x {TRAIN, TEST, FULL} performance table
#           figs/equity_curves.png, drawdowns.png, rolling_12m_return.png,
#           figs/weights_area.png
# NOTE:     All books share one engine, one calendar, one cost model. Signals are
#           taken at a month-end CLOSE and filled at the NEXT session's OPEN.
# =============================================================================

from __future__ import annotations

import argparse
import os
import sys
import time
import warnings
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")  # headless-safe; must precede pyplot import
import matplotlib.pyplot as plt

# =============================================================================
# CONSTANTS - tunables live here and nowhere else. Nothing below is optimised.
# =============================================================================

SLEEVES: List[str] = ["WEAT", "CORN", "SOYB", "CANE"]  # tradable active universe
BENCHMARK: str = "DBA"                                  # passive benchmark
CASH_PROXY: str = "BIL"                                 # optional; 0% if unavailable
TICKERS: List[str] = [BENCHMARK] + SLEEVES + [CASH_PROXY]

START: str = "2011-10-01"       # common sample start (WEAT/CORN/SOYB/CANE overlap)
TRAIN_END: str = "2018-12-31"   # TRAIN = START..TRAIN_END, TEST = TRAIN_END+1..end
DOWNLOAD_START: str = "2006-01-01"  # extra history so SMA/momentum warm up pre-START

K: int = 2            # number of sleeves held when fully eligible
SMA_LEN: int = 200    # trend filter length (trading days)
MOM_LEN: int = 252    # 12m momentum lookback - drives eligibility AND ranking
MOM_LEN_SHORT: int = 63  # 3m momentum - REPORTED ONLY, not used for selection

COMMISSION_BPS: float = 5.0    # 0.05% of notional per side
SLIPPAGE_BPS: float = 10.0     # 0.10% of notional per side
COST_BPS: float = COMMISSION_BPS + SLIPPAGE_BPS  # 15 bps per side on traded notional

START_CAPITAL: float = 100_000.0
FFILL_LIMIT: int = 5        # forward-fill at most 5 sessions
TZ: str = "America/New_York"
TRADING_DAYS: int = 252
WEIGHT_TOL: float = 1e-9    # treat |w| below this as flat

_WARNINGS: List[str] = []
_WARN_COUNTS: Dict[str, int] = {}


def warn(msg: str) -> None:
    """Collect a warning so the run ends with one consolidated list. Identical
    messages are counted rather than repeated - every book re-checks the same
    price gaps, so a single bad print would otherwise be shouted seven times."""
    n = _WARN_COUNTS.get(msg, 0) + 1
    _WARN_COUNTS[msg] = n
    if n == 1:
        _WARNINGS.append(msg)
        print(f"  [WARN] {msg}")


def rule(title: str = "") -> None:
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


# =============================================================================
# DATA LAYER
# =============================================================================

_OHLC_FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def _flatten(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """yfinance returns MultiIndex columns for some versions/arg combos. Reduce
    to a plain Open/High/Low/Close/Adj Close/Volume frame for one ticker."""
    if isinstance(df.columns, pd.MultiIndex):
        for level in range(df.columns.nlevels):
            vals = df.columns.get_level_values(level)
            if ticker in set(vals):
                df = df.xs(ticker, axis=1, level=level)
                break
        else:
            df = df.droplevel(list(range(1, df.columns.nlevels)), axis=1)
    df = df.loc[:, ~df.columns.duplicated()]
    keep = [c for c in _OHLC_FIELDS if c in df.columns]
    return df.loc[:, keep]


def _normalise_index(df: pd.DataFrame) -> pd.DataFrame:
    """All dates become tz-naive America/New_York session dates (midnight)."""
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert(TZ).tz_localize(None)
    df = df.copy()
    df.index = idx.normalize()
    df.index.name = "Date"
    return df[~df.index.duplicated(keep="last")].sort_index()


def _cache_paths(data_dir: str, ticker: str) -> Tuple[str, str]:
    return (os.path.join(data_dir, f"{ticker}.parquet"),
            os.path.join(data_dir, f"{ticker}.csv"))


def _read_cache(data_dir: str, ticker: str) -> Optional[pd.DataFrame]:
    pq, csv = _cache_paths(data_dir, ticker)
    try:
        if os.path.exists(pq):
            return _normalise_index(pd.read_parquet(pq))
        if os.path.exists(csv):
            return _normalise_index(pd.read_csv(csv, index_col=0, parse_dates=True))
    except Exception as exc:  # corrupt cache must not kill the run
        warn(f"{ticker}: cache unreadable ({exc}); will try to re-download")
    return None


def _write_cache(data_dir: str, ticker: str, df: pd.DataFrame) -> None:
    os.makedirs(data_dir, exist_ok=True)
    pq, csv = _cache_paths(data_dir, ticker)
    try:
        df.to_parquet(pq)
    except Exception:
        df.to_csv(csv)  # pyarrow/fastparquet missing -> CSV is fine


def _download(ticker: str, attempts: int = 3) -> Optional[pd.DataFrame]:
    """auto_adjust=False so we keep BOTH raw Close and Adj Close: raw Close is
    what a broker prints, Adj Close carries splits + distributions."""
    try:
        import yfinance as yf
    except ImportError:
        warn("yfinance is not installed; running from cache only")
        return None
    for attempt in range(1, attempts + 1):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yf.download(ticker, start=DOWNLOAD_START, auto_adjust=False,
                                  progress=False, threads=False)
            if raw is not None and len(raw) > 0:
                return _normalise_index(_flatten(raw, ticker))
            reason = "empty response"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(2 ** attempt)
        else:
            warn(f"{ticker}: download failed after {attempts} attempts ({reason})")
    return None


def get_prices(ticker: str, data_dir: str, offline: bool,
               refresh: bool) -> Optional[pd.DataFrame]:
    """Cache-first price loader. Returns None only if both paths fail."""
    if not refresh:
        cached = _read_cache(data_dir, ticker)
        if cached is not None and not offline:
            fresh = _download(ticker)
            if fresh is not None:
                merged = fresh.combine_first(cached).sort_index()
                _write_cache(data_dir, ticker, merged)
                return merged
            print(f"  {ticker}: download unavailable, using cache "
                  f"({cached.index[0].date()} -> {cached.index[-1].date()})")
            return cached
        if cached is not None:
            print(f"  {ticker}: cache "
                  f"({cached.index[0].date()} -> {cached.index[-1].date()})")
            return cached
    if offline:
        return None
    fresh = _download(ticker)
    if fresh is not None:
        _write_cache(data_dir, ticker, fresh)
        print(f"  {ticker}: downloaded "
              f"({fresh.index[0].date()} -> {fresh.index[-1].date()})")
    return fresh


def load_universe(data_dir: str, offline: bool, refresh: bool
                  ) -> Tuple[Dict[str, pd.DataFrame], bool]:
    """Load every ticker. Required tickers failing is a hard abort (data_rules:
    'do not silently skip'); the optional cash proxy only downgrades to 0% cash."""
    print("Loading prices...")
    raw: Dict[str, pd.DataFrame] = {}
    failed: List[str] = []
    for ticker in TICKERS:
        df = get_prices(ticker, data_dir, offline, refresh)
        if df is None or df.empty:
            failed.append(ticker)
        else:
            raw[ticker] = df

    required = [t for t in failed if t != CASH_PROXY]
    if required:
        raise SystemExit(
            f"\nFATAL: could not obtain price data for {', '.join(required)}.\n"
            f"       Every ticker in {TICKERS[:-1]} is required; the backtest\n"
            f"       will not silently drop a name. Check your network, then\n"
            f"       re-run (cached files live in '{data_dir}/').")

    use_cash_proxy = CASH_PROXY in raw
    if not use_cash_proxy:
        warn(f"{CASH_PROXY} unavailable -> cash earns 0.00% and pays no trading "
             f"costs. All 'cash' allocations sit uninvested.")
    return raw, use_cash_proxy


def build_panel(raw: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """Build aligned Open/Close/AdjClose/AdjOpen panels plus a staleness matrix.

    Adjusted open = Open * (Adj Close / Close): the same split+distribution
    factor Yahoo applies to the close, so fills and marks live on one scale.
    """
    analysis_cal = pd.DatetimeIndex(sorted(set().union(*[d.index for d in raw.values()])))

    fields = {}
    for name in ("Open", "Close", "Adj Close"):
        fields[name] = pd.DataFrame(
            {t: d[name].reindex(analysis_cal) if name in d.columns else np.nan
             for t, d in raw.items()},
            index=analysis_cal)

    close_raw, adj_close_raw, open_raw = fields["Close"], fields["Adj Close"], fields["Open"]
    for t in adj_close_raw.columns:
        if adj_close_raw[t].isna().all():
            warn(f"{t}: no 'Adj Close' column -> falling back to raw Close. Returns "
                 f"will ignore distributions for this name.")
            adj_close_raw[t] = close_raw[t]

    # Staleness = sessions since the last genuine print, measured BEFORE any
    # forward-fill. 0 = printed today. Used to enforce the 5-day gap rule.
    pos = np.arange(len(analysis_cal), dtype=float)
    last_real = pd.DataFrame(
        np.where(adj_close_raw.notna().to_numpy(), pos[:, None], np.nan),
        index=analysis_cal, columns=adj_close_raw.columns).ffill()
    stale = pd.DataFrame(pos[:, None] - last_real.to_numpy(),
                         index=analysis_cal, columns=adj_close_raw.columns)

    # Forward-fill at most FFILL_LIMIT sessions. Longer holes stay NaN and are
    # caught at rebalance time by the staleness check.
    adj_close = adj_close_raw.ffill(limit=FFILL_LIMIT)
    close = close_raw.ffill(limit=FFILL_LIMIT)
    # Separate panel for VALUATION only, forward-filled without limit. A holding
    # that stops printing must be carried at its last known mark - never at zero,
    # which would silently book a -100% loss. Tradability still obeys FFILL_LIMIT.
    mark = adj_close_raw.ffill()
    adj_factor = (adj_close / close).replace([np.inf, -np.inf], np.nan)
    open_px = open_raw.ffill(limit=FFILL_LIMIT)
    adj_open = open_px * adj_factor

    return {"analysis_cal": analysis_cal, "close_raw": close_raw,
            "open_raw": open_raw, "adj_close": adj_close, "adj_open": adj_open,
            "close": close, "open": open_px, "stale": stale,
            "adj_close_raw": adj_close_raw, "mark": mark}


def backtest_calendar(raw: Dict[str, pd.DataFrame], start: str) -> pd.DatetimeIndex:
    """data_rules: 'align on the intersection of trading days'. Intersect over
    the REQUIRED names only - the optional cash proxy must never shrink the
    sample - then keep sessions from START onward."""
    idx = None
    for t in [BENCHMARK] + SLEEVES:
        idx = raw[t].index if idx is None else idx.intersection(raw[t].index)
    cal = idx[idx >= pd.Timestamp(start)]
    if len(cal) == 0:
        raise SystemExit(f"FATAL: no overlapping trading days at or after {start}.")
    union = None
    for t in [BENCHMARK] + SLEEVES:
        u = raw[t].index
        union = u if union is None else union.union(u)
    dropped = len(union[(union >= cal[0]) & (union <= cal[-1])]) - len(cal)
    if dropped > 0:
        warn(f"{dropped} session(s) in the window traded for some names but not "
             f"all; dropped by the intersection rule.")
    return cal


# =============================================================================
# MISSING-DATA REPORT
# =============================================================================

def missing_data_report(raw: Dict[str, pd.DataFrame], panel: Dict[str, pd.DataFrame],
                        cal: pd.DatetimeIndex, ind: Dict[str, pd.DataFrame]) -> None:
    rule("MISSING-DATA REPORT")
    hdr = (f"{'ticker':<7}{'first':<12}{'last':<12}{'obs':>7}{'NaN cls':>9}"
           f"{'NaN opn':>9}{'max gap':>9}{'sig from':>12}")
    print(hdr)
    print("-" * len(hdr))
    for t in TICKERS:
        if t not in raw:
            print(f"{t:<7}{'-- NOT AVAILABLE --':<40}")
            continue
        d = raw[t]
        win = panel["adj_close_raw"][t].reindex(cal)
        nan_close = int(win.isna().sum())
        nan_open = int(panel["open_raw"][t].reindex(cal).isna().sum())
        max_gap = int(panel["stale"][t].reindex(cal).max()) if len(cal) else 0
        if t in SLEEVES:
            ok = ind["mom12"][t].notna() & ind["sma"][t].notna()
            ok = ok[ok.index >= cal[0]]
            sig = str(ok.index[ok][0].date()) if ok.any() else "never"
        else:
            sig = "n/a"
        print(f"{t:<7}{str(d.index[0].date()):<12}{str(d.index[-1].date()):<12}"
              f"{len(d):>7}{nan_close:>9}{nan_open:>9}{max_gap:>9}{sig:>12}")
    print(f"\n  obs = raw rows downloaded (from {DOWNLOAD_START}); NaN/max gap are "
          f"measured\n  inside the backtest window only. 'max gap' = longest run of "
          f"sessions with no\n  print (0 = never missing). 'sig from' = first date "
          f"both the {MOM_LEN}d momentum\n  and the {SMA_LEN}d SMA exist, i.e. the "
          f"first date the name can be selected.")


# =============================================================================
# SIGNALS - every value is computed from closes at or before the signal date.
# =============================================================================

def compute_indicators(panel: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    close = panel["adj_close"]
    return {
        "mom12": close / close.shift(MOM_LEN) - 1.0,
        "mom3": close / close.shift(MOM_LEN_SHORT) - 1.0,   # reported, not traded
        "sma": close.rolling(SMA_LEN, min_periods=SMA_LEN).mean(),
        "above_sma": close > close.rolling(SMA_LEN, min_periods=SMA_LEN).mean(),
        "close": close,
    }


def tradable_at(sig_date: pd.Timestamp, panel: Dict[str, pd.DataFrame],
                names: List[str]) -> List[str]:
    """A name is tradable at a rebalance only if its last genuine print is within
    FFILL_LIMIT sessions. Longer gap -> dropped from THIS rebalance, with a warn."""
    out = []
    for t in names:
        s = panel["stale"].at[sig_date, t]
        if pd.isna(s) or s > FFILL_LIMIT:
            warn(f"{sig_date.date()}: {t} has no print within {FFILL_LIMIT} sessions "
                 f"-> dropped from this rebalance.")
        else:
            out.append(t)
    return out


def strategy_weights(sig_date: pd.Timestamp, ind: Dict[str, pd.DataFrame],
                     panel: Dict[str, pd.DataFrame]) -> Dict[str, float]:
    """Dual momentum: absolute (12m return > 0) AND trend (close > 200d SMA);
    rank survivors by 12m momentum, hold top K equal-weight, else cash."""
    candidates = tradable_at(sig_date, panel, SLEEVES)
    eligible = []
    for t in candidates:
        m12 = ind["mom12"].at[sig_date, t]
        above = ind["above_sma"].at[sig_date, t]
        if pd.notna(m12) and m12 > 0 and bool(above):
            eligible.append((t, float(m12)))
    eligible.sort(key=lambda kv: kv[1], reverse=True)
    picks = [t for t, _ in eligible[:K]]
    if not picks:
        return {}                      # 0 eligible -> 100% cash
    w = 1.0 / len(picks)               # 1 eligible -> 100% that name
    return {t: w for t in picks}


def ew4_weights(sig_date: pd.Timestamp, panel: Dict[str, pd.DataFrame]
                ) -> Dict[str, float]:
    names = tradable_at(sig_date, panel, SLEEVES)
    if not names:
        return {}
    return {t: 1.0 / len(names) for t in names}


def buy_hold_weights(ticker: str) -> Dict[str, float]:
    """100% one name. Once held, drift keeps the weight at exactly 1.0, so the
    monthly rebalance is a no-op and only the opening fill costs anything."""
    return {ticker: 1.0}


# =============================================================================
# EXECUTION ENGINE
# =============================================================================

def rebalance_schedule(cal: pd.DatetimeIndex, analysis_cal: pd.DatetimeIndex
                       ) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """Return (signal_date, execution_date) pairs.

    Signal date = last trading day of the month, using closes through that day.
    Execution   = the NEXT session, at the open. No future bar is ever consulted.

    The schedule is seeded with the last session BEFORE the window so every book
    is invested from the first day of the sample; that seed still only reads a
    close that precedes the first fill. A month-end with no following session
    (the sample simply ends) is skipped - there is no bar to fill on.
    """
    pairs: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    prior = analysis_cal[analysis_cal < cal[0]]
    if len(prior):
        pairs.append((prior[-1], cal[0]))
    else:
        warn(f"No session before {cal[0].date()}; the first rebalance is deferred "
             f"to the first month-end and books start in cash.")

    month_ends = pd.Series(cal, index=cal).groupby(cal.to_period("M")).last()
    pos = {d: i for i, d in enumerate(cal)}
    for sig in month_ends:
        i = pos[sig]
        if i + 1 < len(cal):
            pairs.append((sig, cal[i + 1]))
        else:
            warn(f"Month-end {sig.date()} is the last session in the sample; no "
                 f"next open to fill on, so that rebalance is skipped.")
    return pairs


def run_book(name: str, weight_fn, cal: pd.DatetimeIndex,
             panel: Dict[str, pd.DataFrame], schedule, use_cash_proxy: bool
             ) -> Dict[str, object]:
    """Share-level simulation on adjusted prices.

    Timing, precisely:
      - close(sig_date)      signal is formed
      - open(exec_date)      the whole trade list is filled, costs are charged
      - close(exec_date)     portfolio is marked with the NEW holdings
    Overnight moves between close(sig) and open(exec) therefore accrue to the
    OLD holdings, because the share vector has not changed yet.

    Costs: commission (5 bps) + slippage (10 bps) on |traded notional| per side.
    A name whose target weight equals its drifted weight generates no trade and
    no cost, which is exactly the 'apply costs only when the weight changes' rule.
    """
    tickers = list(panel["adj_close"].columns)
    adj_close, adj_open, mark = panel["adj_close"], panel["adj_open"], panel["mark"]
    close_raw = panel["close_raw"]

    shares = pd.Series(0.0, index=tickers)
    cash = START_CAPITAL                      # uninvested USD, earns 0
    exec_map = {e: s for s, e in schedule}

    equity, hold_w, trades = [], [], []
    cost_by_date: Dict[pd.Timestamp, float] = {}

    for day in cal:
        px_c = mark.loc[day]          # valuation mark, never zero-filled

        if day in exec_map:
            sig_date = exec_map[day]
            target = dict(weight_fn(sig_date))
            residual = 1.0 - sum(target.values())
            if use_cash_proxy and residual > WEIGHT_TOL:
                target[CASH_PROXY] = target.get(CASH_PROXY, 0.0) + residual

            # Fill price ladder, in order of preference:
            #   1. adjusted OPEN of the execution session  (the intended fill)
            #   2. adjusted CLOSE of that same session      (open missing; logged)
            #   3. last known mark                          (name has gone dark)
            # Nothing here ever reads a bar after `day`, so there is no look-ahead.
            # A name priced only by (3) cannot be bought - it is stripped from the
            # target - but an existing holding is still liquidated at that mark so
            # the book does not strand an untradable position.
            fill = adj_open.loc[day].copy()
            held = set(shares[shares.abs() > 0].index)
            stale_px = set()
            for t in set(list(target)) | held:
                if pd.notna(fill.get(t, np.nan)):
                    continue
                if pd.notna(adj_close.loc[day].get(t, np.nan)):
                    fill[t] = adj_close.loc[day][t]
                    warn(f"{day.date()}: {t} open missing -> filled at that day's "
                         f"close ({name}).")
                elif pd.notna(px_c.get(t, np.nan)):
                    fill[t] = px_c[t]
                    stale_px.add(t)
                    if t in held:
                        warn(f"{day.date()}: {t} has no fresh print -> position "
                             f"valued/exited at its last known mark ({name}).")
            target = {t: w for t, w in target.items()
                      if pd.notna(fill.get(t, np.nan)) and t not in stale_px}

            # Value the book at execution prices, marking any untradable holding
            # at its carried price rather than dropping it to zero.
            px_exec = fill.reindex(tickers)
            px_exec = px_exec.where(px_exec.notna(), px_c.reindex(tickers))
            v_open = float((shares * px_exec.fillna(0.0)).sum()) + cash

            # Costs come out of the same pot being allocated, so solve the small
            # fixed point v_target = v_open - cost(v_target). 15 bps converges in
            # a couple of passes; six is belt-and-braces.
            tgt = pd.Series(0.0, index=tickers)
            for t, w in target.items():
                tgt[t] = w
            v_target, new_shares, cost = v_open, shares.copy(), 0.0
            for _ in range(6):
                new_shares = pd.Series(
                    np.where(tgt.to_numpy() > 0,
                             tgt.to_numpy() * v_target
                             / px_exec.replace(0.0, np.nan).to_numpy(), 0.0),
                    index=tickers).fillna(0.0)
                dsh = new_shares - shares
                notional = float((dsh.abs() * px_exec.fillna(0.0)).sum())
                cost = notional * COST_BPS / 1e4
                v_target = v_open - cost

            dsh = new_shares - shares
            for t in tickers:
                q = float(dsh[t])
                px = float(px_exec[t]) if pd.notna(px_exec[t]) else 0.0
                if abs(q) * px < 1e-8:
                    continue
                notl = abs(q) * px
                trades.append({
                    "date": day, "book": name, "ticker": t,
                    "side": "BUY" if q > 0 else "SELL", "qty": q, "price": px,
                    "notional": notl,
                    "commission": notl * COMMISSION_BPS / 1e4,
                    "slippage": notl * SLIPPAGE_BPS / 1e4,
                    "raw_price": float(close_raw.at[day, t])
                    if pd.notna(close_raw.at[day, t]) else np.nan,
                    "signal_date": sig_date})
            if cost > 0:
                cost_by_date[day] = cost_by_date.get(day, 0.0) + cost
            cash = (v_open - cost
                    - float((new_shares * px_exec.fillna(0.0)).sum()))
            shares = new_shares

        mv = shares * px_c.reindex(tickers).fillna(0.0)
        v_close = float(mv.sum()) + cash
        equity.append(v_close)
        w = (mv / v_close) if v_close > 0 else mv * 0.0
        w["CASH"] = cash / v_close if v_close > 0 else 0.0
        hold_w.append(w)

    eq = pd.Series(equity, index=cal, name=name)
    # A long-only, unlevered book cannot go to zero or NaN. If it does, the price
    # panel is broken - fail loudly rather than publish nonsense metrics.
    bad = eq[~np.isfinite(eq.to_numpy()) | (eq.to_numpy() <= 0.0)]
    if len(bad):
        raise SystemExit(f"FATAL: book '{name}' hit a non-positive/NaN value on "
                         f"{bad.index[0].date()}. This means a held position lost "
                         f"its price. Inspect the missing-data report.")
    wdf = pd.DataFrame(hold_w, index=cal)
    tdf = pd.DataFrame(trades, columns=["date", "book", "ticker", "side", "qty",
                                        "price", "notional", "commission",
                                        "slippage", "raw_price", "signal_date"])
    costs = pd.Series(cost_by_date, dtype=float).reindex(cal).fillna(0.0)
    return {"equity": eq, "weights": wdf, "trades": tdf, "costs": costs}


# =============================================================================
# METRICS
# =============================================================================

def _returns(eq: pd.Series) -> pd.Series:
    return (eq / eq.shift(1) - 1.0).iloc[1:]


def _max_drawdown(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1.0).min()) if len(eq) else np.nan


def drawdown_series(eq: pd.Series) -> pd.Series:
    return eq / eq.cummax() - 1.0


def perf_metrics(book: Dict[str, object], period: str, lo: pd.Timestamp,
                 hi: pd.Timestamp, use_cash_proxy: bool) -> Dict[str, float]:
    eq_full = book["equity"]
    r = _returns(eq_full)
    r = r[(r.index >= lo) & (r.index <= hi)]
    eq = eq_full[(eq_full.index >= lo) & (eq_full.index <= hi)]
    if len(r) < 2:
        return {"period": period}

    n = len(r)
    total = float((1.0 + r).prod() - 1.0)

    # CAGR is annualised on CALENDAR time, over exactly the span the returns
    # cover: from the close the first return is measured FROM, to the last close
    # in the window. TRAIN and TEST therefore tile the sample with no gap and no
    # double-count, and their compounded returns chain-link to FULL exactly.
    i0 = eq_full.index.get_loc(r.index[0])
    d_start = eq_full.index[max(i0 - 1, 0)]
    cal_years = (r.index[-1] - d_start).days / 365.25
    cagr = (1.0 + total) ** (1.0 / cal_years) - 1.0 if cal_years > 0 else np.nan

    # Vol/Sharpe/Sortino annualise on 252 trading days, as specified.
    sd = float(r.std(ddof=1))
    vol = sd * np.sqrt(TRADING_DAYS)
    sharpe = float(r.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else np.nan
    # Sortino uses the textbook downside deviation (Sortino & Price): the RMS of
    # min(r, 0) over EVERY observation, not the std of the negative subset only.
    dd = float(np.sqrt((r.clip(upper=0.0) ** 2).mean()))
    sortino = float(r.mean() / dd * np.sqrt(TRADING_DAYS)) if dd > 0 else np.nan
    # Drawdown is measured inside the window, from the window's own high-water mark.
    mdd = _max_drawdown(eq / eq.iloc[0])
    calmar = cagr / abs(mdd) if mdd and mdd < 0 else np.nan

    monthly = (1.0 + r).groupby(r.index.to_period("M")).prod() - 1.0
    hit = float((monthly > 0).mean() * 100.0) if len(monthly) else np.nan

    # Turnover: one-way = 0.5 * sum|dw| per rebalance, annualised over the window.
    w = book["weights"]
    risk_cols = [c for c in w.columns if c != "CASH" and (
        c != CASH_PROXY or not use_cash_proxy)]
    tr = book["trades"]
    if len(tr):
        tr = tr[(tr["date"] >= lo) & (tr["date"] <= hi)]
    eq_at = eq_full.reindex(tr["date"]).to_numpy() if len(tr) else np.array([])
    one_way = float((tr["notional"].to_numpy() / eq_at).sum() / 2.0) if len(tr) else 0.0
    turnover = one_way / cal_years if cal_years > 0 else np.nan

    # Average holdings counts risk positions: every ticker column except the
    # cash proxy and the residual CASH column (so DBA buy-and-hold reads 1.00).
    wwin = w[(w.index >= lo) & (w.index <= hi)]
    risk = [c for c in wwin.columns if c not in ("CASH", CASH_PROXY)]
    avg_hold = float((wwin[risk].abs() > WEIGHT_TOL).sum(axis=1).mean())
    cost = book["costs"]
    cost_usd = float(cost[(cost.index >= lo) & (cost.index <= hi)].sum())

    return {"period": period, "CAGR": cagr, "TotalReturn": total, "Vol": vol,
            "Sharpe": sharpe, "Sortino": sortino, "MaxDD": mdd, "Calmar": calmar,
            "HitMonths%": hit, "TurnoverAnn": turnover, "AvgHoldings": avg_hold,
            "CostDrag$": cost_usd, "Months": int(len(monthly)), "Days": n,
            "CalYears": cal_years}


_METRIC_COLS = ["CAGR", "TotalReturn", "Vol", "Sharpe", "Sortino", "MaxDD",
                "Calmar", "HitMonths%", "TurnoverAnn", "AvgHoldings", "CostDrag$"]


def print_metrics_table(df: pd.DataFrame, period: str) -> None:
    sub = df[df["period"] == period]
    if sub.empty:
        print(f"  (no data for {period})")
        return
    hdr = (f"{'book':<12}{'CAGR':>8}{'TotRet':>10}{'Vol':>8}{'Sharpe':>8}"
           f"{'Sortino':>9}{'MaxDD':>8}{'Calmar':>8}{'Hit%':>7}{'Turn':>7}"
           f"{'Hold':>6}{'Cost$':>10}")
    print(hdr)
    print("-" * len(hdr))
    for _, row in sub.iterrows():
        print(f"{row['book']:<12}{row['CAGR']:>7.2%} {row['TotalReturn']:>9.2%} "
              f"{row['Vol']:>7.2%} {row['Sharpe']:>8.2f} {row['Sortino']:>9.2f} "
              f"{row['MaxDD']:>7.2%} {row['Calmar']:>8.2f} {row['HitMonths%']:>6.1f} "
              f"{row['TurnoverAnn']:>6.2f} {row['AvgHoldings']:>6.2f} "
              f"{row['CostDrag$']:>9,.0f}")


# =============================================================================
# PLOTS
# =============================================================================

def make_plots(books: Dict[str, Dict[str, object]], outdir: str,
               train_end: pd.Timestamp) -> None:
    figs = os.path.join(outdir, "figs")
    os.makedirs(figs, exist_ok=True)
    trio = [("STRATEGY", "Strategy (dual momentum)"), ("EW4", "EW four-pack"),
            ("DBA_BH", "DBA buy & hold")]

    fig, ax = plt.subplots(figsize=(12, 6.5))
    for key, lbl in trio:
        ax.plot(books[key]["equity"], label=lbl, lw=1.5)
    ax.set_yscale("log")
    ax.axvline(train_end, color="grey", ls="--", lw=1)
    ax.text(train_end, ax.get_ylim()[1], " TRAIN | TEST", va="top",
            fontsize=8, color="grey")
    ax.set_title("Equity curves (log scale), net of commission + slippage")
    ax.set_ylabel("Portfolio value (USD)")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(os.path.join(figs, "equity_curves.png"), dpi=140)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5.5))
    for key, lbl in trio:
        ax.plot(drawdown_series(books[key]["equity"]) * 100, label=lbl, lw=1.2)
    ax.axvline(train_end, color="grey", ls="--", lw=1)
    ax.set_title("Drawdown from running peak")
    ax.set_ylabel("Drawdown (%)")
    ax.legend(loc="lower left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(figs, "drawdowns.png"), dpi=140)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5.5))
    for key, lbl in trio:
        eq = books[key]["equity"]
        ax.plot((eq / eq.shift(TRADING_DAYS) - 1.0) * 100, label=lbl, lw=1.2)
    ax.axhline(0, color="black", lw=0.8)
    ax.axvline(train_end, color="grey", ls="--", lw=1)
    ax.set_title(f"Rolling {TRADING_DAYS}-day (12m) total return")
    ax.set_ylabel("Return (%)")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(figs, "rolling_12m_return.png"), dpi=140)
    plt.close(fig)

    w = books["STRATEGY"]["weights"]
    cols = [c for c in SLEEVES if c in w.columns]
    cash = w["CASH"].copy()
    if CASH_PROXY in w.columns:
        cash = cash + w[CASH_PROXY].fillna(0.0)
    stack = pd.concat([w[cols].fillna(0.0).clip(lower=0.0),
                       cash.clip(lower=0.0).rename("CASH")], axis=1)
    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.stackplot(stack.index, *[stack[c].to_numpy() * 100 for c in stack.columns],
                 labels=list(stack.columns), alpha=0.9)
    ax.axvline(train_end, color="black", ls="--", lw=1)
    ax.set_ylim(0, 100)
    ax.set_title("Strategy sleeve weights (actual, drifted)")
    ax.set_ylabel("Weight (%)")
    ax.legend(loc="upper left", ncols=len(stack.columns), fontsize=8)
    ax.margins(x=0)
    fig.tight_layout()
    fig.savefig(os.path.join(figs, "weights_area.png"), dpi=140)
    plt.close(fig)
    print(f"  figs/ -> equity_curves.png, drawdowns.png, rolling_12m_return.png, "
          f"weights_area.png")


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="Ag commodity ETF dual-momentum backtest")
    ap.add_argument("--data-dir", default="data", help="price cache (default ./data)")
    ap.add_argument("--outdir", default=".", help="where csv/ and figs/ are written")
    ap.add_argument("--offline", action="store_true", help="use cache only")
    ap.add_argument("--refresh", action="store_true", help="ignore cache, re-download")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    rule("ASSUMPTIONS")
    print(f"""  Universe      sleeves {SLEEVES}, benchmark {BENCHMARK}, cash proxy {CASH_PROXY}
  Sample        {START} -> last available   (TRAIN <= {TRAIN_END}, TEST after)
  Signal        eligible if {MOM_LEN}d momentum > 0 AND close > {SMA_LEN}d SMA
                rank eligible by {MOM_LEN}d momentum desc, hold top K={K} equal weight
                1 eligible -> 100% that name; 0 eligible -> 100% cash
  Timing        signal at month-end CLOSE, filled at the NEXT session's OPEN
                (adjusted open); missing open -> that session's close, logged
  Costs         {COMMISSION_BPS:.2f} bps commission + {SLIPPAGE_BPS:.2f} bps slippage
                = {COST_BPS:.2f} bps per side, charged on traded notional only
  Capital       {START_CAPITAL:,.0f} USD, long only, no leverage, always 100% allocated
  Prices        Adj Close for all returns and signals (corporate actions handled);
                forward-filled at most {FFILL_LIMIT} sessions, longer gap -> name
                dropped from that rebalance
  Not applied   no expense-ratio deduction (already in price), no vol targeting,
                no parameter optimisation, no intra-month trades
  Note          {MOM_LEN_SHORT}d momentum is computed and reported but, per spec, is
                NOT part of eligibility or ranking
  Timezone      {TZ}""")

    raw, use_cash_proxy = load_universe(args.data_dir, args.offline, args.refresh)
    panel = build_panel(raw)
    cal = backtest_calendar(raw, START)
    ind = compute_indicators(panel)
    missing_data_report(raw, panel, cal, ind)

    schedule = rebalance_schedule(cal, panel["analysis_cal"])
    train_end = pd.Timestamp(TRAIN_END)
    lo, hi = cal[0], cal[-1]

    rule("RUNNING BOOKS")
    print(f"  window {lo.date()} -> {hi.date()}  ({len(cal)} sessions, "
          f"{len(schedule)} rebalances)")
    books: Dict[str, Dict[str, object]] = {}
    books["STRATEGY"] = run_book("STRATEGY",
                                 lambda d: strategy_weights(d, ind, panel),
                                 cal, panel, schedule, use_cash_proxy)
    books["EW4"] = run_book("EW4", lambda d: ew4_weights(d, panel),
                            cal, panel, schedule, use_cash_proxy)
    books[f"{BENCHMARK}_BH"] = run_book(
        f"{BENCHMARK}_BH", lambda d, t=BENCHMARK: buy_hold_weights(t),
        cal, panel, schedule, use_cash_proxy)
    for t in SLEEVES:
        books[f"{t}_BH"] = run_book(f"{t}_BH", lambda d, tk=t: buy_hold_weights(tk),
                                    cal, panel, schedule, use_cash_proxy)

    rows = []
    for key, bk in books.items():
        for period, a, b in (("TRAIN", lo, train_end), ("TEST", train_end + pd.Timedelta(days=1), hi), ("FULL", lo, hi)):
            m = perf_metrics(bk, period, a, b, use_cash_proxy)
            m["book"] = key
            rows.append(m)
    metrics = pd.DataFrame(rows)
    metrics = metrics[["book", "period"] + _METRIC_COLS
                      + ["Months", "Days", "CalYears"]]

    for period, label in (("TRAIN", f"TRAIN  {lo.date()} -> {train_end.date()}"),
                          ("TEST", f"TEST   {(train_end + pd.Timedelta(days=1)).date()} -> {hi.date()}"),
                          ("FULL", f"FULL   {lo.date()} -> {hi.date()}")):
        rule(label)
        print_metrics_table(metrics, period)
    print("""
  Definitions. CAGR compounds TotalReturn over calendar years (365.25 days) of
  the span the window's returns cover, so TRAIN and TEST chain-link to FULL
  exactly. Vol, Sharpe and Sortino annualise on 252 trading days with rf = 0.
  Sortino divides by the textbook downside deviation sqrt(mean(min(r,0)^2))
  taken over EVERY observation, not the std of the negative subset. MaxDD runs
  from the window's own high-water mark. Turnover is annualised one-way, i.e.
  traded notional / equity at the fill, halved, divided by calendar years.
  AvgHoldings counts risk positions and excludes cash and the cash proxy, so a
  single-name buy-and-hold book reads 1.00. Cost$ is realised commission plus
  slippage inside the window, so it scales with the book's size at the time.""")

    # ---- 10 worst strategy months --------------------------------------------
    rule("10 WORST STRATEGY MONTHS")
    eq = books["STRATEGY"]["equity"]
    r = _returns(eq)
    monthly = (1.0 + r).groupby(r.index.to_period("M")).prod() - 1.0
    w = books["STRATEGY"]["weights"]
    first_day = pd.Series(w.index, index=w.index).groupby(w.index.to_period("M")).first()
    print(f"{'month':<10}{'return':>10}   holdings at start of month")
    print("-" * 74)
    for period_key, ret in monthly.nsmallest(10).items():
        row = w.loc[first_day[period_key]]
        held = [f"{c} {row[c]:.0%}" for c in SLEEVES
                if c in row.index and row[c] > 0.005]
        if use_cash_proxy and CASH_PROXY in row.index and row[CASH_PROXY] > 0.005:
            held.append(f"{CASH_PROXY} {row[CASH_PROXY]:.0%}")
        if row.get("CASH", 0.0) > 0.005:
            held.append(f"CASH {row['CASH']:.0%}")
        print(f"{str(period_key):<10}{ret:>9.2%}   {', '.join(held) if held else 'flat'}")

    # ---- outputs --------------------------------------------------------------
    rule("WRITING OUTPUTS")
    all_trades = pd.concat([b["trades"] for b in books.values()], ignore_index=True)
    all_trades = all_trades.sort_values(["date", "book", "ticker"])
    all_trades.to_csv(os.path.join(args.outdir, "trades.csv"), index=False)

    sw = books["STRATEGY"]["weights"].copy()
    sw.index.name = "date"
    sw.round(10).to_csv(os.path.join(args.outdir, "weights.csv"))

    equity = pd.DataFrame({
        "strategy": books["STRATEGY"]["equity"],
        "ew4": books["EW4"]["equity"],
        BENCHMARK: books[f"{BENCHMARK}_BH"]["equity"],
        **{t: books[f"{t}_BH"]["equity"] for t in SLEEVES}})
    equity.index.name = "date"
    equity.round(6).to_csv(os.path.join(args.outdir, "equity.csv"))

    metrics.to_csv(os.path.join(args.outdir, "metrics.csv"), index=False)
    for f in ("trades.csv", "weights.csv", "equity.csv", "metrics.csv"):
        print(f"  {f} -> {os.path.join(args.outdir, f)}")
    make_plots(books, args.outdir, train_end)

    if _WARNINGS:
        rule(f"WARNINGS ({len(_WARNINGS)})")
        for m in _WARNINGS[:40]:
            n = _WARN_COUNTS.get(m, 1)
            print(f"  - {m}" + (f"  (x{n} across books)" if n > 1 else ""))
        if len(_WARNINGS) > 40:
            print(f"  ... and {len(_WARNINGS) - 40} more")

    rule("CAVEAT")
    print("""  These are futures-based products, not warehouses of grain. WEAT, CORN, SOYB
  and CANE hold laddered CBOT/ICE contracts and must roll them before expiry, so
  in contango the fund sells a cheap expiring contract and buys a dearer deferred
  one and bleeds; in backwardation it earns the reverse. DBA layers its own
  multi-commodity roll rules on top. Over a 12-month horizon that roll yield can
  easily dominate the spot move, so an ETF drawdown is not a statement about the
  price of wheat and this backtest measures the tradable ETF, not the crop. On
  top of that these are small funds - daily dollar volume is thin and spreads are
  wide relative to the 15 bps per side assumed here - so a strategy that
  concentrates into two sleeves at month-end has real capacity limits, and the
  modelled fills at the open are optimistic at size. Treat the results as a study
  of a rules-based rotation on these specific tickers, nothing more.""")
    print()


if __name__ == "__main__":
    main()
