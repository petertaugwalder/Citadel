# TLT Duration Scanner

Terminal scanner for swing-trading **TLT** (iShares 20+ Year Treasury ETF), with
**UB futures** (Ultra T-Bond) and the **30y yield (^TYX)** as confirming tape.
**The tape is the product; `--allocate` is an experimental allocator layered
on top** (binary gate, details below).
**Two traded legs: TLT and SCHD**, each with its own engine, walled off from
one another. Tape rows: **TLT / UB / ^TYX** (the duration triangle) plus
**SCHD** (the equity leg). Fetched quietly as a derived input, never shown as
a row and never required: **^TNX** (10s30s one-liner), plus TLT's live
effective duration.
Daily (end-of-day) data from Yahoo Finance, cached locally. Built for iTerm —
rich TUI dashboard with a plain-ANSI fallback.

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
| `python tlt_scanner.py --backtest --schd` | Backtest the SCHD leg against SCHD buy-and-hold (total-return and price-only) |
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
  ^TYX below its 50-day. Tiers: **3/8 = SCOUT** (1/3 size), **5/8 = CONFIRMED**
  (2/3), **close > 200-day = REGIME FLIP** (full). Bottoms are processes — each
  condition is a brick, and confirmation deliberately costs a worse price in
  exchange for better odds.

**Layer 3 — CROSS-CHECKS**: TLT bullish RSI divergence, ^TYX yield-exhaustion
divergence, and UB/TLT momentum disagreement (UB leads by hours).
The aux inputs add display lines here: live duration (`D≈…` with the
first-order Δy mapping, STALE-marked 15.0 fallback), a 5-session
actual-vs-duration-implied residual (cross-check only), and the 10s30s
STEEPENING/FLATTENING one-liner. Only bear-steepening during an active
bounce adds a CAUTION-class warning — never an EXIT, never a buy/sell
boolean.

## The SCHD leg (traded, separate engine)

SCHD is a quality/dividend equity ETF in a durable uptrend, so it gets
**trend rules, not TLT's mean-reversion rules** — nothing is shared between
the two engines, in either direction.

- **Trend gate** (all three, else stand aside): close > 200-day, 50-day >
  200-day, 50-day rising.
- **Entries**: **PULLBACK** — tagged the 20-EMA within 3 sessions and closed
  back above it; **BREAKOUT** — a new 20-day closing high.
- **Exits**: close < 50-day reduces; close < 200-day exits. No 21-EMA trail.
- **Risk**: stop = 20-day swing low − 0.5×ATR; size = (account × risk%) ÷
  (entry − stop); first target +2R. `--schd-entry 28.50` adds open P&L and
  R-multiple.
- **Backtest**: `--backtest --schd`; compare entry/exit families with
  `--backtest --schd --ablate` (4 variants × 1bp/5bp). The in-sample caveat
  applies exactly as it does to TLT.

## Buying SCHD calls (`--options`)

A call holder captures the **price leg only** — SCHD's ~3.7% dividend accrues
to shareholders and is already discounted into the forward. So the honest
benchmark is **price-only buy-and-hold** (the `vsPX` column in the ablation),
not the dividend-adjusted number, and the share backtest *understates* churn
costs badly: a −0.5% share scratch is roughly −15/−25% on a 30-delta call once
spread and theta are paid. SCHD's option book is also thin, so spreads matter.

`--options` therefore picks an expiry at least `--min-dte` days out (default
150 — the winning holds ran 70–110 sessions) and a strike near
`--target-delta` (default 0.70; SCHD grinds, so ITM leverage beats cheap OTM).
It prints ATM IV, premium as % of notional, breakeven and the move required,
theta as % of premium per day, and the dividends forfeited over the hold.
Greeks are Black-Scholes (r=4.5%, q=3.7%) — display-grade, not a pricing
engine.

**Layer 4 — EXIT ENGINE** (the sell signal, evaluated "as if long"):
**EXIT** on a close under the trail (21-EMA for rentals/swings, 50-day after a
regime flip) or under the prior 15-day low (the structure stop). **TRIM** on a
50-day tag-and-reject in a bear regime, or RSI ≥ 70. **CAUTION** when ≥ 2 early
warnings fire: UB futures lose their 21-EMA, 30y-yield momentum turns back up,
or a bearish RSI divergence forms on the highs. `--notify` alerts on new
EXIT/TRIM verdicts as well as BUYs.

**Risk**: stop = 15-day swing low − 0.5×ATR; size = (account × risk%) ÷
(entry − stop); targets = 50-day, then 200-day / +2R.

## Backtest verdict (2024-06-12 → 2026-08-26, real data)

On this window the engine **lost to holding TLT**: −12.86% (max DD −18.33%)
vs buy-and-hold −1.12% (max DD −14.79%), 46% time in market. 29 trades,
win rate 20.7%, profit factor 0.34; 23 of 29 exits were 21-EMA trail stops.
The only two real winners (+3.61%, +2.89%) were ~7-week holds that opened
at SCOUT and ended on STRUCT exits. Costs at 5 bps/side and 0%-cash
crediting do not flip the vs-B&H verdict. Conclusion: **the dashboard is a
tape, not an allocator** — use it for regime context, exit discipline, and
levels; do not auto-trade the BUY tiers, especially flat-state
CONFIRMED/FLIP opens. No thresholds were changed on this result.

## The allocator (`--allocate`) — experimental

The tape never changes; the allocator is a separate, deliberately dumb layer
built from thresholds the tape already computes (nothing retuned): **new
longs only when 30y yield < its 50-day AND UB > its 50-day AND TLT > its
50-day; size binary 1.0/0; no SCOUT opens, no bounce opens, no trims; exits
on a close under the prior 15-day low or the 50-day (never the 21-EMA).**
The bounce is now a tape signal only — it is never presented as a new entry.
Judge the allocator with `--ablate`, which compares all four variants at 1bp
and 5bp on the same window; every variant is in-sample.

## Design notes / accepted tradeoffs

- **Duration math is first-order only.** TLT% ≈ −D × Δyield (in percentage
  points), where D is TLT's *live* effective duration from the issuer — not a
  constant; it shifts with yield levels and coupon mix (~15 as of late Aug
  2026, per BlackRock ~14.97). Convexity, curve twist, dividends, and NAV
  premium/discount mean realized moves won't match the estimate.
- **One market, three quotes.** Cash 30y yields, UB, and TLT co-move in
  overlapping hours; there is no strict causal chain. UB's edge is *hours*
  (Globex Sun 5pm CT–Fri 4pm CT, 1h daily halt): it discovers price while TLT
  is closed, so TLT often gaps at the cash open.
- **UB is the only futures leg.** Ultra T-Bond's 25y+ deliverable basket is
  the closest listed futures to TLT's 20+y cash basket. Not a 1:1 clone —
  basis, cheapest-to-deliver, and conversion factors keep them close, not
  identical — but it is the tightest available proxy. A missing UB feed
  degrades the scan like a missing ^TYX; no substitute is invented.
- **^TNX is not a watchlist ticker.** It is fetched privately only for the
  10s30s display line (bear steepener = worse for TLT, bull flattener =
  better) — there is no curve trade and no curve EXIT. ^TYX remains the
  single driver in the signal logic.
- **Duration is displayed live, used nowhere.** D comes from Yahoo fund data
  on a weekly cache and falls back to 15.0 marked STALE; the
  actual-vs-implied residual it enables is a cross-check line only.
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
- `requirements.txt` — pandas, numpy, yfinance, rich

## Changelog

- ZB removed, UB only.
- DBA and DBC removed; SCHD added display-only.
- SCHD promoted to a traded leg with its own trend engine and backtest.
- SCHD ablation (4 entry/exit variants), price-only benchmark, and a call-buyer options panel.
