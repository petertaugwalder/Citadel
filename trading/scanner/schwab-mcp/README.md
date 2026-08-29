# Schwab market data MCP server

Exposes the Schwab Trader API's market data to Claude Code as MCP tools: quotes,
price history, option chains, and a one-call export of the CSV that
`../backtest.js --load` reads.

Runs locally over stdio. Your credentials stay in your environment — they are
never written to disk by this server, never logged, and never included in an
error message.

## Setup

**1. Register an app** at [developer.schwab.com](https://developer.schwab.com):
add the **Market Data** product, and set the callback URL to `https://127.0.0.1`.
Approval takes a day or two. Note the app key and secret.

**2. Install and mint a refresh token:**

```
cd trading/scanner/schwab-mcp
npm install
export SCHWAB_APP_KEY=your_key
export SCHWAB_APP_SECRET=your_secret
export SCHWAB_CALLBACK_URL=https://127.0.0.1
node auth.js
```

`auth.js` prints a login URL, you approve access in the browser, and you paste
the redirect URL back. It prints an `export SCHWAB_REFRESH_TOKEN=...` line for
your shell profile. **Schwab refresh tokens expire after about seven days**, so
this is a recurring chore; the server tells you plainly when yours has lapsed.

**3. Register the server with Claude Code:**

```
claude mcp add schwab --env SCHWAB_APP_KEY=$SCHWAB_APP_KEY \
  --env SCHWAB_APP_SECRET=$SCHWAB_APP_SECRET \
  --env SCHWAB_REFRESH_TOKEN=$SCHWAB_REFRESH_TOKEN \
  -- node $(pwd)/server.js
```

Then `/mcp` in Claude Code should list `schwab` with four tools.

## Tools

| Tool | What it does |
|---|---|
| `schwab_get_quotes` | Last, net change, bid/ask, volume and 52-week range for up to 25 symbols |
| `schwab_get_price_history` | Daily/weekly/monthly candles for one symbol. Returns a compact summary by default; `output: "rows"` for recent bars, `output: "file"` to write the full series |
| `schwab_get_option_chain` | Chain flattened to strike, expiration, DTE, bid/ask/mark, delta, IV, open interest and volume — filterable by type, strike count and date range |
| `schwab_export_backtest_csv` | Fetches several symbols and writes the `ticker,date,close` file for `backtest.js --load` |

Full history is thousands of rows, so the history and chain tools summarize and
cap by default rather than flooding the conversation; ask for `rows` or a bigger
`limit` when you actually need the detail.

## Using it with the backtest

In a Claude Code session with this server registered:

> Export daily history for WEAT, CORN, SOYB, CANE and DBA to ./bars.csv, then run
> `node backtest.js --load ./bars.csv`

That is the whole loop — Schwab for the data, `backtest.js` for the analysis.

## Caveats

**Schwab candles are not distribution-adjusted.** Total return and CAGR computed
from them exclude distributions, which matters for DBA (~3% yield). Yahoo's
adjusted close is the better series for return math; Schwab matches what your
broker screen shows. The tools say so in their output.

Rate limit is roughly 120 requests/minute; a 429 is reported as such. Option
chains are large — filter with `strikeCount` and a date range.

## Development

```
node server.js --selftest
```

21 offline assertions covering candle parsing across datetime formats, summary
statistics, chain flattening and truncation, quote mapping, CSV shape, and that
credential errors name the missing variable without echoing secrets.
