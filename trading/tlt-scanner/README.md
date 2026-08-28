# TLT Duration Scanner

Terminal scanner for swing-trading **TLT** (iShares 20+ Year Treasury ETF), with
**UB futures** (Ultra T-Bond) and the **30y yield ($TYX)** as confirming tape.
**The tape is the product; `--allocate` is an experimental allocator layered
on top** (binary gate, details below).
**Two traded legs: TLT and SCHD**, each with its own engine, walled off from
one another. Tape rows: **TLT / UB / $TYX** (the duration triangle) plus
**SCHD** (the equity leg). Fetched quietly as a derived input, never shown as
a row and never required: **$TNX** (10s30s one-liner).
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
| `python tlt_scanner.py --backtest --schd` | Backtest the SCHD leg against Schwab price-only SCHD buy-and-hold |
| `python tlt_scanner.py --backtest --schd --ablate` | 4 SCHD entry/exit variants × 1bp/5bp, with `vsPX` — the call-buyer's benchmark |
| `python tlt_scanner.py --options` | SCHD call panel: expiry, strike, IV, delta, theta, breakeven, dividends forfeited |
| `python tlt_scanner.py --schd-entry 28.50` | Adds open P&L and R-multiple to the SCHD leg |
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

## The SCHD leg — a call-timing overlay

**I buy SCHD calls, not shares.** This leg is a timing overlay for call
entries and exits: it prints setups, watch levels, and invalidation levels —
never a share size or an expiry prescription. The stock buy-and-hold column is **context, not the
objective**; beating an ETF you don't own isn't the bar.

- **Trend gate** (all three, else stand aside): close > 200-day, 50-day >
  200-day, 50-day rising.
- **Entries** (unchanged in the engine/backtest): **PULLBACK** — tagged the
  20-EMA within 3 sessions and closed back above it; **BREAKOUT** — a new
  20-day closing high. The live panel speaks a breakout only as a candidate;
  a pullback is watch context, not an order.
- **Exits** — `--schd-exit`, default **`trend`**:
  | mode | behaviour |
  |---|---|
  | `swing` | flatten on close < 50-day or < 200-day — the share-overlay baseline, kept for comparison |
  | `reduce` | 50-day halves the position (restored on a reclaim); 200-day flattens |
  | **`trend`** | **default** — flatten only on close < 200-day; the 50-day is a trim/roll warning, not an auto-flatten |
- **Hold statistics** stay in `--backtest --schd`; the live panel does not turn
  in-sample ETF-path holds into an expiry recommendation.
- **`--schd-entry`** is an ETF fill reference for open P&L / R on the
  underlying path — *not* your call P&L.
- **Backtest**: `--backtest --schd` prints the swing/reduce/trend comparison
  (trades, time-in-market, total return, maxDD, avg hold, %holds ≤5d, PF,
  2–5 session scratches, ≥60 session holds). Naming a mode prints it in
  detail. This is **ETF-path timing, not marked-to-market call P&L** — a
  3-day loser costs far more in calls than the stock % shows, while actual
  option outcomes remain unmeasured. In-sample caveat applies.

**Not solved:** there are no historical SCHD option marks here, so nothing
measures actual call P&L. The overlay times the underlying; the option
outcome depends on strike, expiry, IV and spread at your fill.

## Buying SCHD calls (`--options`)

A call holder captures the **price leg only** — SCHD's ~3.7% dividend accrues
to shareholders and is already discounted into the forward. So the honest
benchmark is **price-only buy-and-hold** (the `vsPX` column in the ablation),
not the dividend-adjusted number, and the share backtest *understates* churn
costs badly: a −0.5% share scratch is roughly −15/−25% on a 30-delta call once
spread and theta are paid. SCHD's option book is also thin, so spreads matter.

`--options` always shows the top-ranked call and put for both TLT and SCHD.
The 50–75 day window, delta 0.65–0.80, spread 10%, open-interest 100, volume 5
and breakeven 8% levels are ranking preferences only: crossing one adds a
visible warning but never hides the contract. The 100-point score weights
spread 30, liquidity 25, theta 15, delta fit 15, expiry fit 10 and breakeven 5.
Only data-integrity failures are excluded: an unsuccessful/delayed/truncated
chain, stale quote, invalid two-sided market, malformed contract or non-standard
deliverable. Signal state controls alerts, not whether the best contract is
displayed. Greeks, IV, bid/ask, volume and open interest come directly from
Schwab; there is no modeled/vendor fallback.

**Layer 4 — EXIT ENGINE** (the sell signal, evaluated "as if long"):
**EXIT** on a close under the trail (21-EMA for rentals/swings, 50-day after a
regime flip) or under the prior 15-day low (the structure stop). **TRIM** on a
50-day tag-and-reject in a bear regime, or RSI ≥ 70. **CAUTION** when UB loses
its 21-EMA during an active TLT bounce, or when ≥ 2 other early warnings fire:
30y-yield momentum turns back up or a bearish RSI divergence forms on the highs.
`--notify` alerts on new
EXIT/TRIM verdicts as well as BUYs.

**Risk**: stop = 15-day swing low − 0.5×ATR; size = (account × risk%) ÷
(entry − stop); targets = 50-day, then 200-day / +2R.

## Backtest verdicts (real data)

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

**SCHD, 2012-08-06 → 2026-08-27** (Schwab price-only, 1bp/side), by exit mode:

| mode | total | CAGR | maxDD | Sharpe | TIM | trades | WR | PF | avg hold |
|---|---|---|---|---|---|---|---|---|---|
| `swing` | +10.2% | — | −24.4% | — | 38.2% | 38 | 37.8% | 1.16 | 37d |
| `reduce` | +62.8% | — | −28.6% | — | 57.4% | 15 | 35.7% | 2.24 | 134d |
| **`trend`** | **+76.6%** | — | −32.4% | — | 57.4% | 15 | 35.7% | **2.30** | 134d |
| B&H (PX) | +266.5% | — | −33.4% | — | 100% | — | — | — | — |

The default `trend` mode has fewer, longer holds than `swing`, which is the shape
a call buyer needs. All modes still trail Schwab price-only buy-and-hold. These
are ETF-path timing results, not historical option returns.

### SCHD calls: no validated edge (Schwab-based test, 2026-08)

A separate backtest against **live Schwab price history** (3,734 sessions,
2011-10-20 → 2026-08-27; price-only SCHD +318.29%, 10.11% CAGR, −33.37% maxDD;
dividends excluded) tested 96 option-model variants with next-session fills,
Schwab-observed spreads and $0.65/contract/side. **Verdict: no configuration
stayed profitable across every sensitivity leg on completed campaigns.**

- Historical model only: the best base result used a 55-day breakout, 240 DTE,
  0.65 delta and 200-day exit. That old 240-DTE assumption is not the live
  selector, which now requires 50–75 days to expiry. That configuration was
  **+$77.08 per initial contract over 17 closed campaigns**, but its
  sensitivity range ran **−$122.58 to +$78.93**: it straddles zero.
- Headline totals that looked stronger were dominated by a **still-open**
  January 2026 campaign.
- 20-day breakouts lost money on completed campaigns; **all 48 "50-day
  reduces to half" variants lost money** — consistent with `reduce`
  underperforming `trend` on the ETF path above.
- 200-day-only exit was the least-bad structure, matching the `trend` default
  here — but least-bad is not an edge.
- **Measured liquidity: median qualifying spread 8.71%, p75 10.62%.** Two real
  identical-contract replays returned −$1.30 and −$41.30 (far too small a
  sample to infer from).
- **Hard limitation:** real Schwab option history covered only four dates and
  two distinct qualifying long-dated contracts, so a genuine multi-month
  option backtest is impossible. The 96 variants are therefore *modelled*
  option P&L over the ETF path, and the model's IV and spread assumptions
  drive the result.

Consequence for this repo: the 55-day breakout is retained as alert context,
not as a contract-selection gate. The contract panel always displays the best
executable Schwab call and put and surfaces preference warnings.

### Stress testing either engine (`--stress`)

The SCHD call verdict turned on a sensitivity range, not a headline number.
`--stress` applies the same discipline to the ETF engines: it splits the
history into contiguous sub-periods, re-runs at 1 / 5 / 10 bps per side, and
reports whether the vs-buy-and-hold result survives **every** leg or straddles
zero.

```bash
python tlt_scanner.py --backtest --stress                 # TLT
python tlt_scanner.py --backtest --stress --schd          # SCHD (trend mode)
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
python webapp.py --entry 83.10 --schd-entry 34.20 --options --account 50000
python webapp.py --port 9000 --refresh-min 10
```

Cards: tape (with a 60-session TLT sparkline), TLT regime + stack checklist,
TLT plan with levels and sizing, TLT exit engine with the invalidation price,
the SCHD call-timing overlay, an optional SCHD calls panel, and "what flips
it". Light/dark follow the OS; the layout collapses to one column on a phone.
`/api` serves the same data as JSON, `/health` for uptime checks.

Numbers come from the identical `analyze()` call the CLI uses, cached for
`--refresh-min` (default 15) with a manual **refresh** link.

## Schwab connection (all live market data)

The scanner uses Schwab's Trader API exclusively for TLT, SCHD, `/UB`, `$TYX`,
`$TNX`, and the SCHD call chain. Missing Schwab data fails closed; it is never
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
python schwab_client.py chain SCHD      # sanity check
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
  no historical option marks, so the SCHD backtest stays ETF-path timing and
  still measures no realised call P&L.

## Design notes / accepted tradeoffs

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
- `requirements.txt` — pandas, numpy, rich

## Changelog

- ZB removed, UB only.
- DBA and DBC removed; SCHD added display-only.
- SCHD promoted to a traded leg with its own trend engine and backtest.
- SCHD ablation (4 entry/exit variants), price-only benchmark, and a call-buyer options panel.
- SCHD reframed as a call-timing overlay: --schd-exit swing/reduce/trend (default trend), no share sizing.
- Schwab Trader API is the only live market-data source; all vendor/model fallbacks removed.
- Local web dashboard (webapp.py): same engines, browser UI, /api JSON, phone-friendly.
