# Trading pipeline (NYSE / NASDAQ swing)

Swing, long-only NASDAQ-100 strategy using daily price data + optional Amplitude engagement metrics (weekly publication cadence).

## Setup

```bash
cd /path/to/Untitled
python3 -m venv .venv-trading
source .venv-trading/bin/activate
pip install -r trading/requirements.txt
```

Optional Alpaca paper keys in `.env`:

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=1
```

Amplitude CSV exports:

```
AMPLITUDE_EXPORT_DIR=/path/to/exports
```

## Daily run (production)

Rebalance is **daily** (`config/spec.yaml`: `rebalance: daily`). Run once per US trading session after prices are available.

**Recommended time (ET):** **5:00–5:30 PM** after the NYSE close (prices and same-day bars are final). Alternative: **9:00–9:25 AM** pre-open if you refresh prices overnight and trade at the open.

From repo root (`Untitled`):

```bash
export PYTHONPATH=/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.ingest.fetch_prices --years 1
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.features.build_features
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.strategy.rank_long
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.execution.paper_alpaca          # dry-run
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.execution.paper_alpaca --live  # Alpaca paper orders
```

One-liner (dry-run audit only):

```bash
export PYTHONPATH=/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled && \
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.ingest.fetch_prices --years 1 && \
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.features.build_features && \
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.strategy.rank_long && \
/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled/.venv-trading/bin/python -m trading.execution.paper_alpaca
```

### macOS cron (weekdays, post-close ET)

Edit crontab (`crontab -e`). Example: **5:15 PM ET** Mon–Fri (cron uses the machine’s local timezone; set `TZ` if your Mac is not US/Eastern):

```cron
15 17 * * 1-5 cd /Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled && \
  export PYTHONPATH=/Users/maciejsmoczynski/Documents/GitHub/citadel/Untitled TZ=America/New_York && \
  .venv-trading/bin/python -m trading.ingest.fetch_prices --years 1 && \
  .venv-trading/bin/python -m trading.features.build_features && \
  .venv-trading/bin/python -m trading.strategy.rank_long && \
  .venv-trading/bin/python -m trading.execution.paper_alpaca --live \
  >> trading/data/logs/daily_cron.log 2>&1
```

Use `paper_alpaca` without `--live` for dry-run. Ensure `trading/data/logs/` exists or change the log path.

## Full pipeline (initial backfill / research)

```bash
python trading/run_pipeline.py --years 3
```

Steps: universe → prices (yfinance or Alpaca) → Amplitude sample/CSV → features → ranks → backtest → paper dry-run audit.

## Individual commands

```bash
python -m trading.ingest.fetch_universe --years 5
python -m trading.ingest.fetch_prices --years 5
python -m trading.ingest.fetch_amplitude --sample
python -m trading.features.build_features
python -m trading.strategy.rank_long
python -m trading.backtest.run_backtest
python -m trading.execution.paper_alpaca          # dry-run
python -m trading.execution.paper_alpaca --live  # Alpaca paper orders
```

## Config

- [`config/spec.yaml`](config/spec.yaml) — horizon, **daily rebalance**, sizing, costs, lag
- [`config/ticker_map.yaml`](config/ticker_map.yaml) — Amplitude key → symbol
- [`config/nasdaq100_symbols.yaml`](config/nasdaq100_symbols.yaml) — universe

Set `rebalance: weekly` and uncomment `rebalance_day` to revert to weekly backtests.

## Outputs

Data under `trading/data/` (gitignored). See [`store/schema.md`](store/schema.md).

## Limitations

- **Amplitude alt-data** is weekly (or slower); `fetch_amplitude` / sample metrics do not change daily. Price-based signals (`rs_20d`, SMA, ADV) refresh every run.
- Daily rebalance increases turnover vs weekly; backtest costs in `spec.yaml` apply on each rebalance.
- Backtest does not guarantee future returns. Do not trade on MNPI.

## Compliance

Do not trade on MNPI. Map only issuers you are permitted to analyze.
