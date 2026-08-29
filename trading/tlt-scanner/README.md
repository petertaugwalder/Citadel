# TLT Duration Scanner

Terminal scanner for swing-trading **TLT** (iShares 20+ Year Treasury ETF), with
**UB futures** (Ultra T-Bond) and the **30y yield ($TYX)** as confirming tape.
**The tape is the product; `--allocate` is an experimental allocator layered
on top** (binary gate, details below).
Tape rows: **TLT / UB / $TYX** (the duration triangle). Fetched quietly as a
derived input, never shown as a row: **$TNX** (10s30s one-liner).
Daily (end-of-day) histories and option chains come from the Schwab Trader API
only and are cached locally. There is no alternate market-data vendor fallback.
Built for iTerm — rich TUI dashboard with a plain-ANSI fallback.

> Decision support, not financial advice. Signals fire on daily closes; act the
> next session. Backtest before trusting any rule with money.

## Quickstart (iTerm)

```bash
cd trading/tlt-scanner
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python schwab_client.py login     # one-time browser auth; re-run weekly

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
P&L, or `UNAVAILABLE`; plus the 10s30s
STEEPENING/FLATTENING one-liner. Only bear-steepening during an active
bounce adds a CAUTION-class warning — never an EXIT, never a buy/sell
boolean.

## Backtest verdicts (real data)

> **Stale — different data source.** The figures below came from
> dividend-adjusted Yahoo bars. The scanner now reads raw Schwab prints, so both
> the levels and the return series differ. Re-run `--backtest` for current
> numbers; the shape of the conclusion does not change.

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

## Schwab connection (all live market data)

The scanner uses Schwab's Trader API exclusively for TLT, `/UB`, `$TYX`,
`$TNX`, and the TLT option chain. Missing Schwab data fails closed; it is never
replaced with another vendor or a modeled option chain. **Credentials never
live in this repo.**

```bash
# 1. Register an app at developer.schwab.com (Market Data API is enough for the
#    call panel). Set the callback URL to exactly:  https://127.0.0.1:8182
# 2. Put the credentials in your shell (or ~/.config/tlt-scanner/schwab.json, chmod 600):
export SCHWAB_APP_KEY='...'
export SCHWAB_APP_SECRET='...'
# 3. One-time browser auth — prints a URL, you paste back the redirected URL:
python schwab_client.py login
python schwab_client.py status          # token ages
python schwab_client.py chain TLT
# 4. The scanner picks it up automatically:
python tlt_scanner.py --options
```

- Access tokens last ~30 minutes and refresh automatically; **refresh tokens
  last 7 days**, so `login` has to be re-run weekly. `status` tells you when.
- Tokens are written to `~/.config/tlt-scanner/schwab_tokens.json` (chmod 600),
  outside the repo. `schwab.json`, `schwab_tokens.json` and `.env` are
  gitignored. `logout` deletes the stored tokens.
- `--options-source schwab` is the only accepted source.
- **What Schwab does not fix:** it serves the *current* chain only. There are
  no historical option marks, so any option backtest stays ETF-path timing and
  still measures no realised call P&L.

## Design notes / accepted tradeoffs

- **Levels are raw prices.** Everything the scanner prints or signals on -- the
  50/200-day, the EMAs, stops, targets, the invalidation price -- is an
  unadjusted print, so it matches the chart the trade is placed against.
  Dividend back-adjustment drags a moving average low in proportion to its
  lookback: on TLT's ~4.2% distribution yield that is ~1.7% on the 200-day
  (~1.4 points), enough to call a regime flip more than a point before any chart
  shows it. Schwab serves raw prints, so this is what arrives.
- **Returns are price-only, and the backtest says so.** Schwab publishes no
  adjusted close and no historical distribution series, so the `TR` column
  equals `Close` and `--backtest` measures price return on **both** the strategy
  and buy-and-hold. That understates holding TLT by its entire dividend stream —
  a caveat line states this on every run rather than passing price return off as
  total return. The plumbing for real total return is intact: give `TR` a
  genuine total-return series and both sides pick it up automatically.
- **Duration math is first-order only.** Schwab does not publish issuer
  Effective Duration. D-beta is therefore estimated from 63 sessions of
  Schwab TLT returns versus Schwab $TYX yield changes, with sample size and
  R² printed. It is an empirical sensitivity, not official fund duration.
- **One market, three quotes.** Cash 30y yields, UB, and TLT co-move in
  overlapping hours; there is no strict causal chain. UB's edge is *hours*
  (Globex Sun 5pm CT–Fri 4pm CT, 1h daily halt): it discovers price while TLT
  is closed, so TLT often gaps at the cash open.
- **UB is the only futures leg.** Ultra T-Bond's 25y+ deliverable basket is
  the closest listed futures to TLT's 20+y cash basket. Not a 1:1 clone —
  basis, cheapest-to-deliver, and conversion factors keep them close, not
  identical — but it is the tightest available proxy. A missing UB feed
  degrades the scan like a missing $TYX; no substitute is invented.
- **$TNX is not a watchlist ticker.** It is fetched privately only for the
  10s30s display line (bear steepener = worse for TLT, bull flattener =
  better) — there is no curve trade and no curve EXIT. $TYX remains the
  single driver in the signal logic.
- **Duration is displayed, used nowhere.** If the Schwab-only empirical beta
  fails validation it displays unavailable; no issuer/Yahoo/constant fallback
  is substituted. The residual it enables is a cross-check line only.
- **The exit rules are risk-management heuristics, not a validated edge.**
  The 15-day lookback is arbitrary, and RSI ≥ 70 will scratch some squeezes
  that keep running. They bound losses; they do not predict.
- **The backtest is honest but in-sample.** `--backtest` replays the exact
  rules with no look-ahead (signals on close T, fills at open T+1, the
  swing-low pivot gets its 4-bar confirmation delay, costs per side) — but
  the rules were designed while looking at this same period, and it is one
  instrument over one short window. Descriptive, not predictive.

## Files

- `tlt_scanner.py` — everything (single file, no project structure needed)
- `schwab_client.py` — Schwab OAuth, price history, option chains
- `test_price_basis.py` — locks the raw-levels / distribution-accounting split
- `test_fetch_shape.py` — locks the Schwab fetch/cache contract (raw OHLC + `TR`)
- `requirements.txt` — pandas, numpy, rich

## Changelog

- ZB removed, UB only.
- Schwab Trader API is the only live market-data source; all vendor/model fallbacks removed.
- Local web dashboard (webapp.py): same engines, browser UI, /api JSON, phone-friendly.
- SCHD removed entirely; the scanner is TLT / UB / ^TYX only.
- Levels and signals moved to raw (unadjusted) prices so they match the chart.
- Market data switched to the Schwab Trader API; yfinance removed entirely.
  Duration is now an empirical TLT/$TYX beta fitted from the Schwab tape, and
  backtest returns are price-only because Schwab publishes no adjusted close.
