#!/usr/bin/env python3
"""
schwab_client.py — Schwab Trader API client for the SCHD call panel.

Why: Schwab returns REAL option greeks and two-sided quotes for SCHD, replacing the
Black-Scholes approximation in tlt_scanner.py. Stdlib only — no new dependencies.

What this does NOT give you: historical option marks. Schwab serves the CURRENT
chain only, so the SCHD backtest stays ETF-path timing. Nothing here measures
realised call P&L.

Credentials (never committed, never pasted into chat):
  export SCHWAB_APP_KEY=...      # "App Key" from developer.schwab.com
  export SCHWAB_APP_SECRET=...   # "Secret"
  …or write ~/.config/tlt-scanner/schwab.json  {"app_key": "...", "app_secret": "...",
                                                "redirect_uri": "https://127.0.0.1:8182"}
The redirect URI must match your app registration EXACTLY (Schwab requires https).

Usage:
  python schwab_client.py doctor    # diagnose: creds -> network -> tokens -> data
  python schwab_client.py login     # one-time browser auth; run again weekly
  python schwab_client.py status    # token age / expiry
  python schwab_client.py quote SCHD
  python schwab_client.py chain SCHD --min-dte 150 --target-delta 0.70
  python schwab_client.py logout    # delete stored tokens
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
API_BASE = "https://api.schwabapi.com"
DEFAULT_REDIRECT = "https://127.0.0.1:8182"

CONFIG_DIR = Path.home() / ".config" / "tlt-scanner"
CRED_FILE = CONFIG_DIR / "schwab.json"
TOKEN_FILE = CONFIG_DIR / "schwab_tokens.json"

ACCESS_TTL = 30 * 60          # Schwab access tokens last ~30 minutes
REFRESH_TTL = 7 * 24 * 3600   # refresh tokens last ~7 days, then re-login


class SchwabError(RuntimeError):
    pass


# ----------------------------------------------------------------- credentials / tokens


def _write_private(path: Path, payload: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)


def load_credentials() -> dict:
    key, secret = os.environ.get("SCHWAB_APP_KEY"), os.environ.get("SCHWAB_APP_SECRET")
    redirect = os.environ.get("SCHWAB_REDIRECT_URI", DEFAULT_REDIRECT)
    if key and secret:
        return {"app_key": key, "app_secret": secret, "redirect_uri": redirect}
    if CRED_FILE.exists():
        c = json.loads(CRED_FILE.read_text())
        if c.get("app_key") and c.get("app_secret"):
            c.setdefault("redirect_uri", DEFAULT_REDIRECT)
            return c
    raise SchwabError(
        "no Schwab credentials found — set SCHWAB_APP_KEY and SCHWAB_APP_SECRET, "
        f"or create {CRED_FILE} with app_key/app_secret/redirect_uri (chmod 600)"
    )


def load_tokens() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def _post_token(cred: dict, form: dict) -> dict:
    basic = base64.b64encode(f"{cred['app_key']}:{cred['app_secret']}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise SchwabError(f"token request failed ({e.code}): {detail}") from None
    except Exception as e:
        raise SchwabError(f"token request failed: {type(e).__name__}: {e}") from None
    if "access_token" not in tok:
        raise SchwabError(f"token response missing access_token: {list(tok)}")
    now = time.time()
    tok["obtained_at"] = now
    tok["access_expires_at"] = now + min(int(tok.get("expires_in", ACCESS_TTL)), ACCESS_TTL)
    prev = load_tokens() or {}
    # a refresh response keeps the original refresh-token clock
    tok["refresh_expires_at"] = (prev.get("refresh_expires_at")
                                 if form.get("grant_type") == "refresh_token" and prev.get("refresh_expires_at")
                                 else now + REFRESH_TTL)
    _write_private(TOKEN_FILE, tok)
    return tok


def get_access_token() -> str:
    """Valid access token, refreshing silently when it has aged out."""
    tok = load_tokens()
    if not tok:
        raise SchwabError("not logged in — run: python schwab_client.py login")
    if time.time() < tok.get("access_expires_at", 0) - 60:
        return tok["access_token"]
    if time.time() > tok.get("refresh_expires_at", 0):
        raise SchwabError("refresh token expired (7-day limit) — run: python schwab_client.py login")
    if not tok.get("refresh_token"):
        raise SchwabError("no refresh token stored — run: python schwab_client.py login")
    cred = load_credentials()
    return _post_token(cred, {"grant_type": "refresh_token",
                              "refresh_token": tok["refresh_token"]})["access_token"]


def login() -> None:
    """One-time browser auth. No local HTTPS server: you paste the redirected URL back."""
    cred = load_credentials()
    params = {"client_id": cred["app_key"], "redirect_uri": cred["redirect_uri"],
              "response_type": "code"}
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"\nusing app key {cred['app_key'][:4]}…{cred['app_key'][-4:]}  "
          f"redirect_uri {cred['redirect_uri']}")
    print("(the redirect_uri above must byte-match your app registration exactly)\n")
    print("1. Open this URL, log in, and approve the app:\n")
    print(f"   {url}\n")
    print("2. Your browser will land on a page that FAILS TO LOAD. That is expected —")
    print(f"   nothing is listening on {cred['redirect_uri']}.")
    print("3. Copy the FULL address-bar URL of that failed page and paste it below.")
    print("   ⚠ THE CODE EXPIRES IN ~30 SECONDS. Have this terminal ready and paste fast;")
    print("     if it fails, just run login again — a stale code is the usual cause.\n")
    pasted = input("redirected URL (or just the code): ").strip().strip('"\'')
    if not pasted:
        raise SchwabError("nothing pasted")
    if "code=" in pasted:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        code = (qs.get("code") or [None])[0]
    else:
        code = pasted  # allow pasting just the code value
    if not code:
        raise SchwabError("no ?code= found — paste the whole address-bar URL, or just the code value")
    try:
        tok = _post_token(cred, {"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": cred["redirect_uri"]})
    except SchwabError as e:
        msg = str(e)
        hint = ""
        if "400" in msg:
            hint = ("\n  likely causes, in order:"
                    "\n   1. the code expired (>30s between approving and pasting) — just re-run login"
                    "\n   2. redirect_uri mismatch — it must byte-match the callback on developer.schwab.com"
                    f" (you sent: {cred['redirect_uri']})"
                    "\n   3. the code was already used — each one works once")
        elif "401" in msg:
            hint = ("\n  401 = the App Key/Secret pair was rejected, or the app is still"
                    " 'Approved - Pending' on developer.schwab.com")
        raise SchwabError(msg + hint) from None
    print(f"\n✓ logged in. Tokens saved to {TOKEN_FILE} (chmod 600).")
    print(f"  access token valid ~30 min (auto-refreshed); refresh token expires "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(tok['refresh_expires_at']))} "
          f"— re-run 'login' after that.")


def doctor() -> None:
    """Check every prerequisite in order and say exactly which step is broken."""
    ok, fail = "  \u2713", "  \u2717"
    print("\nSchwab connection doctor\n" + "=" * 46)
    print(f"{ok} python {sys.version.split()[0]}")

    try:
        cred = load_credentials()
        src = "environment" if os.environ.get("SCHWAB_APP_KEY") else str(CRED_FILE)
        k = cred["app_key"]
        print(f"{ok} credentials found via {src}")
        print(f"      app key    : {k[:4]}…{k[-4:]}  ({len(k)} chars)")
        print(f"      secret     : {'set' if cred.get('app_secret') else 'MISSING'} "
              f"({len(cred.get('app_secret') or '')} chars)")
        print(f"      redirect   : {cred['redirect_uri']}")
        if cred["redirect_uri"] != DEFAULT_REDIRECT:
            print(f"      note: differs from the default {DEFAULT_REDIRECT} — it must match your app")
    except SchwabError as e:
        print(f"{fail} credentials: {e}")
        print("      fix: export SCHWAB_APP_KEY=... and SCHWAB_APP_SECRET=... in THIS shell")
        print("      (env vars do not survive a new tab — re-export or use the config file)")
        return

    import socket
    try:
        socket.create_connection(("api.schwabapi.com", 443), timeout=10).close()
        print(f"{ok} network: api.schwabapi.com:443 reachable")
    except Exception as e:
        print(f"{fail} network: cannot reach api.schwabapi.com ({type(e).__name__})")
        print("      fix: check VPN/firewall — the OAuth and data calls both need this host")
        return

    tok = load_tokens()
    if not tok:
        print(f"{fail} tokens: none stored at {TOKEN_FILE}")
        print("      fix: run  python schwab_client.py login")
        return
    now = time.time()
    a, r = tok.get("access_expires_at", 0), tok.get("refresh_expires_at", 0)
    print(f"{ok if now < r else fail} tokens: access {(a - now) / 60:+.0f} min, "
          f"refresh {(r - now) / 86400:+.1f} days  ({TOKEN_FILE})")
    if now >= r:
        print("      fix: refresh token expired (7-day limit) — run login again")
        return

    try:
        get_access_token()
        print(f"{ok} token refresh works")
    except SchwabError as e:
        print(f"{fail} token refresh: {e}")
        return

    try:
        q = quote("SCHD")
        last = (q.get("SCHD", {}).get("quote", {}) or {}).get("lastPrice")
        print(f"{ok} market data: SCHD quote returned (last {last})")
    except SchwabError as e:
        print(f"{fail} market data: {e}")
        print("      401/403 here usually means the app is not yet approved for Market Data")
        return

    try:
        c = pick_call("SCHD")
        print(f"{ok} option chain: {c['expiry']} {c['strike']} call, delta {c['delta']}, OI {c['open_interest']}")
        print("\nall good — run:  python tlt_scanner.py --options")
    except SchwabError as e:
        print(f"{fail} option chain: {e}")


def status() -> None:
    tok = load_tokens()
    if not tok:
        print("not logged in — run: python schwab_client.py login")
        return
    now = time.time()
    a, r = tok.get("access_expires_at", 0), tok.get("refresh_expires_at", 0)
    print(f"token file : {TOKEN_FILE}")
    print(f"access     : {'valid' if now < a else 'expired'} "
          f"({(a - now) / 60:+.0f} min) — auto-refreshes on use")
    print(f"refresh    : {'valid' if now < r else 'EXPIRED — run login'} "
          f"({(r - now) / 86400:+.1f} days)")


def logout() -> None:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print(f"deleted {TOKEN_FILE}")
    else:
        print("no stored tokens")


# ----------------------------------------------------------------- API calls


def api_get(path: str, params: dict | None = None) -> dict:
    token = get_access_token()
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SchwabError(f"GET {path} failed ({e.code}): {e.read().decode()[:300]}") from None
    except Exception as e:
        raise SchwabError(f"GET {path} failed: {type(e).__name__}: {e}") from None


def quote(symbol: str) -> dict:
    return api_get("/marketdata/v1/quotes", {"symbols": symbol})


def option_chain(symbol: str, contract_type: str = "CALL", strike_count: int = 40) -> dict:
    return api_get("/marketdata/v1/chains", {"symbol": symbol, "contractType": contract_type,
                                             "strikeCount": strike_count, "includeUnderlyingQuote": "true"})


def _iter_contracts(chain: dict):
    """Flatten Schwab's callExpDateMap -> {expiry: {strike: [contract, ...]}}."""
    for exp_key, strikes in (chain.get("callExpDateMap") or {}).items():
        # keys look like "2027-01-15:141" (expiry:daysToExpiration)
        exp_date = exp_key.split(":")[0]
        for _, contracts in (strikes or {}).items():
            for c in contracts or []:
                yield exp_date, c


def pick_call(symbol: str = "SCHD", spot: float | None = None,
              min_dte: int = 150, target_delta: float = 0.70) -> dict:
    """Choose an expiry past min_dte and the strike closest to target_delta, using
    Schwab's own greeks and quotes. Same dict shape the scanner's panel expects."""
    chain = option_chain(symbol)
    if spot is None:
        u = chain.get("underlying") or {}
        spot = float(u.get("last") or u.get("mark") or 0) or None
    rows = []
    for exp_date, c in _iter_contracts(chain):
        dte = int(c.get("daysToExpiration") or 0)
        bid, ask = float(c.get("bid") or 0), float(c.get("ask") or 0)
        mid = float(c.get("mark") or 0) or (bid + ask) / 2
        delta = c.get("delta")
        if dte < min_dte or mid <= 0 or delta in (None, "NaN"):
            continue
        try:
            delta = float(delta)
        except (TypeError, ValueError):
            continue
        rows.append({"expiry": exp_date, "dte": dte, "strike": float(c.get("strikePrice") or 0),
                     "bid": bid, "ask": ask, "mid": mid, "delta": delta,
                     "theta_day": float(c.get("theta") or 0),
                     "iv": float(c.get("volatility") or 0) / 100.0,
                     "oi": int(c.get("openInterest") or 0),
                     "volume": int(c.get("totalVolume") or 0)})
    if not rows:
        raise SchwabError(f"no {symbol} calls with quotes and greeks at >= {min_dte} DTE")
    nearest_dte = min(r["dte"] for r in rows)
    same_exp = [r for r in rows if r["dte"] == nearest_dte]
    pick = min(same_exp, key=lambda r: abs(r["delta"] - target_delta))
    atm = min(same_exp, key=lambda r: abs(r["strike"] - (spot or r["strike"])))
    spread_pct = (pick["ask"] - pick["bid"]) / pick["mid"] * 100 if pick["mid"] else None
    return {
        "source": "schwab",
        "expiry": pick["expiry"], "dte": pick["dte"], "spot": round(spot, 2) if spot else None,
        "atm_iv_pct": round(atm["iv"] * 100, 1),
        "strike": pick["strike"], "mid": round(pick["mid"], 2),
        "bid": round(pick["bid"], 2), "ask": round(pick["ask"], 2),
        "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
        "delta": round(pick["delta"], 2),
        "theta_day": round(pick["theta_day"], 4),
        "theta_pct_of_premium_per_day": round(abs(pick["theta_day"]) / pick["mid"] * 100, 2) if pick["mid"] else None,
        "open_interest": pick["oi"], "volume": pick["volume"],
        "breakeven": round(pick["strike"] + pick["mid"], 2),
        "breakeven_move_pct": round((pick["strike"] + pick["mid"] - spot) / spot * 100, 2) if spot else None,
        "premium_pct_of_notional": round(pick["mid"] / spot * 100, 1) if spot else None,
    }


# ----------------------------------------------------------------- CLI


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    try:
        if cmd == "login":
            login()
        elif cmd == "status":
            status()
        elif cmd == "doctor":
            doctor()
        elif cmd == "logout":
            logout()
        elif cmd == "quote":
            print(json.dumps(quote(args[1] if len(args) > 1 else "SCHD"), indent=2))
        elif cmd == "chain":
            sym = args[1] if len(args) > 1 and not args[1].startswith("-") else "SCHD"
            dte = int(args[args.index("--min-dte") + 1]) if "--min-dte" in args else 150
            dl = float(args[args.index("--target-delta") + 1]) if "--target-delta" in args else 0.70
            print(json.dumps(pick_call(sym, min_dte=dte, target_delta=dl), indent=2))
        else:
            print(__doc__)
            return 1
    except SchwabError as e:
        print(f"schwab: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
