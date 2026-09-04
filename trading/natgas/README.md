# natgas_fix

Settlement-basis, session and formatting fixes for the desk's `natgas_tracker.py`.
Standard library only, Python 3.9+. Copy `natgas_fix.py` next to the tracker.

## What was wrong on the 2026-09-02 18:55 ET run

| Printed | Actual | Why |
|---|---|---|
| prior settle 2.904, +0.105 (+3.62%) "the day's change" | 09-02 settle 2.956, day +0.053 (+1.79%) | Schwab `closePrice` still held the 09-01 settle. Sprague Energy: Oct settled 2.904 on 09-01, 2.956 on 09-02 |
| UNG capture 0.60x (expected ~1.0x) | 1.21x of NG | ETF close-to-close was divided by a two-session futures move |
| BOIL capture 0.54x (expected ~2.0x) | 2.20x of NG = 1.10 of its 2x target | value was leverage-normalised, label was not |
| NY session AFTERHOURS | Globex OPEN, trade date 09-03 | futures prints after 18:00 ET belong to the next trade date |
| header 09-02 UTC, probe 09-03 | one clock | probe was stamped in local time |
| 18 weekdays left | 17 trading days | Labor Day |
| 42th pctile, "at the BOTTOM" | 42nd, "NEW LOW below its range" | 0.124 < range low 0.129 |

`python3 natgas_fix.py --demo` prints the corrected HEADLINE / VEHICLES block for that run.

## Wiring into natgas_tracker.py

```python
from natgas_fix import (contract_from_symbol, resolve_settle, change, capture, render_headline,
                        render_vehicle, session_header, stamp, stamp_date, days_to_expiry,
                        ng_expiry_for, ordinal, range_position, rank_pctile, settle_field_report)

now = datetime.now(timezone.utc)

# 1. header: one clock everywhere (ET first, UTC in brackets), both sessions named
print(f"NATGAS TRACKER · {session_header(now)}")
print(f"BCOMNG probed {stamp_date(probe_time)} ...")            # was a local-time date

# 2. the settlement the headline change is measured against
settle = resolve_settle(
    ngv26_payload,                       # the Schwab quote dict you already fetch for /NGV26
    now,
    contract=contract_from_symbol("/NGV26"),
    index_quotes={"$DJCING": djcing_quote, "$SPGSNG": spgsng_quote},   # the stand-ins you already fetch
    prev_close_seen=state.get("closePrice_prev_session"),             # see "persist" below
)
for line in render_headline("/NGV26", last, settle, oi, now):
    print("     " + line)

# 3. vehicles: same window on both sides of the ratio
_, ng_settle_pct = change(settle.price, settle.schwab_close) if settle.stale_sessions == 0 else (None, float("nan"))
print(render_vehicle("UNG", ung_last, ung_chg, ung_vol, ng_settle_pct, leverage=1.0))
print(render_vehicle("BOIL", boil_last, boil_chg, boil_vol, ng_settle_pct, leverage=2.0))

# 4. curve / spreads: label the basis instead of calling it "the day's change"
hdr = "chg% vs today's settle" if settle.stale_sessions == 0 else f"chg% vs settle-{settle.stale_sessions} ({settle.basis})"
print(range_position(spread_now, lo, hi, rank_pctile(spread_now, history), len(history)))

# 5. expiry
print(days_to_expiry(now_et.date(), ng_expiry_for("/NGV26")).render())
```

`resolve_settle` returns a `Settle` with `price`, `session`, `source`, `stale_sessions`,
`basis` ("day" or "2-session") and `notes`. Print the notes: they say which evidence
decided the basis. Its priority order:

1. `known={session_date: settle}` if you keep your own settle log.
2. Schwab's field when `settleTime` stamps it as the same session and `closePrice`
   moved since the previous session's run.
3. The same-day settle derived from a Schwab single-commodity index stand-in
   ($DJCING, $SPGSNG) when it holds the same contract (business days 1-4 of the month
   for the M+1 contract, 10+ for M+2; the 5th-9th are the roll and are refused).
   Still Schwab-only. Two coincidence traps are refused: an index whose % change
   equals the live futures move, and two stand-ins that disagree by more than 2 ticks.
4. Schwab's stale value, honestly labelled `2-session`.

Without `settleTime` or a persisted `closePrice`, staleness is ASSUMED: one session behind
on the same evening (the measured 09-02 behaviour), current from the next session on.
The notes say so.

### Persist closePrice per run

Write `settle.schwab_close` and `settle.session` to the tracker's state file each run and
pass the previous session's value back as `prev_close_seen`. When the value has not moved
across a settlement, Schwab has not rolled, whatever `settleTime` says.

### Diagnostic log

Append `settle_field_report(payload, now)` to a log once per run for a few sessions. It
prints `closePrice`, `futureSettlementPrice`, `settleTime`, the base Schwab uses for its own
`netChange`, and the quote stamps side by side, so you can see which field rolls to the new
settlement and at what time. Once you know, the `known` or `settleTime` path takes over and
the index derivation is only a cross-check.

## Tests

```
python3 -m unittest test_natgas_fix -v
```
