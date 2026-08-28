#!/usr/bin/env python3
"""
schwab_client.py — Schwab Trader API client for scanner histories and TLT options.

Schwab supplies every live market-data input: TLT, /UB, $TYX, $TNX,
plus real TLT option greeks and two-sided quotes. Stdlib only.

What this does NOT give you: historical option marks. Schwab serves the CURRENT
chain only, so any option backtest stays ETF-path timing. Nothing here measures
realised call P&L.

Credentials (never committed, never pasted into chat):
  export SCHWAB_APP_KEY=...      # "App Key" from developer.schwab.com
  export SCHWAB_APP_SECRET=...   # "Secret"
  …or write ~/.config/tlt-scanner/schwab.json  {"app_key": "...", "app_secret": "...",
                                                "redirect_uri": "https://127.0.0.1:8182"}
The redirect URI must match your app registration EXACTLY (Schwab requires https).

Usage:
  python schwab_client.py doctor    # diagnose: creds -> network -> tokens -> data
  python schwab_client.py login     # one-time browser auth (auto-captures the redirect)
  python schwab_client.py login --manual   # fallback: paste the redirected URL yourself
  python schwab_client.py login --code '<redirected URL or bare code>'   # non-interactive
  python schwab_client.py status    # token age / expiry
  python schwab_client.py quote TLT
  python schwab_client.py chain TLT --side BOTH
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
import shutil
import ssl
import subprocess
import threading
import webbrowser
from datetime import datetime, time as clock_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
API_BASE = "https://api.schwabapi.com"
DEFAULT_REDIRECT = "https://127.0.0.1:8182"

CONFIG_DIR = Path.home() / ".config" / "tlt-scanner"
CRED_FILE = CONFIG_DIR / "schwab.json"
TOKEN_FILE = CONFIG_DIR / "schwab_tokens.json"
CERT_FILE = CONFIG_DIR / "localhost-cert.pem"
KEY_FILE = CONFIG_DIR / "localhost-key.pem"

# Reuse the already-authorized Energy Desk Schwab app when the scanner has no
# private config of its own.  Both clients then share one rotating token file;
# secrets are read locally and are never copied into this repository.
SHARED_ENV_FILE = Path(os.environ.get(
    "TLT_SCANNER_SCHWAB_ENV",
    "/Users/maciejsmoczynski/bbbot-rotation/sector_rotation.env",
))
SHARED_TOKEN_FILE = Path(os.environ.get(
    "TLT_SCANNER_SCHWAB_TOKEN",
    "/Users/maciejsmoczynski/bbbot-rotation/sector_rotation_data/schwab_token.json",
))

ACCESS_TTL = 30 * 60          # Schwab access tokens last ~30 minutes
REFRESH_TTL = 7 * 24 * 3600   # refresh tokens last ~7 days, then re-login

CALL_MIN_DTE = 50
CALL_MAX_DTE = 75
CALL_MIN_DELTA = 0.65
CALL_MAX_DELTA = 0.80
CALL_TARGET_DTE = 63
CALL_TARGET_DELTA = 0.70
CALL_MAX_SPREAD_PCT = 10.0
CALL_MIN_OPEN_INTEREST = 100
CALL_MIN_VOLUME = 5
CALL_MAX_BREAKEVEN_MOVE_PCT = 8.0
CALL_MAX_LIVE_QUOTE_AGE_SEC = 20 * 60


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
    if SHARED_ENV_FILE.exists():
        env = {}
        for line in SHARED_ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip("\"'")
        if env.get("SCHWAB_APP_KEY") and env.get("SCHWAB_APP_SECRET"):
            return {
                "app_key": env["SCHWAB_APP_KEY"],
                "app_secret": env["SCHWAB_APP_SECRET"],
                "redirect_uri": env.get("SCHWAB_CALLBACK_URL", DEFAULT_REDIRECT),
                "_source": str(SHARED_ENV_FILE),
            }
    raise SchwabError(
        "no Schwab credentials found — set SCHWAB_APP_KEY and SCHWAB_APP_SECRET, "
        f"or create {CRED_FILE} with app_key/app_secret/redirect_uri (chmod 600)"
    )


def load_tokens() -> dict | None:
    if TOKEN_FILE.exists():
        try:
            tok = json.loads(TOKEN_FILE.read_text())
            tok["_storage"] = "scanner"
            tok["_token_file"] = str(TOKEN_FILE)
            return tok
        except Exception:
            return None
    if SHARED_TOKEN_FILE.exists():
        try:
            payload = json.loads(SHARED_TOKEN_FILE.read_text())
            raw = dict(payload["token"])
            created = float(payload["creation_timestamp"])
            expires = float(raw.get("expires_at") or 0)
            raw["obtained_at"] = expires - float(raw.get("expires_in") or ACCESS_TTL)
            raw["access_expires_at"] = expires
            raw["refresh_expires_at"] = created + REFRESH_TTL
            raw["_storage"] = "schwab-py"
            raw["_token_file"] = str(SHARED_TOKEN_FILE)
            raw["_creation_timestamp"] = created
            raw["_raw_token"] = dict(payload["token"])
            return raw
        except Exception:
            return None
    return None


def _store_refreshed_token(tok: dict, previous: dict) -> None:
    if previous.get("_storage") == "schwab-py":
        raw = dict(previous.get("_raw_token") or {})
        raw.update({k: v for k, v in tok.items() if not k.startswith("_")
                    and k not in ("obtained_at", "access_expires_at", "refresh_expires_at")})
        raw["expires_at"] = tok["access_expires_at"]
        payload = {"creation_timestamp": previous["_creation_timestamp"], "token": raw}
        path = Path(previous["_token_file"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.chmod(0o600)
        tmp.replace(path)
    else:
        clean = {k: v for k, v in tok.items() if not k.startswith("_")}
        _write_private(TOKEN_FILE, clean)


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
    _store_refreshed_token(tok, prev)
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


def _ensure_cert() -> bool:
    """Self-signed cert for the loopback callback. Uses the openssl already on macOS."""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return True
    if not shutil.which("openssl"):
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    base = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE), "-subj", "/CN=127.0.0.1"]
    # macOS ships LibreSSL, where -addext may not exist; the SAN is cosmetic here
    # because the browser warning is click-through either way.
    for cmd in (base + ["-addext", "subjectAltName=IP:127.0.0.1"], base):
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            KEY_FILE.chmod(0o600)
            CERT_FILE.chmod(0o600)
            return True
        except Exception as e:
            last = e
    print(f"   (self-signed cert generation failed: {type(last).__name__})")
    return False


def _capture_code(redirect_uri: str, timeout: int = 300) -> str | None:
    """Serve HTTPS on the callback host/port just long enough to catch ?code=."""
    parsed = urllib.parse.urlparse(redirect_uri)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 443
    box: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            box["code"] = (qs.get("code") or [None])[0]
            box["error"] = (qs.get("error") or [None])[0]
            body = (b"<h2>Schwab login captured.</h2><p>You can close this tab and "
                    b"return to the terminal.</p>" if box.get("code")
                    else b"<h2>No authorization code in the callback.</h2>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass  # keep the terminal clean

    try:
        srv = HTTPServer((host, port), Handler)
    except OSError as e:
        raise SchwabError(f"cannot listen on {host}:{port} ({e.strerror}) — "
                          "close whatever is using it, or use --manual") from None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.timeout = 1  # short poll so the deadline below is what actually governs
    deadline = time.time() + timeout

    def serve():
        # keep serving: the browser's cert-warning click aborts one handshake, and
        # some browsers also fetch /favicon.ico, either of which would otherwise
        # consume the single request we care about
        while time.time() < deadline and not box.get("code") and not box.get("error"):
            try:
                srv.handle_request()
            except Exception:
                continue

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    t.join(timeout)
    srv.server_close()
    if box.get("error"):
        raise SchwabError(f"Schwab returned error={box['error']}")
    return box.get("code")


def login(manual: bool = False, supplied: str | None = None) -> None:
    """Browser auth. Default: a loopback HTTPS listener captures the redirect
    automatically (no copy-paste, no 30-second race). --manual falls back to pasting."""
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
    code = None
    if supplied:  # non-interactive: user pasted the URL (or bare code) as an argument
        supplied = supplied.strip().strip('"\'')
        if "code=" in supplied:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(supplied).query)
            code = (qs.get("code") or [None])[0]
        else:
            code = supplied
        if not code:
            raise SchwabError("could not find a code in what you passed to --code")
    if code is None and not manual and _ensure_cert():
        print("   Starting a local HTTPS listener to capture the redirect automatically.")
        print("   Your browser will warn about a self-signed certificate for 127.0.0.1 —")
        print("   click Advanced → Proceed. That page is served by this script, not the internet.\n")
        try:
            webbrowser.open(url)
            print("   (browser opened; waiting up to 5 minutes for the callback…)\n")
        except Exception:
            pass
        code = _capture_code(cred["redirect_uri"])
        if not code:
            print("   no callback captured — falling back to manual paste.\n")
    elif not manual:
        print("   openssl not found, so the automatic listener is unavailable.\n")

    if not code:
        print("   If the browser shows \"this site can't be reached\" / \"website is not working\",")
        print("   that is EXPECTED — copy the whole address bar from that page and paste it here.")
        print("   (Or press Ctrl-C and re-run:  python schwab_client.py login --code '<paste>')\n")
        pasted = input("redirected URL (or just the code): ").strip().strip('"\'')
        if not pasted:
            raise SchwabError("nothing pasted — run login on its own line, not chained with other commands")
        if "code=" in pasted:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
            code = (qs.get("code") or [None])[0]
        else:
            code = pasted
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

    try:  # a real request: raw sockets can succeed through an intercepting proxy
        urllib.request.urlopen(AUTH_URL, timeout=15)
        print(f"{ok} network: api.schwabapi.com responds")
    except urllib.error.HTTPError:
        print(f"{ok} network: api.schwabapi.com responds")
    except Exception as e:
        print(f"{fail} network: cannot reach api.schwabapi.com — {type(e).__name__}: {str(e)[:80]}")
        print("      fix: check VPN/proxy/firewall — OAuth and data calls both need this host")
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
        q = quote("TLT")
        last = (q.get("TLT", {}).get("quote", {}) or {}).get("lastPrice")
        print(f"{ok} market data: TLT quote returned (last {last})")
    except SchwabError as e:
        print(f"{fail} market data: {e}")
        print("      401/403 here usually means the app is not yet approved for Market Data")
        return

    try:
        c = pick_call("TLT")
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


def price_history(symbol: str, start_ms: int, end_ms: int) -> dict:
    """Schwab daily OHLCV only; callers must validate and scale instrument units."""
    return api_get("/marketdata/v1/pricehistory", {
        "symbol": symbol,
        "periodType": "year",
        "startDate": int(start_ms),
        "endDate": int(end_ms),
        "frequencyType": "daily",
        "frequency": 1,
        "needExtendedHoursData": "false",
        "needPreviousClose": "false",
    })


def option_chain(symbol: str, contract_type: str = "CALL", strike_count: int = 40) -> dict:
    return api_get("/marketdata/v1/chains", {"symbol": symbol, "contractType": contract_type,
                                             "strikeCount": strike_count, "includeUnderlyingQuote": "true"})


def _iter_contracts(chain: dict, side: str = "CALL"):
    """Flatten one side of Schwab's expiry/strike contract map."""
    key = "callExpDateMap" if side.upper() == "CALL" else "putExpDateMap"
    for exp_key, strikes in (chain.get(key) or {}).items():
        # keys look like "2027-01-15:141" (expiry:daysToExpiration)
        exp_date = exp_key.split(":")[0]
        for _, contracts in (strikes or {}).items():
            for c in contracts or []:
                yield exp_date, c


def _expected_quote_session(now: datetime) -> tuple[datetime.date, bool]:
    """Expected latest US session date and whether that session is currently open."""
    et = now.astimezone(ZoneInfo("America/New_York"))
    live = et.weekday() < 5 and clock_time(9, 30) <= et.time() <= clock_time(16, 15)
    if et.weekday() < 5 and et.time() >= clock_time(9, 30):
        session = et.date()
    else:
        session = et.date() - timedelta(days=1)
        while session.weekday() >= 5:
            session -= timedelta(days=1)
    return session, live


def _quote_fresh(quote_ms: int | float | None, now: datetime) -> tuple[bool, float | None]:
    try:
        q = datetime.fromtimestamp(float(quote_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return False, None
    expected, live = _expected_quote_session(now)
    age = max(0.0, (now.astimezone(timezone.utc) - q).total_seconds())
    same_session = q.astimezone(ZoneInfo("America/New_York")).date() == expected
    return bool(same_session and (not live or age <= CALL_MAX_LIVE_QUOTE_AGE_SEC)), age


def _chain_health(chain: dict, side: str = "CALL") -> dict:
    side = side.upper()
    checks = {
        "status_success": str(chain.get("status") or "").upper() == "SUCCESS",
        "not_delayed": chain.get("isDelayed") is False,
        "not_truncated": chain.get("isChainTruncated") is False,
        "has_requested_side": bool(chain.get("callExpDateMap" if side == "CALL" else "putExpDateMap")),
        "underlying_positive": float(chain.get("underlyingPrice") or 0) > 0,
    }
    return {"ok": all(checks.values()), "checks": checks}


def _rank_score(row: dict, target_dte: int, target_delta: float,
                max_spread_pct: float, min_oi: int, min_volume: int,
                max_breakeven_move_pct: float,
                dte_score_span: float = 13.0) -> tuple[float, dict]:
    """Transparent 100-point execution-quality score; higher is better."""
    components = {
        "spread": 30 * max(0.0, 1 - row["spread_pct"] / max_spread_pct),
        "liquidity": (15 * min(1.0, row["oi"] / max(min_oi * 10, 1))
                      + 10 * min(1.0, row["volume"] / max(min_volume * 10, 1))),
        "theta": 15 * max(0.0, 1 - row["theta_pct_day"] / 0.20),
        "delta_fit": 15 * max(0.0, 1 - abs(row["delta"] - target_delta) / 0.15),
        "dte_fit": 10 * max(0.0, 1 - abs(row["dte"] - target_dte)
                            / max(float(dte_score_span), 1.0)),
        "breakeven": 5 * min(1.0, max(0.0, 1 - row["breakeven_move_pct"] / max_breakeven_move_pct)),
    }
    return sum(components.values()), {k: round(v, 2) for k, v in components.items()}


def pick_option(symbol: str, side: str, spot: float | None = None,
                min_dte: int = CALL_MIN_DTE, max_dte: int = CALL_MAX_DTE,
                target_dte: int = CALL_TARGET_DTE,
                min_delta: float = CALL_MIN_DELTA, max_delta: float = CALL_MAX_DELTA,
                target_delta: float = CALL_TARGET_DELTA,
                max_spread_pct: float = CALL_MAX_SPREAD_PCT,
                min_open_interest: int = CALL_MIN_OPEN_INTEREST,
                min_volume: int = CALL_MIN_VOLUME,
                max_breakeven_move_pct: float = CALL_MAX_BREAKEVEN_MOVE_PCT,
                now: datetime | None = None, chain_data: dict | None = None) -> dict:
    """Always rank the best executable contract; preferences never exclude it.

    Only corrupted/untradeable inputs are excluded: unhealthy chains, stale
    quotes, invalid two-sided markets, bad strikes/expiries, and non-standard
    deliverables.  DTE, delta, spread, liquidity and breakeven are ranking
    preferences and warnings, not eligibility gates.
    """
    side = side.upper()
    if side not in ("CALL", "PUT"):
        raise ValueError("side must be CALL or PUT")
    chain = chain_data if chain_data is not None else option_chain(symbol, contract_type=side, strike_count=80)
    now = now or datetime.now(timezone.utc)
    health = _chain_health(chain, side)
    if spot is None:
        spot = float(chain.get("underlyingPrice") or 0) or None
    preferences = {
        "days_to_expiry": [min_dte, max_dte], "delta_abs": [min_delta, max_delta],
        "target_days": target_dte, "target_delta_abs": target_delta,
        "preferred_max_spread_pct": max_spread_pct,
        "preferred_min_open_interest": min_open_interest,
        "preferred_min_volume": min_volume,
        "preferred_max_breakeven_move_pct": max_breakeven_move_pct,
    }
    base = {"source": "schwab", "symbol": symbol, "side": side,
            "ranking_policy": "preferences_not_gates", "preferences": preferences,
            "thresholds": preferences, "chain_health": health}
    if not health["ok"] or not spot or spot <= 0:
        return {**base, "contract_selected": False, "contract_qualified": False,
                "status": "CHAIN_REJECTED", "candidates_scanned": 0,
                "ranked_candidates": 0, "rejection_counts": {"chain_health": 1}}

    rows, rejection_counts = [], {}
    scanned = 0

    def reject(reason: str) -> None:
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    for exp_date, c in _iter_contracts(chain, side):
        scanned += 1
        try:
            dte = int(c.get("daysToExpiration") or 0)
            strike = float(c.get("strikePrice") or 0)
            bid, ask = float(c.get("bid") or 0), float(c.get("ask") or 0)
            oi, volume = int(c.get("openInterest") or 0), int(c.get("totalVolume") or 0)
        except (TypeError, ValueError, OverflowError):
            reject("malformed_contract")
            continue
        fresh, age = _quote_fresh(c.get("quoteTimeInLong"), now)
        if dte <= 0 or strike <= 0:
            reject("invalid_strike_or_expiry")
            continue
        if bid <= 0 or ask < bid:
            reject("invalid_bid_ask")
            continue
        if not fresh:
            reject("stale_quote")
            continue
        if bool(c.get("nonStandard")) or bool(c.get("mini")):
            reject("nonstandard_contract")
            continue
        raw_delta = c.get("delta")
        try:
            signed_delta = float(raw_delta)
            delta_abs = abs(signed_delta)
        except (TypeError, ValueError, OverflowError):
            signed_delta, delta_abs = None, -1.0
        try:
            theta = float(c.get("theta") or 0)
        except (TypeError, ValueError, OverflowError):
            theta = 0.0
        try:
            iv = float(c.get("volatility") or 0) / 100.0
        except (TypeError, ValueError, OverflowError):
            iv = 0.0
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        breakeven = strike + ask if side == "CALL" else strike - ask
        be_move = ((breakeven - spot) / spot * 100 if side == "CALL"
                   else (spot - breakeven) / spot * 100)
        warnings_ = []
        if not min_dte <= dte <= max_dte: warnings_.append("outside_preferred_days")
        if delta_abs < 0 or not min_delta <= delta_abs <= max_delta: warnings_.append("outside_preferred_delta")
        if spread_pct > max_spread_pct: warnings_.append("wide_spread")
        if oi < min_open_interest: warnings_.append("low_open_interest")
        if volume < min_volume: warnings_.append("low_volume")
        if be_move > max_breakeven_move_pct: warnings_.append("large_breakeven_move")
        row = {"contract_symbol": "".join(str(c.get("symbol") or "").split()),
               "expiry": exp_date, "dte": dte, "strike": strike,
               "bid": bid, "ask": ask, "mid": mid, "delta": delta_abs,
               "signed_delta": signed_delta, "theta_day": theta,
               "theta_pct_day": abs(theta) / ask * 100, "iv": iv,
               "oi": oi, "volume": volume, "spread_pct": spread_pct,
               "breakeven": breakeven, "breakeven_move_pct": be_move,
               "quote_age_seconds": age, "warnings": warnings_}
        dte_score_span = max(target_dte - min_dte, max_dte - target_dte, 1)
        score, components = _rank_score(row, target_dte, target_delta, max_spread_pct,
                                        min_open_interest, min_volume, max_breakeven_move_pct,
                                        dte_score_span=dte_score_span)
        row["rank_score"], row["score_components"] = score, components
        rows.append(row)

    base.update({"candidates_scanned": scanned, "ranked_candidates": len(rows),
                 "qualified_candidates": len(rows), "rejection_counts": rejection_counts})
    if not rows:
        return {**base, "contract_selected": False, "contract_qualified": False,
                "status": "NO_EXECUTABLE_CONTRACT"}

    pick = max(rows, key=lambda r: (r["rank_score"], r["oi"], r["volume"], -r["spread_pct"]))
    same_exp = [r for r in rows if r["expiry"] == pick["expiry"]]
    atm = min(same_exp, key=lambda r: abs(r["strike"] - spot))
    return {
        **base, "contract_selected": True, "contract_qualified": True,
        "status": "TOP_RANKED_CONTRACT", "contract_symbol": pick["contract_symbol"],
        "expiry": pick["expiry"], "dte": pick["dte"], "spot": round(spot, 2),
        "atm_iv_pct": round(atm["iv"] * 100, 1), "strike": pick["strike"],
        "mid": round(pick["mid"], 2), "bid": round(pick["bid"], 2),
        "ask": round(pick["ask"], 2), "spread_pct": round(pick["spread_pct"], 2),
        "delta": round(pick["signed_delta"], 3) if pick["signed_delta"] is not None else None,
        "delta_abs": round(pick["delta"], 3) if pick["delta"] >= 0 else None,
        "theta_day": round(pick["theta_day"], 4),
        "theta_pct_of_premium_per_day": round(pick["theta_pct_day"], 3),
        "open_interest": pick["oi"], "volume": pick["volume"],
        "breakeven": round(pick["breakeven"], 2), "breakeven_basis": "ask",
        "breakeven_move_pct": round(pick["breakeven_move_pct"], 2),
        "premium_pct_of_notional": round(pick["ask"] / spot * 100, 1),
        "quote_age_seconds": round(pick["quote_age_seconds"], 0),
        "rank_score": round(pick["rank_score"], 2),
        "score_components": pick["score_components"], "warnings": pick["warnings"],
    }


def pick_call(symbol: str = "TLT", **kwargs) -> dict:
    return pick_option(symbol, "CALL", **kwargs)


def pick_put(symbol: str = "TLT", **kwargs) -> dict:
    return pick_option(symbol, "PUT", **kwargs)


def best_options(symbol: str, spot: float | None = None,
                 now: datetime | None = None, chain_data: dict | None = None,
                 **preferences) -> dict:
    """Return both top-ranked sides from one consistent Schwab chain snapshot."""
    chain = chain_data if chain_data is not None else option_chain(symbol, contract_type="ALL", strike_count=80)
    if spot is None:
        spot = float(chain.get("underlyingPrice") or 0) or None
    return {"source": "schwab", "symbol": symbol,
            "call": pick_option(symbol, "CALL", spot=spot, now=now, chain_data=chain, **preferences),
            "put": pick_option(symbol, "PUT", spot=spot, now=now, chain_data=chain, **preferences)}


# ----------------------------------------------------------------- CLI


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    try:
        if cmd == "login":
            supplied = args[args.index("--code") + 1] if "--code" in args else None
            login(manual="--manual" in args or bool(supplied), supplied=supplied)
        elif cmd == "status":
            status()
        elif cmd == "doctor":
            doctor()
        elif cmd == "logout":
            logout()
        elif cmd == "quote":
            print(json.dumps(quote(args[1] if len(args) > 1 else "TLT"), indent=2))
        elif cmd == "chain":
            sym = args[1] if len(args) > 1 and not args[1].startswith("-") else "TLT"
            min_dte = int(args[args.index("--min-dte") + 1]) if "--min-dte" in args else CALL_MIN_DTE
            max_dte = int(args[args.index("--max-dte") + 1]) if "--max-dte" in args else CALL_MAX_DTE
            target_dte = int(args[args.index("--target-dte") + 1]) if "--target-dte" in args else CALL_TARGET_DTE
            dl = float(args[args.index("--target-delta") + 1]) if "--target-delta" in args else 0.70
            side = args[args.index("--side") + 1].upper() if "--side" in args else "BOTH"
            prefs = {"min_dte": min_dte, "max_dte": max_dte,
                     "target_dte": target_dte, "target_delta": dl}
            result = (best_options(sym, **prefs) if side == "BOTH"
                      else pick_option(sym, side, **prefs))
            print(json.dumps(result, indent=2))
        else:
            print(__doc__)
            return 1
    except SchwabError as e:
        print(f"schwab: {e}", file=sys.stderr)
        if "--verbose" in args:
            import traceback
            traceback.print_exc()
        return 1
    except Exception as e:
        print(f"schwab: unexpected {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
