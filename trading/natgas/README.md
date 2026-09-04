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

`python3 natgas_fix.py --demo` prints the corrected HEADLINE / VEHICLES blocks for the
09-02 run and the 09-04 run.

## Measured Schwab behaviour (09-02 18:55 ET and 09-04 07:59 ET runs)

- **The index stand-ins are live quotes with a same-day base.** At 07:59 ET on 09-04,
  $DJCING printed +0.34%, exactly /NGV26 2.923 against its 09-03 settle 2.913. At 18:55 ET
  on 09-02 both stand-ins printed +1.79%, exactly 3.009 against the 09-02 settle 2.956.
  So `futures_last / (index_last / index_close)` is the same-day settle, to about a tick.
- **The futures closePrice rolls the next morning.** The 09-04 run carried `settleTime`
  09-04 07:53 ET with the 09-03 settle. On the evening of 09-02 the field was stamped
  17:38 ET but still held the 09-01 value, so `settleTime` is a posted-at stamp, not the
  settlement date. The module maps it to a session by the calendar and never trusts it alone.
- **Dating a settle by matching its value against history is a dead end.** The 09-04 run
  called 2.913 "ambiguous" because 08-27 also settled there. The calendar says the last
  settled session at 07:59 ET on 09-04 is 09-03, and the index-implied value confirms it.

## How `resolve_settle` decides

It gathers three pieces of evidence and returns a `Settle` with `price`, `session`,
`source`, `stale_sessions`, `basis` ("day" or "2-session") and `notes` that say which
evidence decided. Print the notes.

1. `known={session_date: settle}` if you keep your own settle log. Highest priority.
2. The index-implied same-day settle from $DJCING / $SPGSNG (still Schwab-only). It is
   used only while the index holds the same contract: business days 1-4 of the month for
   the M+1 contract, 10+ for M+2; the 5th-9th are the roll and are refused. Two stand-ins
   that disagree by more than 2 ticks are not used.
   - agrees with Schwab's field and nothing says the field is stale: Schwab **confirmed**.
   - agrees, but it is the settle evening and the field equals closePrice: no confirmation,
     because an index whose base has not rolled lands exactly on the stale close.
   - disagrees: Schwab is one session behind, the index-implied value is used, **unless** a
     persisted closePrice proves the field rolled, in which case Schwab is kept and the note
     tells you to check the index roll window.
3. Otherwise `settleTime`'s session and a persisted `closePrice` decide the staleness, and
   with no evidence at all it is ASSUMED: one session behind on the settle evening, current
   from the next session on. The notes say so.

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
    index_quotes={"$DJCING": djcing_quote, "$SPGSNG": spgsng_quote},   # pass the whole quote dicts
    prev_close_seen=state.get("closePrice_prev_session"),             # see "persist" below
)
for line in render_headline("/NGV26", last, settle, oi, now):
    print("     " + line)

# 3. vehicles: both legs over the same two timestamps. Best: the futures 16:00 -> 16:00
#    print you already keep for the last completed session. Else settle-to-settle.
print(render_vehicle("UNG", ung_last, ung_chg, ung_vol, ng_1600_pct, 1.0, ng_window="16:00->16:00"))
print(render_vehicle("BOIL", boil_last, boil_chg, boil_vol, ng_1600_pct, 2.0, ng_window="16:00->16:00"))

# 4. curve / spreads: label the basis instead of calling it "the day's change"
hdr = "chg% vs today's settle" if settle.stale_sessions == 0 else f"chg% vs settle-{settle.stale_sessions} ({settle.basis})"
print(range_position(spread_now, lo, hi, rank_pctile(spread_now, history), len(history)))

# 5. expiry
print(days_to_expiry(now_et.date(), ng_expiry_for("/NGV26")).render())
```

### Persist closePrice per run

Write `settle.schwab_close` and `settle.session` to the tracker's state file each run and
pass the previous session's value back as `prev_close_seen`. Unchanged across a settlement
means Schwab has not rolled; changed means it has, and that outranks a disagreeing index.

### Diagnostic log

Append `settle_field_report(payload, now)` to a log once per run. It prints `closePrice`,
`futureSettlementPrice`, `settleTime`, the base Schwab uses for its own `netChange`, and the
quote stamps side by side. Two runs already showed the field rolls between 17:38 ET and
07:53 ET the next morning; a few more will pin the hour.

## Tests

```
python3 -m unittest test_natgas_fix -v
```

## restart-scanner.sh

Finds and restarts the desk's `commodity-scanner.js`. No placeholders to fill in:
angle brackets are redirection operators in zsh, so a command containing `<label>`
or `<that directory>` fails with "no such file or directory" before it runs.

```
./restart-scanner.sh              # stop it if running, start it, verify it came up
./restart-scanner.sh --status     # report only, change nothing
./restart-scanner.sh --stop       # stop and leave stopped
```

It prefers launchd when a LaunchAgent in `~/Library/LaunchAgents` references the
script, because a hand-started process dies with its terminal. Otherwise it starts
the scanner with `nohup` from the script's own directory and logs to
`~/Library/Logs/commodity-scanner.log`.

A process counts as the scanner only when its command line names the file **and**
its executable is node. Matching the command line alone also catches this script,
an editor with the file open, and a `tail` on its log.

Override with environment variables: `SCANNER_JS`, `SCANNER_ARGS`, `SCANNER_LOG_DIR`.

### Refreshing the regime data

`gas_alerts.json` is a separate input. The tracker fails closed when it is older
than 36 hours. Regenerate it from the pipeline directory:

```
cd "$HOME/Energy&Commodity Desk/pipeline"
python3 gas_alerts.py
```
