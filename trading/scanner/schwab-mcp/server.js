#!/usr/bin/env node
/**
 * Schwab market data MCP server (stdio).
 *
 * Exposes the Schwab Trader API's market data as MCP tools: quotes, daily price
 * history, option chains, and a one-call export of the CSV that backtest.js
 * reads with --load.
 *
 * Credentials come from the environment and are never written to disk or echoed
 * in output:
 *
 *   SCHWAB_APP_KEY        app key from developer.schwab.com
 *   SCHWAB_APP_SECRET     app secret
 *   SCHWAB_REFRESH_TOKEN  from your OAuth callback; lasts about 7 days
 *
 *   node server.js              run as an MCP stdio server
 *   node server.js --selftest   offline checks of the pure functions, exit 0/1
 */
import fs from 'node:fs';
import path from 'node:path';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

const TOKEN_URL = 'https://api.schwabapi.com/v1/oauth/token';
const MARKETDATA = 'https://api.schwabapi.com/marketdata/v1';
const DEFAULT_SYMBOLS = ['WEAT', 'CORN', 'SOYB', 'CANE', 'DBA'];

// ── auth ────────────────────────────────────────────────────────────────────

let cachedToken = null; // { value, expiresAt }

function credentials() {
  const key = process.env.SCHWAB_APP_KEY;
  const secret = process.env.SCHWAB_APP_SECRET;
  const refresh = process.env.SCHWAB_REFRESH_TOKEN;
  const missing = [
    !key && 'SCHWAB_APP_KEY',
    !secret && 'SCHWAB_APP_SECRET',
    !refresh && 'SCHWAB_REFRESH_TOKEN',
  ].filter(Boolean);
  if (missing.length) {
    throw new Error(
      `Missing ${missing.join(', ')} in the server environment. Set them where this MCP ` +
      'server is launched (the env block of your MCP config, or the shell that starts it) ' +
      'and restart it.',
    );
  }
  return { key, secret, refresh };
}

async function accessToken() {
  if (cachedToken && cachedToken.expiresAt > Date.now() + 60_000) return cachedToken.value;
  const { key, secret, refresh } = credentials();
  const res = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: {
      Authorization: `Basic ${Buffer.from(`${key}:${secret}`).toString('base64')}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: refresh }),
    signal: AbortSignal.timeout(20_000),
  });
  if (!res.ok) {
    throw new Error(
      res.status === 400 || res.status === 401
        ? 'Schwab rejected the refresh token (HTTP ' + res.status + '). These expire after ' +
          'about 7 days — re-run your OAuth flow to mint a new one, update ' +
          'SCHWAB_REFRESH_TOKEN, and restart this server.'
        : `Schwab token exchange failed with HTTP ${res.status}.`,
    );
  }
  const body = await res.json();
  if (!body?.access_token) throw new Error('Schwab token response contained no access_token.');
  cachedToken = {
    value: body.access_token,
    expiresAt: Date.now() + (Number(body.expires_in) || 1800) * 1000,
  };
  return cachedToken.value;
}

async function schwabGet(endpoint, params) {
  const token = await accessToken();
  const qs = new URLSearchParams(params).toString();
  const res = await fetch(`${MARKETDATA}${endpoint}?${qs}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
    signal: AbortSignal.timeout(30_000),
  });
  if (res.status === 401) {
    cachedToken = null; // force a refresh on the next call
    throw new Error('Schwab returned 401. The access token was refreshed; retry the call.');
  }
  if (res.status === 404) {
    throw new Error(`Schwab has no data for that request (404). Check the symbol spelling.`);
  }
  if (res.status === 429) {
    throw new Error('Schwab rate limit hit (429). Wait a minute, then retry with fewer symbols.');
  }
  if (!res.ok) throw new Error(`Schwab ${endpoint} failed with HTTP ${res.status}.`);
  return res.json();
}

// ── pure helpers (covered by --selftest) ────────────────────────────────────

// Candle datetimes are epoch ms, but some gateways hand back ISO strings.
export function parseCandles(body) {
  const candles = body?.candles;
  if (!Array.isArray(candles)) throw new Error('response contained no candles');
  const bars = [];
  for (const c of candles) {
    const close = Number(c?.close);
    if (!Number.isFinite(close) || close <= 0) continue;
    const raw = c?.datetime ?? c?.dateTime;
    const when = typeof raw === 'number' ? new Date(raw)
      : typeof raw === 'string' ? new Date(/^\d+$/.test(raw) ? Number(raw) : raw)
      : null;
    if (!when || Number.isNaN(when.getTime())) continue;
    bars.push({
      date: when.toISOString().slice(0, 10),
      open: Number(c.open), high: Number(c.high), low: Number(c.low),
      close, volume: Number(c.volume),
    });
  }
  bars.sort((a, b) => a.date.localeCompare(b.date));
  return bars;
}

export function sma(bars, period) {
  if (bars.length < period) return null;
  return bars.slice(-period).reduce((a, b) => a + b.close, 0) / period;
}

export function summarizeBars(symbol, bars) {
  if (!bars.length) return { symbol, bars: 0 };
  let hi = bars[0], lo = bars[0];
  for (const b of bars) {
    if (b.close > hi.close) hi = b;
    if (b.close < lo.close) lo = b;
  }
  const last = bars.at(-1);
  const prev = bars.length > 1 ? bars.at(-2) : null;
  return {
    symbol, bars: bars.length, from: bars[0].date, to: last.date,
    last: +last.close.toFixed(4),
    changePct: prev ? +(((last.close - prev.close) / prev.close) * 100).toFixed(2) : null,
    high: +hi.close.toFixed(4), highDate: hi.date,
    low: +lo.close.toFixed(4), lowDate: lo.date,
    pctOffHigh: +(((last.close - hi.close) / hi.close) * 100).toFixed(2),
    sma20: round4(sma(bars, 20)), sma50: round4(sma(bars, 50)), sma200: round4(sma(bars, 200)),
  };
}

const round4 = (x) => (Number.isFinite(x) ? +x.toFixed(4) : null);

// The raw chain is enormous; keep the columns an options trader actually reads.
export function flattenChain(body, { limit = 60 } = {}) {
  const out = [];
  for (const mapKey of ['callExpDateMap', 'putExpDateMap']) {
    const byExp = body?.[mapKey];
    if (!byExp) continue;
    for (const [expKey, strikes] of Object.entries(byExp)) {
      for (const [strike, contracts] of Object.entries(strikes)) {
        for (const c of contracts) {
          out.push({
            type: mapKey === 'callExpDateMap' ? 'CALL' : 'PUT',
            expiration: expKey.split(':')[0],
            dte: Number(expKey.split(':')[1] ?? NaN),
            strike: Number(strike),
            bid: numOrNull(c.bid), ask: numOrNull(c.ask), last: numOrNull(c.last),
            mark: numOrNull(c.mark),
            delta: numOrNull(c.delta), gamma: numOrNull(c.gamma),
            theta: numOrNull(c.theta), vega: numOrNull(c.vega), rho: numOrNull(c.rho),
            iv: numOrNull(c.volatility),
            openInterest: numOrNull(c.openInterest), volume: numOrNull(c.totalVolume),
            inTheMoney: c.inTheMoney ?? null,
          });
        }
      }
    }
  }
  out.sort((a, b) => a.expiration.localeCompare(b.expiration) || a.strike - b.strike);
  return { total: out.length, rows: out.slice(0, limit), truncated: out.length > limit };
}

const numOrNull = (x) => (Number.isFinite(Number(x)) ? Number(x) : null);

export function quoteRow(symbol, entry) {
  const q = entry?.quote ?? {};
  const r = entry?.reference ?? {};
  return {
    symbol,
    description: r.description ?? null,
    last: numOrNull(q.lastPrice),
    netChange: numOrNull(q.netChange),
    netPercentChange: numOrNull(q.netPercentChange),
    bid: numOrNull(q.bidPrice), ask: numOrNull(q.askPrice),
    volume: numOrNull(q.totalVolume),
    high: numOrNull(q.highPrice), low: numOrNull(q.lowPrice),
    week52High: numOrNull(q['52WeekHigh']), week52Low: numOrNull(q['52WeekLow']),
  };
}

export function toBacktestCsv(seriesBySymbol) {
  const rows = ['ticker,date,close'];
  for (const [symbol, bars] of Object.entries(seriesBySymbol)) {
    for (const b of bars) rows.push(`${symbol},${b.date},${b.close.toFixed(6)}`);
  }
  return rows.join('\n') + '\n';
}

// ── server ──────────────────────────────────────────────────────────────────

const ok = (data) => ({
  content: [{ type: 'text', text: JSON.stringify(data, null, 2) }],
  structuredContent: data,
});
const fail = (err) => ({
  content: [{ type: 'text', text: `Error: ${err instanceof Error ? err.message : String(err)}` }],
  isError: true,
});

function buildServer() {
  const server = new McpServer({ name: 'schwab-marketdata', version: '1.0.0' });

  server.registerTool('schwab_get_quotes', {
    title: 'Get Schwab quotes',
    description:
      'Current quote for one or more symbols: last price, net change, bid/ask, volume and the ' +
      '52-week range. Use for a point-in-time read; use schwab_get_price_history for series.',
    inputSchema: z.object({
      symbols: z.array(z.string().min(1)).min(1).max(25)
        .describe('Ticker symbols, e.g. ["WEAT","CORN"]. Indexes use $ (e.g. $SPX).'),
    }),
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, async ({ symbols }) => {
    try {
      const body = await schwabGet('/quotes', { symbols: symbols.join(','), indicative: 'false' });
      const rows = symbols.map((s) => (body?.[s] ? quoteRow(s, body[s]) : { symbol: s, error: 'not returned' }));
      return ok({ quotes: rows });
    } catch (err) { return fail(err); }
  });

  server.registerTool('schwab_get_price_history', {
    title: 'Get Schwab price history',
    description:
      'Daily/weekly/monthly candles for one symbol. Returns a compact summary by default ' +
      '(range, last, high/low with dates, 20/50/200 SMA) because full history is thousands of ' +
      'rows; set output to "rows" for the most recent bars, or "file" to write the whole series ' +
      'to disk and get the path back.',
    inputSchema: z.object({
      symbol: z.string().min(1).describe('Ticker symbol, e.g. "WEAT".'),
      years: z.number().int().min(1).max(20).default(20)
        .describe('Years of history. Schwab accepts 1, 2, 3, 5, 10, 15 or 20; other values round up.'),
      frequency: z.enum(['daily', 'weekly', 'monthly']).default('daily'),
      output: z.enum(['summary', 'rows', 'file']).default('summary'),
      limit: z.number().int().min(1).max(400).default(60)
        .describe('For output "rows": how many of the most recent bars to return.'),
      filePath: z.string().optional()
        .describe('For output "file": where to write the CSV. Defaults to ./<symbol>-history.csv'),
    }),
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, async ({ symbol, years, frequency, output, limit, filePath }) => {
    try {
      const allowed = [1, 2, 3, 5, 10, 15, 20];
      const period = allowed.find((p) => p >= years) ?? 20;
      const body = await schwabGet('/pricehistory', {
        symbol, periodType: 'year', period: String(period),
        frequencyType: frequency, frequency: '1', needExtendedHoursData: 'false',
      });
      if (body?.empty) throw new Error(`Schwab returned an empty series for ${symbol}.`);
      const bars = parseCandles(body);
      const summary = summarizeBars(symbol, bars);
      if (output === 'summary') return ok({ ...summary, note: 'Prices are unadjusted for distributions.' });
      if (output === 'rows') {
        return ok({ ...summary, returned: Math.min(limit, bars.length), rows: bars.slice(-limit) });
      }
      const target = path.resolve(filePath || `./${symbol}-history.csv`);
      fs.writeFileSync(target, toBacktestCsv({ [symbol]: bars }));
      return ok({ ...summary, file: target, rowsWritten: bars.length });
    } catch (err) { return fail(err); }
  });

  server.registerTool('schwab_get_option_chain', {
    title: 'Get Schwab option chain',
    description:
      'Option chain for one symbol, flattened to the columns that matter for a trade: strike, ' +
      'expiration, days to expiry, bid/ask/mark, the full greeks Schwab publishes (delta, gamma, ' +
      'theta, vega, rho), implied volatility, open interest and volume. Filter with contractType, ' +
      'strikeCount and a date range to keep it small.',
    inputSchema: z.object({
      symbol: z.string().min(1),
      contractType: z.enum(['CALL', 'PUT', 'ALL']).default('CALL'),
      strikeCount: z.number().int().min(1).max(50).default(10)
        .describe('Strikes above and below at-the-money.'),
      fromDate: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional()
        .describe('Earliest expiration, YYYY-MM-DD.'),
      toDate: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional()
        .describe('Latest expiration, YYYY-MM-DD.'),
      limit: z.number().int().min(1).max(200).default(60),
    }),
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, async ({ symbol, contractType, strikeCount, fromDate, toDate, limit }) => {
    try {
      const params = { symbol, contractType, strikeCount: String(strikeCount) };
      if (fromDate) params.fromDate = fromDate;
      if (toDate) params.toDate = toDate;
      const body = await schwabGet('/chains', params);
      const chain = flattenChain(body, { limit });
      return ok({
        symbol,
        underlyingPrice: numOrNull(body?.underlyingPrice),
        contractsAvailable: chain.total,
        contractsReturned: chain.rows.length,
        truncated: chain.truncated,
        contracts: chain.rows,
      });
    } catch (err) { return fail(err); }
  });

  server.registerTool('schwab_export_backtest_csv', {
    title: 'Export price history for backtest.js',
    description:
      'Fetches daily history for several symbols and writes the ticker,date,close CSV that ' +
      'backtest.js reads with --load. One call to produce the whole dataset for an analysis.',
    inputSchema: z.object({
      symbols: z.array(z.string().min(1)).min(1).max(15).default(DEFAULT_SYMBOLS),
      years: z.number().int().min(1).max(20).default(20),
      frequency: z.enum(['daily', 'monthly']).default('daily'),
      filePath: z.string().default('./bars.csv'),
    }),
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, async ({ symbols, years, frequency, filePath }) => {
    try {
      const allowed = [1, 2, 3, 5, 10, 15, 20];
      const period = allowed.find((p) => p >= years) ?? 20;
      const series = {};
      const failed = [];
      for (const symbol of symbols) {
        try {
          const body = await schwabGet('/pricehistory', {
            symbol, periodType: 'year', period: String(period),
            frequencyType: frequency, frequency: '1', needExtendedHoursData: 'false',
          });
          const bars = parseCandles(body);
          if (!bars.length) throw new Error('no candles');
          series[symbol] = bars;
        } catch (err) {
          failed.push({ symbol, reason: err.message });
        }
      }
      if (!Object.keys(series).length) {
        throw new Error(`No symbol returned data. First failure: ${failed[0]?.reason}`);
      }
      const target = path.resolve(filePath);
      fs.writeFileSync(target, toBacktestCsv(series));
      return ok({
        file: target,
        symbols: Object.keys(series),
        rowsWritten: Object.values(series).reduce((a, b) => a + b.length, 0),
        coverage: Object.fromEntries(Object.entries(series)
          .map(([s, b]) => [s, `${b[0].date} → ${b.at(-1).date} (${b.length})`])),
        failed,
        nextStep: `node backtest.js --load ${target}`,
        note: 'Schwab prices are unadjusted for distributions; returns exclude them.',
      });
    } catch (err) { return fail(err); }
  });

  return server;
}

// ── selftest ────────────────────────────────────────────────────────────────

function selftest() {
  let pass = 0, fail_ = 0;
  const t = (name, cond) => {
    if (cond) { pass++; console.log(`  ok   ${name}`); }
    else { fail_++; console.log(`  FAIL ${name}`); }
  };

  console.log('candles');
  const bars = parseCandles({ candles: [
    { datetime: Date.UTC(2024, 0, 3), open: 1, high: 2, low: 0.5, close: 20.5, volume: 10 },
    { datetime: Date.UTC(2024, 0, 2), open: 1, high: 2, low: 0.5, close: 20.0, volume: 12 },
    { datetime: Date.UTC(2024, 0, 4), close: null },
    { datetime: 'bad', close: 5 },
  ] });
  t('sorted ascending and bad rows dropped',
    bars.length === 2 && bars[0].date === '2024-01-02' && bars[1].close === 20.5);
  t('ISO datetimes accepted',
    parseCandles({ candles: [{ datetime: '2024-03-05T14:30:00Z', close: 9 }] })[0].date === '2024-03-05');
  let threw = false;
  try { parseCandles({}); } catch { threw = true; }
  t('missing candles rejected', threw);

  console.log('summary');
  const many = Array.from({ length: 250 }, (_, i) => ({
    date: `2024-${String(1 + (i % 12)).padStart(2, '0')}-01`, close: 10 + i,
  }));
  const s = summarizeBars('X', many);
  t('high/low and count', s.bars === 250 && s.high === 259 && s.low === 10);
  t('moving averages present', s.sma20 > 0 && s.sma50 > 0 && s.sma200 > 0);
  t('sma20 above sma200 on an uptrend', s.sma20 > s.sma200);
  t('sma null when too few bars', sma(many.slice(0, 5), 200) === null);
  t('empty series handled', summarizeBars('X', []).bars === 0);

  console.log('option chain');
  const chain = flattenChain({
    callExpDateMap: {
      '2026-09-19:22': {
        '28.0': [{ bid: 1.2, ask: 1.4, last: 1.3, mark: 1.3, delta: 0.52, gamma: 0.06,
                   theta: -0.0071, vega: 0.0412, rho: 0.0139, volatility: 31.2,
                   openInterest: 400, totalVolume: 25, inTheMoney: true }],
        '30.0': [{ bid: 0.5, ask: 0.7, delta: 0.28, volatility: 33.0, openInterest: 900, totalVolume: 60 }],
      },
    },
    putExpDateMap: { '2026-09-19:22': { '26.0': [{ bid: 0.3, ask: 0.5 }] } },
  }, { limit: 2 });
  t('flattens calls and puts', chain.total === 3);
  t('respects limit and flags truncation', chain.rows.length === 2 && chain.truncated === true);
  t('sorted by expiration then strike', chain.rows[0].strike === 26 && chain.rows[0].type === 'PUT');
  t('greeks carried through', chain.rows[1].delta === 0.52 && chain.rows[1].iv === 31.2);
  t('theta, vega and rho carried through',
    chain.rows[1].theta === -0.0071 && chain.rows[1].vega === 0.0412 && chain.rows[1].rho === 0.0139);
  t('absent second-order greeks become null', chain.rows[0].theta === null);
  t('missing numerics become null', flattenChain({
    callExpDateMap: { '2026-09-19:22': { '28.0': [{ bid: 'n/a' }] } } }).rows[0].bid === null);

  console.log('quotes');
  const q = quoteRow('WEAT', { quote: { lastPrice: 28, netChange: 0.78, netPercentChange: 2.87,
    bidPrice: 27.99, askPrice: 28.01, totalVolume: 1287821, '52WeekHigh': 30, '52WeekLow': 19.5 },
    reference: { description: 'Teucrium Wheat Fund' } });
  t('maps quote fields', q.last === 28 && q.netPercentChange === 2.87 && q.week52Low === 19.5);
  t('keeps the description', q.description === 'Teucrium Wheat Fund');
  t('absent quote yields nulls', quoteRow('X', {}).last === null);

  console.log('csv');
  const csv = toBacktestCsv({ AAA: [{ date: '2020-01-02', close: 10 }], BBB: [{ date: '2020-01-02', close: 5 }] });
  t('header and rows', csv.startsWith('ticker,date,close\n') && csv.trim().split('\n').length === 3);
  t('backtest-compatible shape', /^AAA,2020-01-02,10\.000000$/m.test(csv));

  console.log('credentials');
  const saved = ['SCHWAB_APP_KEY', 'SCHWAB_APP_SECRET', 'SCHWAB_REFRESH_TOKEN'].map((k) => [k, process.env[k]]);
  for (const [k] of saved) delete process.env[k];
  let msg = '';
  try { credentials(); } catch (e) { msg = e.message; }
  t('names every missing variable',
    /SCHWAB_APP_KEY/.test(msg) && /SCHWAB_APP_SECRET/.test(msg) && /SCHWAB_REFRESH_TOKEN/.test(msg));
  process.env.SCHWAB_APP_KEY = 'k';
  process.env.SCHWAB_APP_SECRET = 'super-secret';
  process.env.SCHWAB_REFRESH_TOKEN = 'r';
  t('returns creds when present', credentials().key === 'k');
  msg = '';
  delete process.env.SCHWAB_REFRESH_TOKEN;
  try { credentials(); } catch (e) { msg = e.message; }
  t('secret never echoed', !/super-secret/.test(msg));
  for (const [k, v] of saved) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }

  console.log(`\n${pass} passed, ${fail_} failed`);
  process.exit(fail_ ? 1 : 0);
}

// ── main ────────────────────────────────────────────────────────────────────

if (process.argv.includes('--selftest')) {
  selftest();
} else {
  const server = buildServer();
  await server.connect(new StdioServerTransport());
}
