#!/usr/bin/env python3
"""
webapp.py — local web dashboard for the TLT scanner.

Same engines as tlt_scanner.py, rendered as a page instead of a terminal panel.
Stdlib only (no Flask): http.server + the scanner's own analyze().

  python webapp.py                 # http://127.0.0.1:8787
  python webapp.py --lan           # also reachable from your phone on the LAN
  python webapp.py --port 9000 --refresh-min 10
  python webapp.py --entry 83.10 --account 50000

Endpoints:  /  dashboard   |   /api  JSON   |   /health

Read-only. It shows what the scanner computes; it places no orders.
"""
from __future__ import annotations

import argparse
import html
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tlt_scanner as ts

STATE: dict = {"res": None, "at": 0.0, "err": None}
LOCK = threading.Lock()
OPTS: dict = {}

# palette (dataviz reference instance) — status roles never carry meaning alone
CSS = """
:root{color-scheme:light;
--surface:#fcfcfb;--card:#ffffff;--line:#e6e5e1;
--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#84837d;
--good:#0ca30c;--warn:#fab219;--serious:#ec835a;--crit:#d03b3b;--accent:#2a78d6;}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--surface:#141413;--card:#1a1a19;--line:#2e2e2b;
--ink:#ffffff;--ink-2:#c3c2b7;--ink-3:#8d8c84;--accent:#3987e5;}}
*{box-sizing:border-box}
body{margin:0;padding:18px;background:var(--surface);color:var(--ink);
font:14px/1.5 ui-sans-serif,-apple-system,SFMono-Regular,Menlo,monospace}
h1{font-size:15px;margin:0;letter-spacing:.02em}
h2{font-size:12px;margin:0 0 10px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.09em;font-weight:600}
a{color:var(--accent)}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.muted{color:var(--ink-3);font-size:12px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card.wide{grid-column:1/-1}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;min-width:430px;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:right;font-weight:600;color:var(--ink-3);font-size:11px;
text-transform:uppercase;letter-spacing:.06em;padding:0 0 6px}
th:first-child,td:first-child{text-align:left}
td{text-align:right;padding:5px 0;border-top:1px solid var(--line)}
.sym{font-weight:700}
.hero{font-size:30px;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}
.sub{color:var(--ink-2);font-size:12px;margin-top:3px}
ul.checks{list-style:none;margin:0;padding:0}
ul.checks li{display:flex;gap:6px;padding:4px 0;border-top:1px solid var(--line);align-items:baseline}
ul.checks li:first-child{border-top:0}
.mark{flex:none;width:2.5em;white-space:nowrap;font-weight:700;font-family:ui-monospace,Menlo,monospace}
.on{color:var(--good)}.off{color:var(--ink-3)}.hot{color:var(--crit)}.wa{color:var(--warn)}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700;
letter-spacing:.05em;border:1px solid currentColor}
.lv{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-top:1px solid var(--line)}
.lv:first-child{border-top:0}
.lv b{font-variant-numeric:tabular-nums}
.flips li{padding:3px 0;color:var(--ink-2)}
.note{color:var(--ink-3);font-size:11.5px;margin-top:10px;line-height:1.45}
svg.spark{display:block;width:100%;height:44px;margin-top:6px;overflow:visible}
.err{border-color:var(--crit);color:var(--crit)}
@media(max-width:560px){body{padding:11px}.grid{grid-template-columns:1fr}.hero{font-size:26px}}
"""


def esc(x) -> str:
    return html.escape(str(x))


def compute(force: bool = False) -> dict:
    """Run the scanner, cached. Both legs come from the same analyze() call."""
    ttl = OPTS.get("refresh_min", 15) * 60
    with LOCK:
        if not force and STATE["res"] and (time.time() - STATE["at"]) < ttl:
            return STATE["res"]
    try:
        frames = ts.demo_frames() if OPTS.get("demo") else ts.load_frames(refresh=force)
        if "TLT" not in frames:
            raise RuntimeError("no TLT data (network blocked?)")
        dur = (15.0, False) if OPTS.get("demo") else ts.fetch_duration()
        res = ts.analyze(frames, account=OPTS.get("account"), risk_pct=OPTS.get("risk", 1.0),
                         entry=OPTS.get("entry"), duration=dur)
        res["_spark"] = {k: [round(float(v), 4) for v in frames[k]["Close"].tail(60)]
                         for k in ("TLT",) if k in frames}
        with LOCK:
            STATE.update(res=res, at=time.time(), err=None)
        return res
    except Exception as e:
        with LOCK:
            STATE["err"] = f"{type(e).__name__}: {e}"
        raise


def spark(vals: list[float], color: str) -> str:
    """Single-series sparkline: one 2px line, no legend (the card title names it)."""
    if not vals or len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = " ".join(f"{i / (n - 1) * 100:.2f},{34 - (v - lo) / rng * 30:.2f}" for i, v in enumerate(vals))
    return (f'<svg class="spark" viewBox="0 0 100 38" preserveAspectRatio="none" role="img" '
            f'aria-label="last {n} closes, low {lo:.2f} high {hi:.2f}">'
            f'<title>last {n} sessions — low {lo:.2f}, high {hi:.2f}</title>'
            f'<polyline fill="none" stroke="{color}" stroke-width="2" vector-effect="non-scaling-stroke" '
            f'stroke-linejoin="round" stroke-linecap="round" points="{pts}"/></svg>')


def checks(items, key="on", label="name") -> str:
    out = []
    for i in items:
        st = i.get(key)
        mark, cls = ("[x]", "on") if st else ("[?]", "off") if st is None else ("[ ]", "off")
        out.append(f'<li><span class="mark {cls}">{mark}</span><span>{esc(i[label])}</span></li>')
    return f'<ul class="checks">{"".join(out)}</ul>'


def render(res: dict) -> str:
    reg, st, ex, plan = res["regime"], res["stack"], res["exit"], res["plan"]
    reg_cls = {"BULLISH": "on", "BEARISH": "hot"}.get(reg["label"], "wa")
    ex_cls = ("hot" if ex["verdict"].startswith("EXIT") else
              "wa" if ex["verdict"].startswith(("TRIM", "CAUTION")) else "on")

    rows = []
    for name, t in res["tape"].items():
        f = ts.to_32nds if name == "UB" else (lambda x: f"{x:.2f}")
        chg_cls = "on" if t["chg1"] >= 0 else "hot"
        rows.append(
            f'<tr><td class="sym">{esc(name)}</td><td>{f(t["close"])}</td>'
            f'<td class="{chg_cls}">{t["chg1"]:+.2f}%</td><td>{t["rsi14"]:.0f}</td>'
            f'<td>{f(t["sma50"]) if t["sma50"] else "—"}</td>'
            f'<td>{f(t["sma200"]) if t["sma200"] else "—"}</td>'
            f'<td>{"▲" if t["macd_up"] else "▼"}</td><td>{t["roc20"]:+.1f}%</td></tr>')

    lv = plan["levels"]
    lvl_rows = "".join(
        f'<div class="lv"><span>{esc(k)}</span><b>{esc(v)}</b></div>' for k, v in [
            ("entry", f'{lv["entry"]:.2f}'), ("stop", f'{lv["stop"]:.2f}'),
            ("risk / share", f'{lv["risk_per_share"]:.2f}'),
            ("targets", " / ".join(f"{t:.2f}" for t in lv["targets"])),
            ("invalidation", f'{ex["invalidation"]:.2f}' if ex.get("invalidation") else "—"),
        ] + ([("size", f'{lv["shares_for_risk"]} sh @ ${lv["risk_amount"]:.0f} risk')]
             if "shares_for_risk" in lv else []))

    src = " · DEMO DATA" if OPTS.get("demo") else ""
    age = int((time.time() - STATE["at"]) / 60)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{OPTS.get('refresh_min', 15) * 60}">
<title>TLT scanner</title><style>{CSS}</style></head><body>
<div class="top"><h1>TLT SCANNER</h1>
<span class="muted">as of {esc(res['as_of'])}{src} · rendered {age}m ago ·
<a href="/?refresh=1">refresh</a> · <a href="/api">json</a></span></div>
<div class="grid">

<section class="card wide"><h2>Tape</h2><div class="tw"><table>
<thead><tr><th>sym</th><th>last</th><th>chg</th><th>rsi</th><th>50d</th><th>200d</th><th>macd</th><th>20d</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
{spark(res.get("_spark", {}).get("TLT", []), "var(--accent)")}
<p class="note">TLT sparkline: last 60 closes.</p></section>

<section class="card"><h2>TLT regime</h2>
<div class="hero {reg_cls}">{esc(reg['label'])}</div>
<div class="sub">score {reg['score']:+.0f} / ±100 · stack {st['lit']}/{st['of']} · tier {esc(st['tier'])}</div>
{checks(st['items'])}</section>

<section class="card"><h2>TLT plan</h2>
<div class="hero">{esc(plan['action'].split(' — ')[0])}</div>
<div class="sub">{esc(plan['size'])} · {esc(plan['management'])}</div>
{lvl_rows}</section>

<section class="card"><h2>TLT exit engine</h2>
<div><span class="pill {ex_cls}">{esc(ex['verdict'])}</span></div>
{checks(ex['conditions'])}
<p class="note">A close under <b>{esc(f"{ex['invalidation']:.2f}") if ex.get('invalidation') else '—'}</b> flips this to EXIT.</p>
</section>

<section class="card wide"><h2>What flips it</h2>
<ul class="checks flips">{"".join(f"<li>{esc(f)}</li>" for f in plan['what_flips_it'])}</ul>
<p class="note">Cross-checks: {" · ".join(esc(m) for m in res['macro'])}</p>
<p class="note">Decision support, not financial advice. Signals form on daily closes; act the next
session. The TLT engine lost to buy-and-hold on its own backtest window — read the levels, not the
verdicts, as instructions.</p></section>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "tlt-scanner"

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        force = "refresh=1" in query
        try:
            if path == "/health":
                return self._send(200, b"ok", "text/plain; charset=utf-8")
            res = compute(force=force)
            if path == "/api":
                payload = {k: v for k, v in res.items() if not k.startswith("_")}
                return self._send(200, json.dumps(payload, indent=2, default=str).encode(),
                                  "application/json; charset=utf-8")
            if path == "/":
                return self._send(200, render(res).encode(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:
            page = (f'<!doctype html><meta charset="utf-8"><style>{CSS}</style>'
                    f'<body><section class="card err"><h2>scanner error</h2>'
                    f'<p>{esc(type(e).__name__)}: {esc(e)}</p>'
                    f'<p class="note">Try <a href="/?refresh=1">refresh</a>, or run '
                    f'<code>python tlt_scanner.py</code> in a terminal to see the full error.</p>'
                    f'</section></body>')
            self._send(500, page.encode(), "text/html; charset=utf-8")

    def log_message(self, fmt, *a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="local web dashboard for the TLT scanner")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--lan", action="store_true", help="bind 0.0.0.0 so your phone can reach it")
    ap.add_argument("--refresh-min", type=int, default=15, help="cache/auto-reload minutes (default 15)")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--account", type=float)
    ap.add_argument("--risk", type=float, default=1.0)
    ap.add_argument("--entry", type=float)
    a = ap.parse_args()
    OPTS.update(vars(a))

    host = "0.0.0.0" if a.lan else "127.0.0.1"
    srv = ThreadingHTTPServer((host, a.port), Handler)
    shown = f"http://127.0.0.1:{a.port}"
    print(f"\n  dashboard: {shown}")
    if a.lan:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            print(f"  on your LAN: http://{s.getsockname()[0]}:{a.port}")
            s.close()
        except Exception:
            pass
    print(f"  auto-reload every {a.refresh_min} min · Ctrl-C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
