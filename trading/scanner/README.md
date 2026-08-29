# Ag Commodity Call-Entry Scanner

Terminal scanner for **WEAT · CORN · SOYB · CANE · DBA** that watches live
prices during the NY regular session (09:30–16:00) and fires **sound +
iTerm2 alerts** when a ticker hits a planned call-entry condition. Zero
dependencies — one Node script plus a config file.

```
cd trading/scanner
node commodity-scanner.js --test-sound   # 1. hear each alert sound
node commodity-scanner.js --simulate     # 2. scripted demo of every alert type
node commodity-scanner.js                # 3. live dashboard (leave running in iTerm2)
```

Requires Node ≥ 18 (`node --version`). Or from the repo root: `npm run scan`.

## The plan it encodes

| Ticker | Entry (pullback buy) | Stop ref | Breakout trigger | Extra |
|---|---|---|---|---|
| CORN | 19.20 (May-high retest; zone down to 20-EMA) | 20-EMA (live) | — price discovery | after 15:45, price < 19.20 → failed-breakout warning |
| WEAT | 26.40 (broken double top) | 20-EMA trail (live) | — new highs | |
| SOYB | 26.30 (breakout shelf) | 20-EMA (live) | — new highs | |
| CANE | 20-EMA touch (live) | 200-day (live) | **11.50** flag resolution | |
| DBA | 20-EMA touch (live) | 50-day (live) | **28.80** May high | breakout alert notes sector-wide breadth confirmation |

Levels written as `"ema20"`, `"sma50"`, `"sma200"` in `config.json` are
recomputed **live** from Yahoo daily data each poll (current price folded in
as today's forming close, like an intraday chart), so trailing references
track the tape without manual edits. Fixed numbers (19.20, 26.40, 26.30,
11.50, 28.80) are structural levels — edit `config.json` as the trade
evolves. `fallbackLevels` (chart values as of 2026-08-26) are used only when
daily history can't be fetched; the dashboard marks them with `~`.

If a rising trailing stop climbs above a fixed entry, the buy zone follows
the stop (shown with `^`) — refresh the config when that happens.

## Alert types

| Alert | Meaning | Default sound |
|---|---|---|
| `BUY-DIP` | price pulled back into the buy zone `[stop … entry]` (or reclaimed it from below) | Glass ×2 |
| `BUY-BREAKOUT` | price took out the overhead trigger (CANE 11.50, DBA 28.80) | Hero ×3 |
| `STOP-WARN` | price lost the stop reference — manage open calls | Basso ×3 |
| `CLOSE-WARN` | CORN under 19.20 after 15:45 NY — failed-breakout risk into the close | Sosumi ×2 |
| `CHIME` | session open/close | Ping ×1 |

Every alert: macOS sound (`afplay`) + spoken headline (`say`) + terminal
bell + iTerm2 native notification + dock-bounce attention request, then a
line in the on-screen log and `alerts.log`. Buy/stop alerts only **sound**
during the 09:30–16:00 session (your options window); off-session
transitions are still logged. A 25-minute per-ticker cooldown stops
re-triggering chop (suppressed re-fires are logged with `[cooldown]`);
re-arming uses 0.1% hysteresis around each level. Session open/close comes
from Yahoo's trading-period data when available, so holidays and half-days
are handled; the NY clock is the fallback.

## Flags

| Flag | Effect |
|---|---|
| `--once` | one poll, plain table, exit |
| `--simulate` (`--fast`) | scripted demo exercising every alert type |
| `--test-sound` | play each configured sound, exit |
| `--check` | offline self-test (36 assertions), exit 0/1 |
| `--quiet` | no sounds/voice, visual only |
| `--interval N` | poll every N seconds (default 15 in-session, 60 off) |
| `--config PATH` | alternate config file |

## iTerm2 setup

- Run it in a dedicated pane/window; the tab title shows live prices and the
  badge shows `AG`.
- Notifications: iTerm2 → Settings → Profiles → Terminal → enable
  notification posting (OSC 9) so alerts hit Notification Center even when
  iTerm2 is in the background. The dock-bounce (attention request) fires
  when iTerm2 isn't focused.
- Sounds use macOS system sounds via `afplay` — independent of iTerm2's
  bell settings. Swap files/repeat counts in `config.json` → `sounds.map`
  (anything in `/System/Library/Sounds/`), voice on/off via `sounds.voice`.
- Keep the Mac awake through the session: `caffeinate -dis node commodity-scanner.js`.

## History & seasonality backtest

`backtest.js` pulls full daily history (Yahoo, `range=max`, distribution-adjusted)
for all five and prints: all-time high/low with dates, CAGR, annualized vol, max
drawdown, calendar-year returns, a year x month return grid, average return and
hit rate per calendar month, era splits (pre-COVID / COVID / Ukraine / post-spike
/ current) and a daily-return correlation matrix.

```
node backtest.js
node backtest.js --section seasonality
node backtest.js --ticker WEAT,CORN --from 2015-01-01
node backtest.js --csv ./out
node backtest.js --check
```

Sections: `summary`, `years`, `months`, `seasonality`, `eras`, `corr`. `--csv DIR`
writes summary/monthly/yearly/seasonality CSVs for a spreadsheet. Seasonal
averages over ~15 observations per month are suggestive, not predictive — read
the hit rate next to the average before trusting either.

### Running it where the network can't reach Yahoo

Split the fetch from the analysis. On a machine that can reach Yahoo:

```
node backtest.js --dump bars.csv
node backtest.js --dump bars.csv --monthly
```

`--dump` saves the raw bars it fetched (`ticker,date,close`). Daily is ~21k rows
(~600 KB); `--monthly` keeps month-end closes only, ~1k rows (~25 KB), small
enough to hand around. Then anywhere, with no network:

```
node backtest.js --load bars.csv
```

Every section runs identically off the file. Month-end input is detected from the
bar spacing: volatility is then annualized from monthly returns, and high, low and
drawdown are month-end extremes rather than intraday.

## Data source & caveats

Quotes come from Yahoo Finance's public chart endpoint (no API key). It is
unofficial and usually real-time-ish for NYSE Arca ETFs, but can lag or
throttle — the dashboard flags stale rows with `*` and repeated fetch
errors in the header, and falls back between `query1`/`query2` hosts.
Confirm price and the option chain in your broker before entering an order.
This is an alerting aid for a plan you defined, not trading advice.
