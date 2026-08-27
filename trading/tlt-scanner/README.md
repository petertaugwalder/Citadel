# TLT Duration Scanner

Terminal scanner for swing-trading **TLT** (iShares 20+ Year Treasury ETF), with
**ZB futures** (30y T-Bond) and the **30y yield (^TYX)** as confirming tape.
TLT is the only trade vehicle.
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
| `python tlt_scanner.py --watch 900 --notify` | Re-scan every 15 min, macOS notification on a new BUY |
| `python tlt_scanner.py --account 50000 --risk 1.0` | Adds position sizing (risk 1% of $50k per trade) |
| `python tlt_scanner.py --entry 82.50` | Adds your open P&L and R-multiple to the exit engine |
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
50-day slope, 50 vs 200) scored across TLT and ZB, plus the same tests inverted
on ^TYX. The regime never generates entries — it decides **size and holding
period**: bear regime = rent bounces; transition = scout; bull = hold and add.

**Layer 2 — TRIGGERS**:

- **BOUNCE** (mean reversion, any regime): RSI(14) < 32 within the last 10 bars,
  RSI hooks up through 35, and price reclaims the 9-EMA (or MACD histogram rises
  two bars). Capitulation → exhaustion → first demand. In a bear regime, profits
  are taken into the 50/200-day band, where bear-market rallies die.
- **TREND-TURN STACK** (8 conditions): TLT 9>21 EMA, MACD cross, 50-day reclaim,
  50-day slope up, higher swing low; ZB momentum cross, ZB 50-day reclaim;
  ^TYX below its 50-day. Tiers: **3/8 = SCOUT** (1/3 size), **5/8 = CONFIRMED**
  (2/3), **close > 200-day = REGIME FLIP** (full). Bottoms are processes — each
  condition is a brick, and confirmation deliberately costs a worse price in
  exchange for better odds.

**Layer 3 — CROSS-CHECKS**: TLT bullish RSI divergence, ^TYX yield-exhaustion
divergence, ZB/TLT momentum disagreement (ZB leads by hours), and the DBA
commodity tape as a **coarse secondary inflation flag** — DBA is agriculture
futures, not CPI; food is one slice of bond-relevant inflation, and a DBA
spike can be weather or cocoa saying nothing about 30y term premium.

**Layer 4 — EXIT ENGINE** (the sell signal, evaluated "as if long"):
**EXIT** on a close under the trail (21-EMA for rentals/swings, 50-day after a
regime flip) or under the prior 15-day low (the structure stop). **TRIM** on a
50-day tag-and-reject in a bear regime, or RSI ≥ 70. **CAUTION** when ≥ 2 early
warnings fire: ZB futures lose their 21-EMA, 30y-yield momentum turns back up,
or a bearish RSI divergence forms on the highs. `--notify` alerts on new
EXIT/TRIM verdicts as well as BUYs.

**Risk**: stop = 15-day swing low − 0.5×ATR; size = (account × risk%) ÷
(entry − stop); targets = 50-day, then 200-day / +2R.

## Design notes / accepted tradeoffs

- **Duration math is first-order only.** TLT% ≈ −D × Δyield (in percentage
  points), where D is TLT's *live* effective duration from the issuer — not a
  constant; it shifts with yield levels and coupon mix (~15 as of late Aug
  2026, per BlackRock ~14.97). Convexity, curve twist, dividends, and NAV
  premium/discount mean realized moves won't match the estimate.
- **One market, three quotes.** Cash 30y yields, ZB, and TLT co-move in
  overlapping hours; there is no strict causal chain. ZB's edge is *hours*
  (Globex Sun 5pm CT–Fri 4pm CT, 1h daily halt): it discovers price while TLT
  is closed, so TLT often gaps at the cash open.
- **ZB is not a 1:1 TLT clone.** Its classic deliverable basket is 15–25y
  remaining maturity vs TLT's 20+y cash basket; basis, cheapest-to-deliver,
  and conversion factors keep them close, not identical. Ultra Bond (UB) is
  the tighter duration proxy if we ever swap; ZB stays for liquidity and the
  23h session.
- **^TNX / 10s30s dropped deliberately.** ^TYX is the better single driver
  for TLT; the accepted cost is no bear-steepener vs bull-flattener
  visibility.
- **The exit rules are risk-management heuristics, not a validated edge.**
  The 15-day lookback is arbitrary, and RSI ≥ 70 will scratch some squeezes
  that keep running. They bound losses; they do not predict.

## Files

- `tlt_scanner.py` — everything (single file, no project structure needed)
- `requirements.txt` — pandas, numpy, yfinance, rich
