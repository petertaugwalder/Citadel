#!/usr/bin/env python3
"""Capture a reproducible, fail-closed Schwab market-data snapshot.

Downloads all daily history Schwab returns for /UB, TLT and $TYX, plus
the current ALL-side option chains for TLT.  Exact JSON responses and
validated/scaled CSVs are kept together with hashes and a quality manifest.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import schwab_client

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data" / "schwab"
HISTORIES = {
    "UB": {"symbol": "/UB", "scale_divisor": 1.0},
    "TLT": {"symbol": "TLT", "scale_divisor": 1.0},
    # Schwab's index bars are ten times the displayed percentage yield.
    "TYX": {"symbol": "$TYX", "scale_divisor": 10.0},
}
CHAINS = {"TLT": "TLT"}


def canonical_bytes(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_history(payload: dict, symbol: str, scale: float) -> tuple[list[dict], dict]:
    candles = payload.get("candles")
    if not isinstance(candles, list) or not candles:
        raise RuntimeError(f"{symbol}: Schwab history has no candles")
    accepted: dict[int, dict] = {}
    failures: dict[str, int] = {}
    input_timestamps: list[int] = []

    def fail(reason: str) -> None:
        failures[reason] = failures.get(reason, 0) + 1

    for candle in candles:
        try:
            ts = int(candle["datetime"])
            o, h, lo, close = (float(candle[k]) for k in ("open", "high", "low", "close"))
            volume = float(candle.get("volume") or 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            fail("malformed_candle")
            continue
        input_timestamps.append(ts)
        if ts <= 0 or not all(math.isfinite(v) for v in (o, h, lo, close, volume)):
            fail("non_finite_or_bad_timestamp")
            continue
        if min(o, h, lo, close) <= 0 or volume < 0:
            fail("non_positive_price_or_negative_volume")
            continue
        if h < max(o, lo, close) or lo > min(o, h, close):
            fail("invalid_ohlc_geometry")
            continue
        if ts in accepted:
            fail("duplicate_timestamp")
            continue
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        accepted[ts] = {
            "date": dt.date().isoformat(), "datetime_ms": ts, "symbol": symbol,
            "open": o / scale, "high": h / scale, "low": lo / scale,
            "close": close / scale, "volume": volume,
            "raw_scale_divisor": scale, "source": "schwab",
        }
    rows = [accepted[k] for k in sorted(accepted)]
    if not rows:
        raise RuntimeError(f"{symbol}: every Schwab candle failed validation: {failures}")
    unordered = sum(b <= a for a, b in zip(input_timestamps, input_timestamps[1:]))
    if unordered:
        failures["unordered_input_pair"] = unordered
    return rows, {
        "input_candles": len(candles), "accepted_rows": len(rows),
        "rejected_rows": len(candles) - len(rows), "quality_failures": failures,
        "first_date": rows[0]["date"], "last_date": rows[-1]["date"],
        "scale_divisor": scale,
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def chain_summary(chain: dict, symbol: str) -> dict:
    checks = {
        "status_success": str(chain.get("status") or "").upper() == "SUCCESS",
        "not_delayed": chain.get("isDelayed") is False,
        "not_truncated": chain.get("isChainTruncated") is False,
        "underlying_positive": float(chain.get("underlyingPrice") or 0) > 0,
        "has_calls": bool(chain.get("callExpDateMap")),
        "has_puts": bool(chain.get("putExpDateMap")),
    }
    counts = {}
    for side, key in (("calls", "callExpDateMap"), ("puts", "putExpDateMap")):
        counts[side] = sum(
            len(contracts or [])
            for strikes in (chain.get(key) or {}).values()
            for contracts in (strikes or {}).values()
        )
    if not all(checks.values()):
        raise RuntimeError(f"{symbol}: current chain rejected: {checks}")
    return {"checks": checks, "contract_counts": counts,
            "underlying_price": float(chain["underlyingPrice"])}


def quote_summary(payload: dict, requested_symbol: str, fetched_ms: int) -> dict:
    if not isinstance(payload, dict) or len(payload) != 1:
        raise RuntimeError(f"{requested_symbol}: quote response has {len(payload) if isinstance(payload, dict) else 'invalid'} records")
    resolved_symbol, record = next(iter(payload.items()))
    quote = record.get("quote") or {}
    reference_price = float(quote.get("mark") or quote.get("lastPrice") or quote.get("closePrice") or 0)
    quote_ms = int(quote.get("quoteTime") or quote.get("tradeTime") or 0)
    age = (fetched_ms - quote_ms) / 1000 if quote_ms > 0 else None
    checks = {
        "realtime": record.get("realtime") is True,
        "positive_reference_price": reference_price > 0,
        "timestamp_present": quote_ms > 0,
        "fresh_within_24h": age is not None and -300 <= age <= 24 * 3600,
    }
    if not all(checks.values()):
        raise RuntimeError(f"{requested_symbol}: live quote rejected: {checks}")
    return {"requested_symbol": requested_symbol, "resolved_symbol": resolved_symbol,
            "asset_main_type": record.get("assetMainType"), "checks": checks,
            "reference_price_raw": reference_price, "quote_timestamp_ms": quote_ms,
            "quote_age_seconds": round(age, 3)}


def main() -> int:
    fetched = datetime.now(timezone.utc)
    stamp = fetched.strftime("%Y%m%dT%H%M%SZ")
    snapshot = DATA_ROOT / stamp
    end_ms = int(time.time() * 1000)
    manifest = {
        "source": "Schwab Trader API only", "fetched_at_utc": fetched.isoformat(),
        "snapshot": stamp, "history_request_start_ms": 0,
        "history_request_end_ms": end_ms, "histories": {}, "quotes": {}, "option_chains": {},
        "limitations": [
            "Schwab price history is OHLCV/price-only; distributions are not total-return adjusted.",
            "Schwab exposes the current option chain here, not historical option marks; actual option P&L cannot be backtested from this snapshot.",
        ],
    }
    for name, cfg in HISTORIES.items():
        payload = schwab_client.price_history(cfg["symbol"], 0, end_ms)
        rows, quality = validate_history(payload, cfg["symbol"], cfg["scale_divisor"])
        raw_path = snapshot / "raw" / f"{name}_history.json"
        csv_path = snapshot / "ohlcv" / f"{name}.csv"
        write_bytes(raw_path, canonical_bytes(payload))
        write_csv(csv_path, rows)
        manifest["histories"][name] = {
            "symbol": cfg["symbol"], **quality,
            "raw_path": str(raw_path.relative_to(ROOT)),
            "csv_path": str(csv_path.relative_to(ROOT)),
            "raw_sha256": sha256_file(raw_path), "csv_sha256": sha256_file(csv_path),
        }
        print(f"{cfg['symbol']}: {len(rows)} valid rows {quality['first_date']} -> {quality['last_date']}")
    total_rejected = sum(v["rejected_rows"] for v in manifest["histories"].values())
    manifest["data_quality_status"] = ("CLEAN" if total_rejected == 0
                                       else "SOURCE_ANOMALIES_FILTERED")
    manifest["rejected_history_rows_total"] = total_rejected
    for name, cfg in HISTORIES.items():
        payload = schwab_client.quote(cfg["symbol"])
        summary = quote_summary(payload, cfg["symbol"], end_ms)
        path = snapshot / "raw" / f"{name}_quote.json"
        write_bytes(path, canonical_bytes(payload))
        manifest["quotes"][name] = {
            **summary, "scale_divisor": cfg["scale_divisor"],
            "raw_path": str(path.relative_to(ROOT)), "raw_sha256": sha256_file(path),
        }
        print(f"{cfg['symbol']} quote: {summary['resolved_symbol']} age {summary['quote_age_seconds']:.0f}s")
    for name, symbol in CHAINS.items():
        chain = schwab_client.option_chain(symbol, contract_type="ALL", strike_count=80)
        summary = chain_summary(chain, symbol)
        path = snapshot / "raw" / f"{name}_option_chain.json"
        write_bytes(path, canonical_bytes(chain))
        manifest["option_chains"][name] = {
            "symbol": symbol, **summary, "raw_path": str(path.relative_to(ROOT)),
            "raw_sha256": sha256_file(path),
        }
        print(f"{symbol} chain: {summary['contract_counts']['calls']} calls, {summary['contract_counts']['puts']} puts")
    manifest_path = snapshot / "manifest.json"
    write_bytes(manifest_path, canonical_bytes(manifest))
    write_bytes(DATA_ROOT / "latest.json", canonical_bytes({
        "snapshot": stamp, "manifest": str(manifest_path.relative_to(ROOT)),
    }))
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
