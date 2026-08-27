#!/usr/bin/env node
/**
 * Ag-commodity call-entry scanner — WEAT · CORN · SOYB · CANE · DBA
 *
 * Watches live prices during the NY regular session (09:30–16:00) and fires
 * sound + iTerm2 alerts when a ticker hits a planned call-entry condition:
 *
 *   buy-dip       price pulls back into the buy zone [stop … entry]
 *   buy-breakout  price takes out an overhead trigger (CANE 11.50, DBA 28.80)
 *   stop-warn     price loses the stop reference (manage open calls)
 *   close-warn    late-session failure check (CORN < 19.20 after 15:45 NY)
 *
 * Zero dependencies, Node >= 18. Levels live in config.json; "ema20",
 * "sma50", "sma200" resolve live from daily data each poll so trailing
 * references track the chart without manual edits.
 *
 *   node commodity-scanner.js               live dashboard (run inside iTerm2)
 *   node commodity-scanner.js --once        one poll, plain table, exit
 *   node commodity-scanner.js --simulate    scripted demo of every alert type
 *   node commodity-scanner.js --test-sound  play each alert sound and exit
 *   node commodity-scanner.js --check       offline self-test, exit 0/1
 *   node commodity-scanner.js --quiet       no sounds (visual only)
 *   node commodity-scanner.js --interval N  poll every N seconds (default 15)
 *   node commodity-scanner.js --config P    alternate config path
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { setTimeout: delay } = require('node:timers/promises');

const IS_MAC = process.platform === 'darwin';
const IS_TTY = !!process.stdout.isTTY;
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36';
const ALERT_LOG = path.join(__dirname, 'alerts.log');
const DYN_LEVELS = new Set(['ema20', 'sma50', 'sma200']);

// ── CLI ─────────────────────────────────────────────────────────────────────

const argv = process.argv.slice(2);
function hasFlag(name) {
  return argv.includes(name);
}
function flagValue(name, dflt) {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : dflt;
}

// ── Colors / terminal ───────────────────────────────────────────────────────

const C = {
  reset: '\x1b[0m',
  bold: '\x1b[1m',
  dim: '\x1b[2m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  cyan: '\x1b[36m',
  gray: '\x1b[90m',
  bgGreen: '\x1b[42;30m',
  bgRed: '\x1b[41;97m',
  bgCyan: '\x1b[46;30m',
  bgYellow: '\x1b[43;30m',
};

function cell(text, width, color) {
  const t = String(text ?? '').slice(0, width).padEnd(width);
  return color ? color + t + C.reset : t;
}

function osc(seq) {
  if (IS_TTY) process.stdout.write(seq);
}
const itermNotify = (msg) =>
  osc(`\x1b]9;${String(msg).replace(/[\x00-\x1f]/g, ' ')}\x07`);
const itermAttention = () => osc('\x1b]1337;RequestAttention=yes\x07');
const setTitle = (t) => osc(`\x1b]0;${t}\x07`);
const setBadge = (t) =>
  osc(`\x1b]1337;SetBadgeFormat=${Buffer.from(t).toString('base64')}\x07`);

// ── Formatting ──────────────────────────────────────────────────────────────

function fmt(x) {
  return Number.isFinite(x) ? x.toFixed(2) : '—';
}
function fmtPct(x, signed = true) {
  if (!Number.isFinite(x)) return '—';
  const s = x >= 0 && signed ? '+' : '';
  return `${s}${x.toFixed(2)}%`;
}

// ── NY time / session ───────────────────────────────────────────────────────

const NY_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  weekday: 'short',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

function nyParts(date = new Date()) {
  const p = {};
  for (const part of NY_FMT.formatToParts(date)) p[part.type] = part.value;
  const hour = Number(p.hour) % 24;
  return {
    weekday: p.weekday,
    dateKey: `${p.year}-${p.month}-${p.day}`,
    minutes: hour * 60 + Number(p.minute),
    clock: `${String(hour).padStart(2, '0')}:${p.minute}:${p.second}`,
  };
}

function hmToMin(hm) {
  const [h, m] = String(hm).split(':').map(Number);
  return h * 60 + m;
}

// tradingPeriod (from Yahoo meta.currentTradingPeriod.regular) handles
// holidays and half-days; the local clock is the fallback.
function sessionState(cfg, now = new Date(), tradingPeriod = null) {
  const p = nyParts(now);
  const weekend = p.weekday === 'Sat' || p.weekday === 'Sun';
  let open;
  if (
    tradingPeriod &&
    Number.isFinite(tradingPeriod.start) &&
    Number.isFinite(tradingPeriod.end)
  ) {
    const t = now.getTime() / 1000;
    open = t >= tradingPeriod.start && t < tradingPeriod.end;
  } else {
    open =
      !weekend &&
      p.minutes >= hmToMin(cfg.session.open) &&
      p.minutes < hmToMin(cfg.session.close);
  }
  const late = open && p.minutes >= hmToMin(cfg.session.lateWarn);
  return { open, late, weekend, ...p };
}

// ── Indicators ──────────────────────────────────────────────────────────────

function emaLast(closes, period) {
  if (!Array.isArray(closes) || closes.length < period) return null;
  const k = 2 / (period + 1);
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < closes.length; i++) ema = closes[i] * k + ema * (1 - k);
  return ema;
}

function smaLast(closes, period) {
  if (!Array.isArray(closes) || closes.length < period) return null;
  return closes.slice(-period).reduce((a, b) => a + b, 0) / period;
}

// Fold the live price in as today's forming close, like an intraday chart does.
function liveEma(closes, price, period) {
  const base = emaLast(closes, period);
  if (base == null || !Number.isFinite(price)) return base;
  const k = 2 / (period + 1);
  return price * k + base * (1 - k);
}

function liveSma(closes, price, period) {
  if (!Number.isFinite(price)) return smaLast(closes, period);
  if (!Array.isArray(closes) || closes.length < period - 1) return null;
  const win = closes.slice(-(period - 1)).concat(price);
  return win.reduce((a, b) => a + b, 0) / period;
}

// ── Config ──────────────────────────────────────────────────────────────────

function loadConfig(configPath) {
  const raw = fs.readFileSync(configPath, 'utf8');
  const cfg = JSON.parse(raw);
  const errors = validateConfig(cfg);
  if (errors.length) {
    throw new Error(`config invalid:\n  - ${errors.join('\n  - ')}`);
  }
  return cfg;
}

function levelSpecOk(spec) {
  return spec == null || Number.isFinite(spec) || DYN_LEVELS.has(spec);
}

function validateConfig(cfg) {
  const errors = [];
  if (!cfg || typeof cfg !== 'object') return ['config is not an object'];
  if (!cfg.tickers || !Object.keys(cfg.tickers).length) errors.push('no tickers');
  if (!(cfg.pollSeconds >= 5)) errors.push('pollSeconds must be >= 5');
  if (!cfg.session?.open || !cfg.session?.close) errors.push('session.open/close required');
  for (const [sym, t] of Object.entries(cfg.tickers || {})) {
    for (const key of ['dipEntry', 'stop', 'breakout']) {
      if (!levelSpecOk(t[key])) errors.push(`${sym}.${key}: bad level spec ${t[key]}`);
    }
    if (t.closeWarnBelow != null && !Number.isFinite(t.closeWarnBelow)) {
      errors.push(`${sym}.closeWarnBelow must be a number`);
    }
    if (
      Number.isFinite(t.dipEntry) &&
      Number.isFinite(t.stop) &&
      t.stop > t.dipEntry
    ) {
      errors.push(`${sym}: stop above dipEntry`);
    }
  }
  return errors;
}

// ── Level resolution ────────────────────────────────────────────────────────

function resolveLevel(spec, ind, fallback) {
  if (spec == null) return { value: null, label: '' };
  if (Number.isFinite(spec)) return { value: spec, label: 'fixed' };
  if (DYN_LEVELS.has(spec)) {
    const live = ind?.[spec];
    if (Number.isFinite(live)) return { value: live, label: spec, dynamic: true };
    const fb = fallback?.[spec];
    if (Number.isFinite(fb)) {
      return { value: fb, label: `${spec}~`, dynamic: true, approx: true };
    }
    return { value: null, label: spec };
  }
  return { value: null, label: '' };
}

function resolveLevels(tcfg, ind) {
  const dipEntry = resolveLevel(tcfg.dipEntry, ind, tcfg.fallbackLevels);
  const stop = resolveLevel(tcfg.stop, ind, tcfg.fallbackLevels);
  const breakout = resolveLevel(tcfg.breakout, ind, tcfg.fallbackLevels);
  let inverted = false;
  // A rising trailing stop can climb above a fixed entry; the zone then
  // follows the stop (an EMA-touch buy) until the config is refreshed.
  if (
    dipEntry.value != null &&
    stop.value != null &&
    stop.value > dipEntry.value
  ) {
    dipEntry.value = stop.value;
    dipEntry.label = `${stop.label}^`;
    inverted = true;
  }
  return { dipEntry, stop, breakout, inverted };
}

// ── Per-ticker alert state machine ──────────────────────────────────────────

class TickerMonitor {
  constructor(symbol, tcfg) {
    this.symbol = symbol;
    this.tcfg = tcfg;
    this.prev = null;
    this.active = { dip: false, breakout: false, stop: false };
    this.lastFired = new Map();
    this.closeWarnDay = null;
  }

  evaluate(sample, levels, session, opts) {
    const events = [];
    const { price, nowMs } = sample;
    if (!Number.isFinite(price)) return events;
    const h = opts.hystPct / 100;
    const E = levels.dipEntry.value;
    const S = levels.stop.value;
    const B = levels.breakout.value;
    const prev = this.prev;

    const fire = (type, msg, voice) => {
      const last = this.lastFired.get(type) ?? -Infinity;
      const suppressed = nowMs - last < opts.cooldownMs;
      if (!suppressed) this.lastFired.set(type, nowMs);
      events.push({
        type,
        symbol: this.symbol,
        price,
        msg,
        voice,
        suppressed,
        startup: prev == null,
      });
    };

    if (Number.isFinite(B)) {
      if (!this.active.breakout && price >= B) {
        this.active.breakout = true;
        const extra = this.tcfg.breakoutNote ? ` ${this.tcfg.breakoutNote}` : '';
        fire(
          'buy-breakout',
          `${this.symbol} ${fmt(price)} >= ${fmt(B)} — breakout trigger. Call-entry signal.${extra}`,
          `${this.symbol} breakout. Buy signal.`,
        );
      } else if (this.active.breakout && price < B * (1 - h)) {
        this.active.breakout = false;
      }
    }

    if (Number.isFinite(S)) {
      if (!this.active.stop && price < S) {
        this.active.stop = true;
        fire(
          'stop-warn',
          `${this.symbol} ${fmt(price)} < ${fmt(S)} (${levels.stop.label}) — stop reference broken; reassess open calls.`,
          `Warning. ${this.symbol} below stop.`,
        );
      } else if (this.active.stop && price >= S * (1 + h)) {
        this.active.stop = false;
      }
    }

    if (Number.isFinite(E) && Number.isFinite(S)) {
      const inZone = price <= E && price >= S;
      if (!this.active.dip && inZone && !this.active.stop) {
        this.active.dip = true;
        const reclaim = prev != null && prev < S;
        fire(
          'buy-dip',
          `${this.symbol} ${fmt(price)} — ${
            reclaim ? 'reclaimed buy zone from below' : 'pullback into buy zone'
          } (entry ${fmt(E)}, stop ${fmt(S)} ${levels.stop.label}). Call-entry signal.`,
          `${this.symbol} buy zone.`,
        );
      } else if (this.active.dip && (price > E * (1 + h) || price < S)) {
        this.active.dip = false;
      }
    }

    const cw = this.tcfg.closeWarnBelow;
    if (
      Number.isFinite(cw) &&
      session.open &&
      session.late &&
      price < cw &&
      this.closeWarnDay !== session.dateKey
    ) {
      this.closeWarnDay = session.dateKey;
      fire(
        'close-warn',
        `${this.symbol} ${fmt(price)} < ${fmt(cw)} after ${opts.lateWarn} NY — failed-breakout risk into the close.`,
        `${this.symbol} failed breakout risk.`,
      );
    }

    this.prev = price;
    return events;
  }
}

function stateLabel(mon, price, levels, nearPct) {
  const E = levels.dipEntry.value;
  const S = levels.stop.value;
  const B = levels.breakout.value;
  if (!Number.isFinite(price)) return { text: 'NO DATA', color: C.gray };
  if (Number.isFinite(S) && price < S) return { text: '!! BELOW STOP', color: C.bgRed };
  if (mon.active.breakout) return { text: '>> BREAKOUT <<', color: C.bgGreen };
  if (Number.isFinite(E) && Number.isFinite(S) && price <= E && price >= S) {
    return { text: '** BUY ZONE **', color: C.bgCyan };
  }
  if (Number.isFinite(B) && price < B && ((B - price) / price) * 100 <= nearPct) {
    return { text: '~ NEAR TRIGGER', color: C.bgYellow };
  }
  if (Number.isFinite(E) && price > E && ((price - E) / E) * 100 <= nearPct) {
    return { text: '~ NEAR ZONE', color: C.bgYellow };
  }
  return { text: 'WATCH', color: C.dim };
}

// ── Sounds ──────────────────────────────────────────────────────────────────

function bell(times = 1) {
  if (!IS_TTY) return;
  let n = Math.max(1, times);
  const ring = () => {
    process.stdout.write('\x07');
    if (--n > 0) setTimeout(ring, 350);
  };
  ring();
}

function playSound(cfg, key, quiet) {
  if (quiet || !cfg.sounds?.enabled) return;
  const spec = cfg.sounds.map?.[key];
  const repeat = Math.max(1, spec?.repeat ?? 1);
  if (!IS_MAC || !spec?.file || !fs.existsSync(spec.file)) return bell(repeat);
  let n = repeat;
  const playOnce = () => {
    const p = spawn('afplay', [spec.file], { stdio: 'ignore' });
    p.on('error', () => bell(1));
    p.on('close', () => {
      if (--n > 0) playOnce();
    });
  };
  playOnce();
  bell(1); // BEL as well, so iTerm2 shows its bell indicator on the tab
}

function sayVoice(cfg, text, quiet) {
  if (quiet || !cfg.sounds?.enabled || !cfg.sounds?.voice) return;
  if (!IS_MAC || !text) return;
  const p = spawn('say', ['-r', '195', text], { stdio: 'ignore' });
  p.on('error', () => {});
}

// ── Data feeds ──────────────────────────────────────────────────────────────

async function fetchChart(cfg, symbol, params) {
  const qs = new URLSearchParams(params).toString();
  let lastErr;
  for (const host of cfg.quoteHosts) {
    try {
      const res = await fetch(
        `https://${host}/v8/finance/chart/${encodeURIComponent(symbol)}?${qs}`,
        {
          headers: { 'User-Agent': UA, Accept: 'application/json' },
          signal: AbortSignal.timeout(8000),
        },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const result = body?.chart?.result?.[0];
      if (!result) {
        throw new Error(body?.chart?.error?.description || 'empty chart result');
      }
      return result;
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr;
}

function parseIntraday(result) {
  const meta = result.meta || {};
  const q = result.indicators?.quote?.[0] || {};
  const finite = (arr) => (arr || []).filter(Number.isFinite);
  const highs = finite(q.high);
  const lows = finite(q.low);
  const closes = finite(q.close);
  return {
    price: Number.isFinite(meta.regularMarketPrice)
      ? meta.regularMarketPrice
      : closes.at(-1) ?? null,
    prevClose: Number.isFinite(meta.chartPreviousClose)
      ? meta.chartPreviousClose
      : Number.isFinite(meta.previousClose)
        ? meta.previousClose
        : null,
    dayHigh: Number.isFinite(meta.regularMarketDayHigh)
      ? meta.regularMarketDayHigh
      : highs.length
        ? Math.max(...highs)
        : null,
    dayLow: Number.isFinite(meta.regularMarketDayLow)
      ? meta.regularMarketDayLow
      : lows.length
        ? Math.min(...lows)
        : null,
    marketTime: Number.isFinite(meta.regularMarketTime) ? meta.regularMarketTime : null,
    tradingPeriod: meta.currentTradingPeriod?.regular ?? null,
  };
}

function parseDailyCloses(result, todayKey) {
  const ts = result.timestamp || [];
  const closes = result.indicators?.quote?.[0]?.close || [];
  const out = [];
  for (let i = 0; i < ts.length; i++) {
    const c = closes[i];
    if (!Number.isFinite(c)) continue;
    if (nyParts(new Date(ts[i] * 1000)).dateKey === todayKey) continue; // forming candle
    out.push(c);
  }
  return out;
}

class YahooFeed {
  constructor(cfg) {
    this.cfg = cfg;
    this.dailies = new Map();
    this.dailiesDay = null;
    this.errors = new Map();
    this.last = new Map();
    this.pollCount = 0;
  }

  now() {
    return Date.now();
  }

  async init() {
    await this.refreshDailies();
  }

  async refreshDailies() {
    const today = nyParts().dateKey;
    await Promise.all(
      Object.keys(this.cfg.tickers).map(async (sym) => {
        try {
          const res = await fetchChart(this.cfg, sym, {
            interval: '1d',
            range: '1y',
            includePrePost: 'false',
          });
          this.dailies.set(sym, parseDailyCloses(res, today));
        } catch {
          // keep whatever we had; fallbackLevels cover a cold start
        }
      }),
    );
    this.dailiesDay = today;
  }

  async poll() {
    this.pollCount++;
    const today = nyParts().dateKey;
    const missingDailies = Object.keys(this.cfg.tickers).some(
      (s) => !this.dailies.has(s),
    );
    if (this.dailiesDay !== today || (missingDailies && this.pollCount % 20 === 0)) {
      await this.refreshDailies();
    }
    const out = new Map();
    await Promise.all(
      Object.keys(this.cfg.tickers).map(async (sym) => {
        try {
          const res = await fetchChart(this.cfg, sym, {
            interval: '1m',
            range: '1d',
            includePrePost: 'false',
          });
          const q = parseIntraday(res);
          q.fetchedAt = Date.now();
          this.last.set(sym, q);
          this.errors.set(sym, 0);
          out.set(sym, q);
        } catch {
          this.errors.set(sym, (this.errors.get(sym) ?? 0) + 1);
          const stale = this.last.get(sym);
          if (stale) out.set(sym, stale);
        }
      }),
    );
    return out;
  }

  session(quotes) {
    let tp = null;
    for (const q of quotes.values()) {
      if (q.tradingPeriod) {
        tp = q.tradingPeriod;
        break;
      }
    }
    return sessionState(this.cfg, new Date(), tp);
  }

  indicatorsFor(sym, livePrice) {
    const closes = this.dailies.get(sym);
    if (!closes || closes.length < 20) return null;
    return {
      ema20: liveEma(closes, livePrice, 20),
      sma50: liveSma(closes, livePrice, 50),
      sma200: liveSma(closes, livePrice, 200),
    };
  }

  errorSummary() {
    const bad = [...this.errors.entries()].filter(([, n]) => n > 2);
    return bad.length ? bad.map(([s, n]) => `${s}x${n}`).join(' ') : null;
  }
}

// Scripted demo: exercises every alert type, cooldown suppression, session
// chimes and the dashboard, with no network. One step ≈ 8 simulated minutes.
const SIM_PATHS = {
  CORN: [20.03, 19.9, 19.7, 19.5, 19.32, 19.25, 19.18, 19.15, 19.22, 19.3, 19.18, 19.12, 19.1, 19.1, 19.1, 19.08, 19.08, 19.08, 19.08, 19.08],
  WEAT: [26.99, 26.8, 26.6, 26.45, 26.38, 26.1, 25.6, 25.2, 25.05, 24.96, 24.9, 25.1, 25.3, 25.45, 25.6, 25.7, 25.8, 25.9, 25.95, 26.0],
  SOYB: [26.75, 26.6, 26.5, 26.42, 26.36, 26.31, 26.28, 26.3, 26.35, 26.4, 26.45, 26.5, 26.52, 26.55, 26.55, 26.55, 26.55, 26.55, 26.55, 26.55],
  CANE: [11.23, 11.26, 11.29, 11.32, 11.36, 11.4, 11.44, 11.47, 11.52, 11.58, 11.6, 11.55, 11.53, 11.56, 11.58, 11.6, 11.6, 11.6, 11.6, 11.6],
  DBA: [28.59, 28.63, 28.68, 28.74, 28.79, 28.84, 28.9, 28.71, 28.4, 28.02, 27.8, 27.55, 27.62, 27.9, 28.05, 28.1, 28.15, 28.2, 28.2, 28.2],
};
const SIM_STEPS = 20;

class SimFeed {
  constructor(cfg) {
    this.cfg = cfg;
    this.step = -1;
    this.fakeMs = Date.parse('2026-08-27T13:25:00Z');
    this.prevCloses = Object.fromEntries(
      Object.keys(cfg.tickers).map((s) => [s, SIM_PATHS[s]?.[0] ?? 0]),
    );
  }

  now() {
    return this.fakeMs;
  }

  async init() {}

  done() {
    return this.step >= SIM_STEPS - 1;
  }

  async poll() {
    this.step++;
    this.fakeMs += 8 * 60 * 1000;
    const out = new Map();
    for (const sym of Object.keys(this.cfg.tickers)) {
      const p = SIM_PATHS[sym];
      const price = p ? p[Math.min(this.step, p.length - 1)] : null;
      out.set(sym, {
        price,
        prevClose: this.prevCloses[sym],
        dayHigh: price,
        dayLow: price,
        marketTime: this.fakeMs / 1000,
        fetchedAt: this.fakeMs,
      });
    }
    return out;
  }

  session() {
    const open = this.step >= 1 && this.step <= SIM_STEPS - 2;
    return {
      open,
      late: open && this.step >= 15,
      weekend: false,
      dateKey: 'SIM',
      clock: `SIM ${Math.max(0, this.step)}/${SIM_STEPS - 1}`,
      weekday: 'Sim',
      minutes: 0,
    };
  }

  indicatorsFor(sym) {
    return this.cfg.tickers[sym]?.fallbackLevels ?? null;
  }

  errorSummary() {
    return null;
  }
}

// ── Alert dispatch ──────────────────────────────────────────────────────────

const SOUND_FOR_TYPE = {
  'buy-dip': 'buy-dip',
  'buy-breakout': 'buy-breakout',
  'stop-warn': 'stop-warn',
  'close-warn': 'close-warn',
  chime: 'chime',
};
const COLOR_FOR_TYPE = {
  'buy-dip': C.cyan,
  'buy-breakout': C.green,
  'stop-warn': C.red,
  'close-warn': C.yellow,
  chime: C.gray,
};

function appendAlertLog(line) {
  fs.appendFile(ALERT_LOG, line + '\n', () => {});
}

class Scanner {
  constructor(cfg, feed, opts) {
    this.cfg = cfg;
    this.feed = feed;
    this.opts = opts; // { quiet, once, simulate }
    this.monitors = new Map(
      Object.keys(cfg.tickers).map((s) => [s, new TickerMonitor(s, cfg.tickers[s])]),
    );
    this.rows = new Map();
    this.log = [];
    this.session = null;
    this.prevOpen = null;
    this.nextPollAt = 0;
    this.stats = { alerts: 0, sounded: 0, suppressed: 0 };
    this.painted = false;
  }

  handleAlert(ev, session) {
    this.stats.alerts++;
    if (ev.suppressed) this.stats.suppressed++;
    const stamp = this.opts.simulate
      ? session.clock
      : `${nyParts(new Date(this.feed.now())).clock} NY`;
    const tags = [
      ev.suppressed ? '[cooldown]' : '',
      ev.startup ? '[active at startup]' : '',
      !session.open && ev.type !== 'chime' ? '[off-session]' : '',
    ]
      .filter(Boolean)
      .join(' ');
    const line = `${stamp}  ${ev.type.toUpperCase().padEnd(12)} ${ev.msg}${tags ? ' ' + tags : ''}`;
    this.log.unshift({ line, type: ev.type });
    this.log = this.log.slice(0, 60);
    appendAlertLog(`${new Date().toISOString()} ${line}`);

    const soundOk =
      !ev.suppressed &&
      (ev.type === 'chime' ||
        session.open ||
        this.cfg.session.soundOutsideSession === true);
    if (soundOk) {
      this.stats.sounded++;
      playSound(this.cfg, SOUND_FOR_TYPE[ev.type] ?? 'chime', this.opts.quiet);
      sayVoice(this.cfg, ev.voice, this.opts.quiet);
      if (ev.type !== 'chime') {
        itermNotify(ev.msg);
        itermAttention();
      }
    }
    if (!IS_TTY) {
      console.log(`[${stamp}] ${ev.type.toUpperCase()} ${ev.msg}${tags ? ' ' + tags : ''}`);
    }
  }

  chime(session, open) {
    this.handleAlert(
      {
        type: 'chime',
        symbol: '*',
        price: null,
        msg: open
          ? `Market open — scanner armed (${this.cfg.session.open}–${this.cfg.session.close} NY).`
          : 'Market closed — alerts muted until next open.',
        voice: open ? 'Market open. Scanner armed.' : 'Market closed.',
        suppressed: false,
        startup: false,
      },
      session,
    );
  }

  async tick() {
    const quotes = await this.feed.poll();
    const session = this.feed.session(quotes);
    this.session = session;
    if (
      this.prevOpen !== null &&
      session.open !== this.prevOpen &&
      this.cfg.session.chimeOnOpenClose
    ) {
      this.chime(session, session.open);
    }
    this.prevOpen = session.open;

    const opts = {
      hystPct: this.cfg.hysteresisPct,
      cooldownMs: this.cfg.alertCooldownMin * 60 * 1000,
      lateWarn: this.cfg.session.lateWarn,
    };
    for (const sym of Object.keys(this.cfg.tickers)) {
      const q = quotes.get(sym) ?? null;
      const ind = this.feed.indicatorsFor(sym, q?.price);
      const levels = resolveLevels(this.cfg.tickers[sym], ind);
      const mon = this.monitors.get(sym);
      if (q && Number.isFinite(q.price)) {
        const events = mon.evaluate(
          { price: q.price, nowMs: this.feed.now() },
          levels,
          session,
          opts,
        );
        for (const ev of events) this.handleAlert(ev, session);
      }
      this.rows.set(sym, {
        quote: q,
        levels,
        state: stateLabel(mon, q?.price, levels, this.cfg.nearPct),
        errs: this.feed.errors?.get?.(sym) ?? 0,
      });
    }

    if (IS_TTY) {
      const summary = [...this.rows.entries()]
        .map(([s, r]) => `${s} ${fmt(r.quote?.price)}`)
        .join(' · ');
      setTitle(`AG scanner · ${summary}`);
    }
  }

  // ── Rendering ─────────────────────────────────────────────────────────

  frameLines() {
    const cfg = this.cfg;
    const s = this.session;
    const lines = [];
    const W = { tkr: 6, last: 9, chg: 9, range: 16, trig: 16, zone: 25, state: 16 };
    const now = new Date();
    const clock = this.opts.simulate ? s?.clock ?? 'SIM' : `NY ${nyParts(now).clock}`;
    const mkt = s?.open
      ? cell(s.late ? ' OPEN·LATE ' : ' OPEN ', s.late ? 11 : 6, C.bgGreen)
      : cell(' CLOSED ', 8, C.bgRed);
    const nextIn = Math.max(0, Math.ceil((this.nextPollAt - Date.now()) / 1000));
    const errSum = this.feed.errorSummary?.();

    lines.push(
      `${C.bold}AG CALL SCANNER${C.reset}  ${Object.keys(cfg.tickers).join(' ')}   ${clock}  ${mkt}  ${C.dim}poll ${cfg.pollSeconds}s · next ${nextIn}s${C.reset}${errSum ? `  ${C.red}quote errs: ${errSum}${C.reset}` : ''}`,
    );
    lines.push(C.gray + '─'.repeat(100) + C.reset);
    lines.push(
      C.bold +
        cell('TKR', W.tkr) +
        cell('LAST', W.last) +
        cell('CHG%', W.chg) +
        cell('DAY RANGE', W.range) +
        cell('TRIGGER', W.trig) +
        cell('BUY ZONE', W.zone) +
        cell('STATE', W.state) +
        C.reset,
    );

    for (const [sym, r] of this.rows.entries()) {
      const q = r.quote;
      const price = q?.price;
      const chg =
        Number.isFinite(price) && Number.isFinite(q?.prevClose)
          ? ((price - q.prevClose) / q.prevClose) * 100
          : null;
      const E = r.levels.dipEntry.value;
      const S = r.levels.stop.value;
      const B = r.levels.breakout.value;

      let trig = '—';
      if (Number.isFinite(B) && Number.isFinite(price)) {
        const d = ((B - price) / price) * 100;
        trig = `${fmt(B)} ${d <= 0 ? 'HIT' : fmtPct(d)}`;
      } else if (Number.isFinite(B)) {
        trig = fmt(B);
      }

      let zone = '—';
      if (Number.isFinite(E) && Number.isFinite(S)) {
        let dist = '';
        if (Number.isFinite(price)) {
          if (price > E) dist = ` ${fmtPct(((E - price) / price) * 100)}`;
          else if (price >= S) dist = ' IN';
          else dist = ' BELOW';
        }
        zone = `${fmt(E)} > ${fmt(S)}${dist}`;
      }

      const chgColor = chg == null ? C.gray : chg >= 0 ? C.green : C.red;
      const stale =
        r.errs > 2 ||
        (s?.open &&
          !this.opts.simulate &&
          Number.isFinite(q?.marketTime) &&
          Date.now() / 1000 - q.marketTime > 240);
      lines.push(
        cell(sym, W.tkr, C.bold) +
          cell(fmt(price) + (stale ? '*' : ''), W.last, stale ? C.yellow : undefined) +
          cell(fmtPct(chg), W.chg, chgColor) +
          cell(
            Number.isFinite(q?.dayLow) && Number.isFinite(q?.dayHigh)
              ? `${fmt(q.dayLow)}-${fmt(q.dayHigh)}`
              : '—',
            W.range,
          ) +
          cell(trig, W.trig, Number.isFinite(B) ? undefined : C.gray) +
          cell(zone, W.zone) +
          cell(` ${r.state.text} `, W.state, r.state.color),
      );
    }

    lines.push(C.gray + '─'.repeat(100) + C.reset);
    lines.push(
      `${C.dim}Session ${cfg.session.open}-${cfg.session.close} NY · sounds ${
        this.opts.quiet || !cfg.sounds.enabled ? 'OFF' : 'in-session only'
      } · cooldown ${cfg.alertCooldownMin}m · zone = entry > stop (dynamic ema/sma track live)${C.reset}`,
    );

    lines.push('');
    lines.push(C.bold + 'PLAN' + C.reset);
    for (const [sym, t] of Object.entries(cfg.tickers)) {
      const r = this.rows.get(sym);
      const lv = r?.levels;
      const seg = (label, l) =>
        l && l.value != null
          ? `${label} ${fmt(l.value)}${l.label && l.label !== 'fixed' ? `(${l.label})` : ''}`
          : null;
      const parts = [
        seg('entry', lv?.dipEntry),
        seg('stop', lv?.stop),
        seg('trig', lv?.breakout),
      ].filter(Boolean);
      lines.push(
        cell(sym, 6, C.bold) +
          cell(parts.join('  '), 53) +
          C.dim +
          (t.note ?? '') +
          C.reset,
      );
    }

    lines.push('');
    lines.push(C.bold + 'ALERTS' + C.reset + C.dim + '  (newest first, also in alerts.log)' + C.reset);
    if (!this.log.length) lines.push(C.gray + '  none yet' + C.reset);
    for (const item of this.log.slice(0, 8)) {
      lines.push('  ' + (COLOR_FOR_TYPE[item.type] ?? '') + item.line + C.reset);
    }
    lines.push('');
    lines.push(C.gray + 'Ctrl-C to quit' + C.reset);
    return lines;
  }

  paint() {
    if (!IS_TTY) return;
    const out = ['\x1b[H'];
    for (const l of this.frameLines()) out.push(l + '\x1b[K\n');
    out.push('\x1b[J');
    process.stdout.write(out.join(''));
    this.painted = true;
  }

  printPlain() {
    const strip = (s) => s.replace(/\x1b\[[0-9;]*m/g, '');
    for (const l of this.frameLines()) console.log(strip(l));
  }
}

// ── Modes ───────────────────────────────────────────────────────────────────

async function runLive(cfg, opts) {
  const feed = opts.simulate ? new SimFeed(cfg) : new YahooFeed(cfg);
  const scanner = new Scanner(cfg, feed, opts);

  if (!opts.simulate) {
    process.stdout.write('Loading daily history for dynamic levels…\n');
  }
  await feed.init();

  if (IS_TTY && !opts.once) {
    process.stdout.write('\x1b[2J\x1b[H\x1b[?25l');
    setBadge('AG');
  }
  const cleanup = () => {
    if (IS_TTY) process.stdout.write('\x1b[?25h' + C.reset + '\n');
  };
  process.on('SIGINT', () => {
    cleanup();
    process.exit(0);
  });
  process.on('SIGTERM', () => {
    cleanup();
    process.exit(0);
  });

  if (opts.once) {
    await scanner.tick();
    scanner.printPlain();
    return;
  }

  if (IS_TTY) {
    setInterval(() => scanner.paint(), 1000).unref();
  }

  const stepMs = opts.simulate ? (opts.fast ? 60 : 1200) : null;
  for (;;) {
    await scanner.tick();
    scanner.paint();
    if (opts.simulate) {
      if (feed.done()) break;
      await delay(stepMs);
      continue;
    }
    const pollMs =
      (scanner.session?.open ? cfg.pollSeconds : cfg.offPollSeconds ?? 60) * 1000;
    scanner.nextPollAt = Date.now() + pollMs;
    await delay(pollMs);
  }

  if (opts.simulate) {
    cleanup();
    const { alerts, sounded, suppressed } = scanner.stats;
    console.log(
      `\nSimulation complete — ${alerts} alerts (${sounded} sounded, ${suppressed} cooldown-suppressed).`,
    );
    console.log('Alert log:');
    for (const item of [...scanner.log].reverse()) console.log('  ' + item.line);
  }
}

async function runTestSound(cfg) {
  console.log(`Sound test (${IS_MAC ? 'afplay + say + BEL' : 'BEL only on this OS'})…`);
  for (const key of Object.keys(cfg.sounds.map)) {
    console.log(`  ${key}  →  ${cfg.sounds.map[key].file} x${cfg.sounds.map[key].repeat}`);
    playSound(cfg, key, false);
    sayVoice(cfg, key.replace(/-/g, ' '), false);
    await delay(1800);
  }
  console.log('Done. If you heard nothing on macOS, check volume / config sound paths.');
}

// ── Self-test (offline) ─────────────────────────────────────────────────────

function runCheck(configPath) {
  let pass = 0;
  let fail = 0;
  const t = (name, cond) => {
    if (cond) {
      pass++;
      console.log(`  ok   ${name}`);
    } else {
      fail++;
      console.log(`  FAIL ${name}`);
    }
  };
  const approx = (a, b, eps = 1e-9) => Math.abs(a - b) < eps;

  console.log('config');
  let cfg = null;
  try {
    cfg = loadConfig(configPath);
    t('loads and validates', true);
  } catch (err) {
    t(`loads and validates (${err.message})`, false);
  }
  if (cfg) {
    t('has all 5 tickers', ['WEAT', 'CORN', 'SOYB', 'CANE', 'DBA'].every((s) => cfg.tickers[s]));
    t(
      'every ticker has fallback levels',
      Object.values(cfg.tickers).every((tk) => tk.fallbackLevels?.ema20 > 0),
    );
    t(
      'sim paths cover configured tickers',
      Object.keys(cfg.tickers).every((s) => SIM_PATHS[s]?.length === SIM_STEPS),
    );
  }

  console.log('indicators');
  t('ema constant series', approx(emaLast([5, 5, 5, 5, 5], 3), 5));
  t('ema known small case', approx(emaLast([2, 4, 6, 8], 3), 6));
  t('liveEma folds price', approx(liveEma([2, 4, 6, 8], 10, 3), 8));
  t('sma', approx(smaLast([1, 2, 3, 4, 5], 5), 3));
  t('liveSma folds price', approx(liveSma([1, 2, 3, 4], 5, 5), 3));
  t('short series -> null', emaLast([1, 2], 20) === null && liveSma([1], 2, 50) === null);

  console.log('parsing');
  const nowSec = Math.floor(Date.now() / 1000);
  const intradayFixture = {
    meta: {
      regularMarketPrice: 20.03,
      chartPreviousClose: 19.59,
      regularMarketTime: nowSec,
      currentTradingPeriod: { regular: { start: nowSec - 3600, end: nowSec + 3600 } },
    },
    timestamp: [nowSec - 120, nowSec - 60],
    indicators: { quote: [{ high: [19.9, null, 20.08], low: [19.64, null], close: [19.8, 20.0] }] },
  };
  const iq = parseIntraday(intradayFixture);
  t('intraday price/prevClose', approx(iq.price, 20.03) && approx(iq.prevClose, 19.59));
  t('intraday hi/lo from arrays with nulls', approx(iq.dayHigh, 20.08) && approx(iq.dayLow, 19.64));
  t('intraday trading period', iq.tradingPeriod.start === nowSec - 3600);
  const dailyFixture = {
    timestamp: [nowSec - 3 * 86400, nowSec - 2 * 86400, nowSec],
    indicators: { quote: [{ close: [10, 11, 12] }] },
  };
  const closes = parseDailyCloses(dailyFixture, nyParts().dateKey);
  t("daily parse drops today's forming candle", closes.length === 2 && closes[1] === 11);

  console.log('levels');
  const ind = { ema20: 18.57, sma50: 17.8, sma200: 17.9 };
  const lv1 = resolveLevels({ dipEntry: 19.2, stop: 'ema20' }, ind);
  t('static entry + dynamic stop', approx(lv1.dipEntry.value, 19.2) && approx(lv1.stop.value, 18.57));
  const lv2 = resolveLevels(
    { dipEntry: 'ema20', stop: 'sma200', fallbackLevels: { ema20: 10.75, sma200: 9.74 } },
    null,
  );
  t('fallback used when no live data', approx(lv2.dipEntry.value, 10.75) && lv2.dipEntry.approx === true);
  const lv3 = resolveLevels({ dipEntry: 19.2, stop: 'ema20' }, { ema20: 19.5 });
  t('inverted zone follows trailing stop', lv3.inverted === true && approx(lv3.dipEntry.value, 19.5));

  console.log('state machine');
  const tcfg = { dipEntry: 28.03, stop: 27.59, breakout: 28.8 };
  const mkLevels = () =>
    resolveLevels(tcfg, null);
  const mon = new TickerMonitor('DBA', tcfg);
  const sess = { open: true, late: false, dateKey: 'D1' };
  const opts = { hystPct: 0.1, cooldownMs: 25 * 60 * 1000, lateWarn: '15:45' };
  let tMs = 0;
  const step = (price, dtMs = 60 * 1000) => {
    tMs += dtMs;
    return mon.evaluate({ price, nowMs: tMs }, mkLevels(), sess, opts);
  };
  t('watch: no events', step(28.5).length === 0);
  let ev = step(28.85);
  t('breakout fires', ev.length === 1 && ev[0].type === 'buy-breakout' && !ev[0].suppressed);
  t('breakout latched: no repeat', step(28.9).length === 0);
  ev = step(28.0);
  t('dip fires on zone entry', ev.length === 1 && ev[0].type === 'buy-dip' && !ev[0].msg.includes('reclaimed'));
  ev = step(27.4);
  t('stop-warn fires below stop', ev.length === 1 && ev[0].type === 'stop-warn');
  t('below stop: no dip', step(27.5).length === 0);
  ev = step(27.65);
  t(
    'reclaim within cooldown -> suppressed',
    ev.length === 1 && ev[0].type === 'buy-dip' && ev[0].suppressed && ev[0].msg.includes('reclaimed'),
  );
  ev = step(29.0, 30 * 60 * 1000);
  t('breakout re-arms after hysteresis + cooldown', ev.length === 1 && ev[0].type === 'buy-breakout' && !ev[0].suppressed);

  const mon2 = new TickerMonitor('CANE', { breakout: 11.5 });
  ev = mon2.evaluate({ price: 11.6, nowMs: 1 }, resolveLevels({ breakout: 11.5 }, null), sess, opts);
  t('startup-active condition alerts once, tagged', ev.length === 1 && ev[0].startup === true);

  const mon3 = new TickerMonitor('CORN', { dipEntry: 19.2, stop: 18.55, closeWarnBelow: 19.2 });
  const lvC = resolveLevels({ dipEntry: 19.2, stop: 18.55 }, null);
  const late = { open: true, late: true, dateKey: 'D1' };
  ev = mon3.evaluate({ price: 19.1, nowMs: 1 }, lvC, late, opts);
  t('close-warn fires late session', ev.some((e) => e.type === 'close-warn'));
  ev = mon3.evaluate({ price: 19.05, nowMs: 2 }, lvC, late, opts);
  t('close-warn once per day', !ev.some((e) => e.type === 'close-warn'));
  ev = mon3.evaluate({ price: 19.05, nowMs: 3 }, lvC, { ...late, dateKey: 'D2' }, opts);
  t('close-warn re-arms next day', ev.some((e) => e.type === 'close-warn'));

  console.log('session');
  const sCfg = { session: { open: '09:30', close: '16:00', lateWarn: '15:45' } };
  t('hmToMin', hmToMin('09:30') === 570 && hmToMin('16:00') === 960);
  t('Tue 10:00 NY open', sessionState(sCfg, new Date('2026-09-01T14:00:00Z')).open === true);
  t('Tue 09:00 NY closed', sessionState(sCfg, new Date('2026-09-01T13:00:00Z')).open === false);
  t('Sat closed', sessionState(sCfg, new Date('2026-08-29T15:00:00Z')).open === false);
  const lateS = sessionState(sCfg, new Date('2026-09-01T19:50:00Z'));
  t('Tue 15:50 NY late', lateS.open === true && lateS.late === true);
  const nowD = new Date('2026-09-01T14:00:00Z');
  const tp = { start: nowD.getTime() / 1000 - 600, end: nowD.getTime() / 1000 + 600 };
  t('yahoo trading period wins (open)', sessionState(sCfg, nowD, tp).open === true);
  t(
    'yahoo trading period wins (closed/holiday)',
    sessionState(sCfg, nowD, { start: tp.start - 86400, end: tp.end - 86400 }).open === false,
  );

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  if (hasFlag('--help') || hasFlag('-h')) {
    console.log(
      fs
        .readFileSync(__filename, 'utf8')
        .split('*/')[0]
        .split('\n')
        .filter((l) => l.startsWith(' *'))
        .map((l) => l.replace(/^ \*\/?/, ''))
        .join('\n'),
    );
    return;
  }

  const configPath = path.resolve(flagValue('--config', path.join(__dirname, 'config.json')));
  if (hasFlag('--check')) return runCheck(configPath);

  const cfg = loadConfig(configPath);
  const interval = Number(flagValue('--interval', cfg.pollSeconds));
  if (Number.isFinite(interval) && interval >= 5 && interval <= 300) {
    cfg.pollSeconds = interval;
  }

  if (hasFlag('--test-sound')) return runTestSound(cfg);

  await runLive(cfg, {
    quiet: hasFlag('--quiet'),
    once: hasFlag('--once'),
    simulate: hasFlag('--simulate'),
    fast: hasFlag('--fast'),
  });
}

main().catch((err) => {
  if (IS_TTY) process.stdout.write('\x1b[?25h' + C.reset + '\n');
  console.error(`scanner error: ${err.message}`);
  process.exit(1);
});
