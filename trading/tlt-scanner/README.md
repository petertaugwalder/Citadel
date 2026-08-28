# TLT Duration Scanner

Terminal scanner for swing-trading **TLT** (iShares 20+ Year Treasury ETF), with
**UB futures** (Ultra T-Bond) and the **30y yield ($TYX)** as confirming tape.
**The tape is the product; `--allocate` is an experimental allocator layered
on top** (binary gate, details below).
Tape rows: **TLT / UB / ^TYX** (the duration triangle). Nothing else is
fetched — no curve leg, no fourth instrument, no derived series with a vote.
Daily (end-of-day) bars come from Yahoo Finance via `yfinance` and are cached
locally.
Built for iTerm — rich TUI dashboard with a plain-ANSI fallback.

> Decision support, not financial advice. Signals fire on daily closes; act the
> next session. Backtest before trusting any rule with money.

## Quickstart (iTerm)

```bash
cd trading/tlt-scanner
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python tlt_scanner.py             # one-shot scan (live data)
python tlt_scanner.py --explain   # the trading logic, in the terminal
```

## Commands

| Command | What it does |
|---|---|
| `python tlt_scanner.py` | Full dashboard: tape, regime, triggers, cross-checks, plan |
| `python tlt_scanner.py --history 15` | What the scanner said each of the last 15 sessions |
| `python tlt_scanner.py --backtest` | Replay the buy/sell rules bar-by-bar over the full history (`--cost-bps` sets per-side costs, default 1.0) |
| `python tlt_scanner.py --allocate` | Adds the experimental allocator panel (gate + current position); with `--backtest`, runs the allocator variant |
| `python tlt_scanner.py --ablate --from 2020-01-01` | 4-variant ablation (current / no-SCOUT / no-trail / allocator) at 1bp and 5bp; `--from` sets the window start |
| `python tlt_scanner.py --watch 900 --notify` | Re-scan every 15 min, macOS notification on a new BUY |
| `python tlt_scanner.py --account 50000 --risk 1.0` | Adds position sizing (risk 1% of $50k per trade) |
| `python tlt_scanner.py --entry 82.50` | Adds open P&L and R-multiple to the exit engine, plus a stop-to-breakeven suggestion past +1R |
| `python tlt_scanner.py --json` | Machine-readable output for piping |
| `python tlt_scanner.py --alert-exit` | Exit code 2 on a BUY, 3 on an EXIT — for scripting/cron |
| `python tlt_scanner.py --demo` | Synthetic data (no network) to see the output shape |
| `--refresh` / `--plain` | Force re-download / force plain ANSI output |

Cache lives in `~/.cache/tlt-scanner/` (4h TTL), so repeated runs are instant.

Cron-style alerting without keeping a window open:

```bash
# crontab: weekdays 4:30pm ET, ping only when there's a BUY
30 16 * * 1-5 cd ~/Citadel/trading/tlt-scanner && .venv/bin/python tlt_scanner.py --refresh --alert-exit >/dev/null 2>&1 || .venv/bin/python tlt_scanner.py --notify
```

## The model

**Layer 1 — REGIME** (−100…+100): moving-average structure (price vs 50/200-day,
50-day slope, 50 vs 200) scored across TLT and UB, plus the same tests inverted
on ^TYX. The regime never generates entries — it decides **size and holding
period**: bear regime = rent bounces; transition = scout; bull = hold and add.

**Layer 2 — TRIGGERS**:

- **BOUNCE** (mean reversion, any regime): RSI(14) < 32 within the last 10 bars,
  RSI hooks up through 35, and price reclaims the 9-EMA (or MACD histogram rises
  two bars). Capitulation → exhaustion → first demand. In a bear regime, profits
  are taken into the 50/200-day band, where bear-market rallies die.
- **TREND-TURN STACK** (8 conditions): TLT 9>21 EMA, MACD cross, 50-day reclaim,
  50-day slope up, higher swing low; UB momentum cross, UB 50-day reclaim;
  ^TYX below its 50-day. Buyable tiers require at least one TLT-native box:
  **3/8 = SCOUT** (1/3 size), **5/8 = CONFIRMED**
  (2/3), **close > 200-day = REGIME FLIP** (full). Bottoms are processes — each
  condition is a brick, and confirmation deliberately costs a worse price in
  exchange for better odds.

**Layer 3 — CROSS-CHECKS**: TLT bullish RSI divergence, ^TYX yield-exhaustion
divergence, and UB/TLT momentum disagreement (UB leads by hours).
The aux inputs add display lines here: dated live duration (`D≈…` with the
first-order Δy mapping), a dated cached value marked **STALE** with no implied
P&L, or `UNAVAILABLE`. Display material only — never an EXIT, never a buy/sell
boolean.

## Backtest verdicts (real data)

> **Stale as of the raw-price/total-return split.** Both figures below were
> computed price-only on both sides: dividend-adjusted bars for the strategy and
> a price-only buy-and-hold. Returns now count distributions, which lifts
> buy-and-hold by the full stream and the strategy by only its exposure-weighted
> share — so the gap against holding TLT **widens**. Re-run `--backtest` for
> current numbers; the shape of the conclusion does not change.

**TLT, full history 2010-10-25 → 2026-08-27** (3984 sessions, 1bp/side):
strategy **+8.93%** (max DD −33.13%, Sharpe 0.10) vs buy-and-hold **+28.88%**
(max DD −48.35%, Sharpe 0.18); 51% time in market, 184 trades, win rate 33.2%,
profit factor 1.12, average hold 12 days. It still loses to holding, but over
16 years it is *profitable and roughly halves the drawdown* — a materially
different picture from the 2-year window below.

**TLT, 2024-06-12 → 2026-08-26** (the window first tested): strategy −12.86%
(max DD −18.33%) vs buy-and-hold −1.12% (max DD −14.79%); 29 trades, win rate
20.7%, profit factor 0.34, 23 of 29 exits on the 21-EMA trail. A short, single-
regime sample — kept here because it is what prompted the "tape, not an
allocator" conclusion.

### Stress testing (`--stress`)

A headline number is not an edge. `--stress` splits the history into
contiguous sub-periods, re-runs at 1 / 5 / 10 bps per side, and
reports whether the vs-buy-and-hold result survives **every** leg or straddles
zero.

```bash
python tlt_scanner.py --backtest --stress                 # TLT
python tlt_scanner.py --backtest --stress --blocks 6      # finer sub-periods
```

It prints per-leg trades, return, B&H, vs-B&H, both drawdowns and PF, then a
range line, a count of legs beating B&H, a count of legs with better drawdown,
and a verdict. A result that survives one leg is a parameter choice, not an
edge — and every leg is still in-sample.

**Read all of the above as descriptive, not predictive** — the rules were
written after seeing these tapes.

## The allocator (`--allocate`) — experimental

The tape never changes; the allocator is a separate, deliberately dumb layer
built from thresholds the tape already computes (nothing retuned): **new
longs only when 30y yield < its 50-day AND UB > its 50-day AND TLT > its
50-day; size binary 1.0/0; no SCOUT opens, no bounce opens, no trims; exits
on a close under the prior 15-day low or the 50-day (never the 21-EMA).**
The bounce is now a tape signal only — it is never presented as a new entry.
Judge the allocator with `--ablate`, which compares all four variants at 1bp
and 5bp on the same window; every variant is in-sample.

## Web dashboard (`webapp.py`)

The same engines rendered as a page instead of terminal panels — useful on a
phone or a second monitor. Stdlib only (no Flask), read-only, places no orders.

```bash
python webapp.py                      # http://127.0.0.1:8787
python webapp.py --lan                # also reachable from your phone on the LAN
python webapp.py --port 9000 --refresh-min 10
```

Cards: tape (with a 60-session TLT sparkline), TLT regime + stack checklist,
TLT plan with levels and sizing, TLT exit engine with the invalidation price,
and "what flips it". Light/dark follow the OS; the layout collapses to one column on a phone.
`/api` serves the same data as JSON, `/health` for uptime checks.

Numbers come from the identical `analyze()` call the CLI uses, cached for
`--refresh-min` (default 15) with a manual **refresh** link.

## Design notes / accepted tradeoffs

- **Levels are raw prices; returns are total return.** Everything the scanner
  prints or signals on -- the 50/200-day, the EMAs, stops, targets, the
  invalidation price -- is an unadjusted price, so it matches the chart the
  trade is placed against. Dividend back-adjustment drags a moving average low
  in proportion to its lookback: on TLT's ~4.2% distribution yield that is
  ~1.7% on the 200-day (~1.4 points), enough to call a regime flip more than a
  point before any chart shows it. `--backtest` keeps the same raw bars for
  entries, exits and stops, and accounts for distributions separately through
  the `TR` column, so buy-and-hold is measured as total return rather than
  flattered by leaving the dividend out. A cache written before this split
  stored adjusted closes as `Close`; it is rejected on sight and refetched.
- **Duration math is first-order only.** D is read from the issuer's fund data
  via `yfinance` and cached for a week. `TLT %chg ≈ -D × Δy` ignores convexity,
  curve twist, distributions and NAV premium/discount, so realised moves will
  not match the estimate. It is a cross-check line, never a buy/sell boolean.
- **One market, three quotes.** Cash 30y yields, UB, and TLT co-move in
  overlapping hours; there is no strict causal chain. UB's edge is *hours*
  (Globex Sun 5pm CT–Fri 4pm CT, 1h daily halt): it discovers price while TLT
  is closed, so TLT often gaps at the cash open.
- **UB is the only futures leg.** Ultra T-Bond's 25y+ deliverable basket is
  the closest listed futures to TLT's 20+y cash basket. Not a 1:1 clone —
  basis, cheapest-to-deliver, and conversion factors keep them close, not
  identical — but it is the tightest available proxy. A missing UB feed
  degrades the scan like a missing ^TYX; no substitute is invented.
- **Duration is displayed, used nowhere.** It fails closed: with no live fetch
  and no cache inside a week it prints `UNAVAILABLE` rather than falling back to
  a constant, and a cached value is shown marked **STALE** but never drives the
  implied-move line. The residual it enables is a cross-check line only.
- **The exit rules are risk-management heuristics, not a validated edge.**
  The 15-day lookback is arbitrary, and RSI ≥ 70 will scratch some squeezes
  that keep running. They bound losses; they do not predict.
- **The backtest is honest but in-sample.** `--backtest` replays the exact
  rules with no look-ahead (signals on close T, fills at open T+1, the
  swing-low pivot gets its 4-bar confirmation delay, costs per side) — but
  the rules were designed while looking at this same period, and it is one
  instrument over one short window. Descriptive, not predictive.

## Files

- `tlt_scanner.py` — the scanner (single file, no project structure needed)
- `webapp.py` — local web dashboard over the same `analyze()` call
- `test_price_basis.py` — locks the raw-levels / total-return-returns split
- `test_fetch_shape.py` — locks the fetch/cache contract (raw OHLC + `TR`)
- `test_tlt_scanner.py` — locks the duration contract (fails closed, never stale-as-live)
- `requirements.txt` — pandas, numpy, yfinance, rich

## Changelog

- ZB removed, UB only.
- Local web dashboard (webapp.py): same engines, browser UI, /api JSON, phone-friendly.
- SCHD removed entirely; the scanner is TLT / UB / ^TYX only.
- Levels and signals moved to raw (unadjusted) prices so they match the chart;
  backtest returns now count distributions on both the strategy and buy-and-hold.
- $TNX and the 10s30s curve line removed again (they had regressed back in as an
  "aux input"); the scan is TLT / UB / ^TYX and nothing else.
- Schwab client, dataset downloader, backtest audit and the option-chain selector
  removed — leftovers of an abandoned migration that the scanner never imported.
