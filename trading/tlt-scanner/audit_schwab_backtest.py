#!/usr/bin/env python3
"""Backtest captured underlying paths and rank both option sides from Schwab."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import schwab_client as schwab
import tlt_scanner as scanner

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data" / "schwab"


def load_snapshot(path: str | None) -> tuple[Path, dict]:
    if path:
        root = Path(path).expanduser().resolve()
    else:
        latest = json.loads((DATA_ROOT / "latest.json").read_text())
        root = DATA_ROOT / latest["snapshot"]
    return root, json.loads((root / "manifest.json").read_text())


def load_frames(root: Path) -> dict[str, pd.DataFrame]:
    frames = {}
    for name in ("UB", "TLT", "TYX"):
        raw = pd.read_csv(root / "ohlcv" / f"{name}.csv")
        idx = pd.to_datetime(raw.pop("date"))
        df = pd.DataFrame({
            "Open": raw["open"].to_numpy(), "High": raw["high"].to_numpy(),
            "Low": raw["low"].to_numpy(), "Close": raw["close"].to_numpy(),
            "Volume": raw["volume"].to_numpy(),
        }, index=idx)
        frames[name] = scanner.enrich(df)
    return frames


def jsonify(value):
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", help="snapshot directory; defaults to data/schwab/latest.json")
    args = ap.parse_args()
    root, manifest = load_snapshot(args.snapshot)
    frames = load_frames(root)
    fetched = datetime.fromisoformat(manifest["fetched_at_utc"])
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)

    tlt_bt = {f"variant_{v}": scanner.backtest(frames, variant=v) for v in (1, 2, 3, 4)}
    chains = {name: json.loads((root / "raw" / f"{name}_option_chain.json").read_text())
              for name in ("TLT",)}
    option_audit = {
        name: schwab.best_options(name, now=fetched, chain_data=chain)
        for name, chain in chains.items()
    }
    report = {
        "snapshot": root.name, "source": "Schwab Trader API only",
        "data_quality_status": manifest.get("data_quality_status"),
        "rejected_history_rows_total": manifest.get("rejected_history_rows_total", 0),
        "underlying_backtests": {"TLT": tlt_bt},
        "live_production_state": {
        },
        "live_option_quality_audit": option_audit,
        "capability_matrix": {
            "TLT_CALL": "always top-ranked when executable chain data exists",
            "TLT_PUT": "always top-ranked when executable chain data exists",
        },
        "logic_verdict": "RANKING_ONLY_BOTH_SIDES",
        "limitations": manifest["limitations"] + [
            "Backtests use validated CSV rows; malformed Schwab source bars are excluded and counted in the manifest.",
            "Underlying backtests do not model option premium, IV, theta, assignment, dividends, or historical spreads.",
            "Ranking preferences do not establish directional edge; selections are displayed independently from signal-triggered alerts.",
        ],
    }
    path = root / "backtest_and_option_logic_audit.json"
    path.write_text(json.dumps(report, indent=2, default=jsonify) + "\n")
    print(f"audit: {path}")
    print(f"verdict: {report['logic_verdict']}")
    for ticker in ("TLT",):
        for side in ("call", "put"):
            row = option_audit[ticker][side]
            if row.get("contract_selected"):
                print(f"{ticker} {side.upper()}: {row['expiry']} {row['strike']:.2f} "
                      f"score {row['rank_score']:.2f} warnings={row.get('warnings', [])}")
            else:
                print(f"{ticker} {side.upper()}: {row['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
