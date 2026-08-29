#!/usr/bin/env node
/**
 * Ag ETF history & seasonality backtest — WEAT · CORN · SOYB · CANE · DBA
 *
 * Full daily history from Yahoo (range=max, split/dividend adjusted), then:
 *
 *   summary     inception, all-time high/low with dates, % off ATH, CAGR,
 *               annualized vol, max drawdown with peak/trough dates
 *   years       calendar-year total returns, side by side
 *   months      every month since inception as a year x month grid
 *   seasonality average / median return per calendar month + hit rate
 *   eras        pre-COVID vs COVID vs post-COVID segment stats
 *   corr        correlation of daily returns (which of these are one bet)
 *
 * Zero dependencies, Node >= 18. Run it where Yahoo is reachable.
 *
 *   node backtest.js                       every section, every ticker
 *   node backtest.js --section seasonality only that section
 *   node backtest.js --ticker WEAT,CORN    only these
 *   node backtest.js --from 2010-01-01     restrict the window
 *   node backtest.js --csv ./out           also write CSVs for a spreadsheet
 *   node backtest.js --check               offline self-test, exit 0/1
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const TICKERS = ['WEAT', 'CORN', 'SOYB', 'CANE', 'DBA'];
const HOSTS = ['query1.finance.yahoo.com', 'query2.finance.yahoo.com'];
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const argv = process.argv.slice(2);
const hasFlag = (n) => argv.includes(n);
const flagValue = (n, d) => {
  const i = argv.indexOf(n);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d;
};

// ── formatting ──────────────────────────────────────────────────────────────

const C = {
  reset: '\x1b[0m', bold: '\x1b[1m', dim: '\x1b[2m',
  red: '\x1b[31m', green: '\x1b[32m', gray: '\x1b[90m',
};
const NO_COLOR = hasFlag('--no-color') || !!process.env.NO_COLOR;
const paint = (s, c) => (NO_COLOR || !c ? s : c + s + C.reset);
const rpad = (s, w) => String(s ?? '').padEnd(w).slice(0, Math.max(w, String(s ?? '').length));
const lpad = (s, w) => {
  const t = String(s ?? '');
  return t.length >= w ? t : ' '.repeat(w - t.length) + t;
};
const pct = (x, dp = 1) => (Number.isFinite(x) ? `${x >= 0 ? '+' : ''}${x.toFixed(dp)}` : '—');
const money = (x) => (Number.isFinite(x) ? x.toFixed(2) : '—');
const pctColor = (x) => (!Number.isFinite(x) ? C.gray : x >= 0 ? C.green : C.red);

// ── data ────────────────────────────────────────────────────────────────────

async function fetchDaily(symbol) {
  const qs = new URLSearchParams({
    interval: '1d', range: 'max', includeAdjustedClose: 'true', events: 'div,split',
  }).toString();
  let lastErr;
  for (const host of HOSTS) {
    try {
      const res = await fetch(`https://${host}/v8/finance/chart/${symbol}?${qs}`, {
        headers: { 'User-Agent': UA, Accept: 'application/json' },
        signal: AbortSignal.timeout(20000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const r = body?.chart?.result?.[0];
      if (!r) throw new Error(body?.chart?.error?.description || 'empty result');
      return parseDaily(r);
    } catch (err) {
      lastErr = err;
    }
  }
  throw new Error(`${symbol}: ${lastErr?.message ?? 'fetch failed'}`);
}

// Adjusted close where Yahoo provides it: DBA pays distributions, and on an
// unadjusted series those show up as fake -3% days.
function parseDaily(result) {
  const ts = result.timestamp || [];
  const close = result.indicators?.quote?.[0]?.close || [];
  const adj = result.indicators?.adjclose?.[0]?.adjclose || [];
  const bars = [];
  for (let i = 0; i < ts.length; i++) {
    const c = Number.isFinite(adj[i]) ? adj[i] : close[i];
    if (!Number.isFinite(c) || c <= 0) continue;
    bars.push({ date: new Date(ts[i] * 1000).toISOString().slice(0, 10), close: c });
  }
  return bars;
}

// ── statistics ──────────────────────────────────────────────────────────────

function monthEnds(bars) {
  const out = [];
  for (let i = 0; i < bars.length; i++) {
    const key = bars[i].date.slice(0, 7);
    if (i === bars.length - 1 || bars[i + 1].date.slice(0, 7) !== key) {
      out.push({ key, date: bars[i].date, close: bars[i].close });
    }
  }
  return out;
}

// Month N's return needs month N-1's close, so the first month is skipped.
function monthlyReturns(bars) {
  const me = monthEnds(bars);
  const out = [];
  for (let i = 1; i < me.length; i++) {
    out.push({
      key: me[i].key,
      year: Number(me[i].key.slice(0, 4)),
      month: Number(me[i].key.slice(5, 7)),
      ret: ((me[i].close - me[i - 1].close) / me[i - 1].close) * 100,
    });
  }
  return out;
}

function yearlyReturns(bars) {
  const me = monthEnds(bars);
  const byYear = new Map();
  for (const m of me) {
    const y = Number(m.key.slice(0, 4));
    if (!byYear.has(y)) byYear.set(y, { first: m, last: m });
    else byYear.get(y).last = m;
  }
  const years = [...byYear.keys()].sort((a, b) => a - b);
  const out = [];
  for (const y of years) {
    // Chain from the prior year's close so a full calendar year is measured;
    // the inception year runs from the very first bar, not its first month end.
    const prev = byYear.get(y - 1)?.last;
    const base = prev ? prev.close : bars[0].close;
    const partial = !prev;
    out.push({
      year: y,
      ret: ((byYear.get(y).last.close - base) / base) * 100,
      partial,
    });
  }
  return out;
}

function maxDrawdown(bars) {
  let peak = -Infinity, peakDate = null;
  let worst = 0, from = null, to = null;
  for (const b of bars) {
    if (b.close > peak) {
      peak = b.close;
      peakDate = b.date;
    }
    const dd = ((b.close - peak) / peak) * 100;
    if (dd < worst) {
      worst = dd;
      from = peakDate;
      to = b.date;
    }
  }
  return { pct: worst, from, to };
}

function annualizedVol(bars) {
  if (bars.length < 30) return null;
  const rets = [];
  for (let i = 1; i < bars.length; i++) {
    rets.push(Math.log(bars[i].close / bars[i - 1].close));
  }
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const varr = rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1);
  return Math.sqrt(varr) * Math.sqrt(252) * 100;
}

function summarize(sym, bars) {
  const first = bars[0], last = bars.at(-1);
  let hi = bars[0], lo = bars[0];
  for (const b of bars) {
    if (b.close > hi.close) hi = b;
    if (b.close < lo.close) lo = b;
  }
  const years = (new Date(last.date) - new Date(first.date)) / (365.25 * 864e5);
  return {
    sym,
    bars: bars.length,
    start: first.date,
    startPx: first.close,
    end: last.date,
    endPx: last.close,
    high: hi,
    low: lo,
    offHigh: ((last.close - hi.close) / hi.close) * 100,
    offLow: ((last.close - lo.close) / lo.close) * 100,
    total: ((last.close - first.close) / first.close) * 100,
    cagr: years > 0 ? ((last.close / first.close) ** (1 / years) - 1) * 100 : null,
    vol: annualizedVol(bars),
    dd: maxDrawdown(bars),
    years,
  };
}

function seasonality(monthly) {
  const out = [];
  for (let m = 1; m <= 12; m++) {
    const rets = monthly.filter((x) => x.month === m).map((x) => x.ret);
    if (!rets.length) {
      out.push({ month: m, n: 0 });
      continue;
    }
    const sorted = [...rets].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    out.push({
      month: m,
      n: rets.length,
      avg: rets.reduce((a, b) => a + b, 0) / rets.length,
      median: sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2,
      hit: (rets.filter((r) => r > 0).length / rets.length) * 100,
      best: sorted.at(-1),
      worst: sorted[0],
    });
  }
  return out;
}

function segment(bars, from, to) {
  const seg = bars.filter((b) => b.date >= from && b.date < to);
  if (seg.length < 2) return null;
  return {
    from, to,
    ret: ((seg.at(-1).close - seg[0].close) / seg[0].close) * 100,
    vol: annualizedVol(seg),
    dd: maxDrawdown(seg).pct,
  };
}

// Correlation of daily returns on dates both series share.
function correlate(barsA, barsB) {
  const mapB = new Map(barsB.map((b) => [b.date, b.close]));
  const a = [], b = [];
  let prevA = null, prevB = null;
  for (const bar of barsA) {
    const cb = mapB.get(bar.date);
    if (cb === undefined) { prevA = null; prevB = null; continue; }
    if (prevA !== null) {
      a.push(Math.log(bar.close / prevA));
      b.push(Math.log(cb / prevB));
    }
    prevA = bar.close;
    prevB = cb;
  }
  if (a.length < 30) return null;
  const ma = a.reduce((x, y) => x + y, 0) / a.length;
  const mb = b.reduce((x, y) => x + y, 0) / b.length;
  let num = 0, da = 0, db = 0;
  for (let i = 0; i < a.length; i++) {
    num += (a[i] - ma) * (b[i] - mb);
    da += (a[i] - ma) ** 2;
    db += (b[i] - mb) ** 2;
  }
  return da && db ? num / Math.sqrt(da * db) : null;
}

// ── report sections ─────────────────────────────────────────────────────────

function printSummary(data) {
  console.log(paint('\nSUMMARY — full history, distribution-adjusted', C.bold));
  console.log(paint(
    '  ' + rpad('TKR', 6) + lpad('bars', 6) + '  ' + rpad('from', 11) +
    lpad('last', 8) + lpad('ATH', 9) + '  ' + rpad('on', 11) +
    lpad('%offATH', 9) + lpad('ATL', 8) + '  ' + rpad('on', 11) +
    lpad('total%', 9) + lpad('CAGR%', 8) + lpad('vol%', 7), C.dim));
  for (const { sym, s } of data) {
    console.log(
      '  ' + paint(rpad(sym, 6), C.bold) + lpad(s.bars, 6) + '  ' + rpad(s.start, 11) +
      lpad(money(s.endPx), 8) + lpad(money(s.high.close), 9) + '  ' + rpad(s.high.date, 11) +
      paint(lpad(pct(s.offHigh), 9), pctColor(s.offHigh)) +
      lpad(money(s.low.close), 8) + '  ' + rpad(s.low.date, 11) +
      paint(lpad(pct(s.total), 9), pctColor(s.total)) +
      paint(lpad(pct(s.cagr, 2), 8), pctColor(s.cagr)) +
      lpad(s.vol?.toFixed(1) ?? '—', 7));
  }
  console.log(paint('\n  worst drawdown', C.bold));
  for (const { sym, s } of data) {
    console.log('  ' + paint(rpad(sym, 6), C.bold) +
      paint(lpad(pct(s.dd.pct), 8), C.red) +
      paint(`   ${s.dd.from} → ${s.dd.to}`, C.dim));
  }
}

function printYears(data) {
  console.log(paint('\nCALENDAR-YEAR RETURNS %', C.bold));
  const years = [...new Set(data.flatMap(({ y }) => y.map((r) => r.year)))].sort();
  console.log(paint('  ' + rpad('year', 6) + data.map(({ sym }) => lpad(sym, 9)).join(''), C.dim));
  for (const yr of years) {
    let line = '  ' + rpad(yr, 6);
    for (const { y } of data) {
      const r = y.find((x) => x.year === yr);
      line += r
        ? paint(lpad(pct(r.ret) + (r.partial ? '*' : ''), 9), pctColor(r.ret))
        : lpad('—', 9);
    }
    console.log(line);
  }
  console.log(paint('  * partial year (from inception)', C.dim));
}

function printMonths(data) {
  for (const { sym, m } of data) {
    console.log(paint(`\nMONTH-BY-MONTH % — ${sym}`, C.bold));
    console.log(paint('  ' + rpad('year', 6) + MONTHS.map((x) => lpad(x, 7)).join('') +
      lpad('year', 8), C.dim));
    const years = [...new Set(m.map((x) => x.year))].sort();
    for (const yr of years) {
      let line = '  ' + rpad(yr, 6);
      let compound = 1;
      let any = false;
      for (let mo = 1; mo <= 12; mo++) {
        const r = m.find((x) => x.year === yr && x.month === mo);
        if (r) {
          compound *= 1 + r.ret / 100;
          any = true;
          line += paint(lpad(r.ret.toFixed(1), 7), pctColor(r.ret));
        } else line += paint(lpad('·', 7), C.gray);
      }
      const yrRet = any ? (compound - 1) * 100 : null;
      console.log(line + paint(lpad(pct(yrRet), 8), pctColor(yrRet)));
    }
  }
}

function printSeasonality(data) {
  console.log(paint('\nSEASONALITY — average % by calendar month (hit rate = share positive)', C.bold));
  console.log(paint('  ' + rpad('TKR', 6) + MONTHS.map((x) => lpad(x, 7)).join(''), C.dim));
  for (const { sym, seas } of data) {
    let avg = '  ' + paint(rpad(sym, 6), C.bold);
    let hit = '  ' + paint(rpad('', 6), C.dim);
    for (const s of seas) {
      avg += s.n ? paint(lpad(s.avg.toFixed(1), 7), pctColor(s.avg)) : lpad('—', 7);
      hit += s.n ? paint(lpad(`${s.hit.toFixed(0)}%`, 7), C.gray) : lpad('', 7);
    }
    console.log(avg);
    console.log(hit);
  }
  console.log(paint('\n  strongest / weakest month by average', C.bold));
  for (const { sym, seas } of data) {
    const valid = seas.filter((s) => s.n);
    if (!valid.length) continue;
    const best = valid.reduce((a, b) => (b.avg > a.avg ? b : a));
    const worst = valid.reduce((a, b) => (b.avg < a.avg ? b : a));
    console.log('  ' + paint(rpad(sym, 6), C.bold) +
      paint(`${MONTHS[best.month - 1]} ${pct(best.avg)}%`, C.green) +
      paint(` (n=${best.n}, ${best.hit.toFixed(0)}% positive)`, C.dim) +
      '   vs   ' + paint(`${MONTHS[worst.month - 1]} ${pct(worst.avg)}%`, C.red) +
      paint(` (n=${worst.n}, ${worst.hit.toFixed(0)}% positive)`, C.dim));
  }
}

const ERAS = [
  ['pre-COVID', '1900-01-01', '2020-02-20'],
  ['COVID crash + reflation', '2020-02-20', '2022-02-24'],
  ['Ukraine invasion → peak', '2022-02-24', '2022-12-31'],
  ['post-spike bear', '2022-12-31', '2025-01-01'],
  ['current', '2025-01-01', '2099-01-01'],
];

function printEras(data) {
  console.log(paint('\nERAS — total return % / annualized vol / max drawdown', C.bold));
  console.log(paint('  ' + rpad('era', 26) + data.map(({ sym }) => lpad(sym, 10)).join(''), C.dim));
  for (const [name, from, to] of ERAS) {
    let line = '  ' + rpad(name, 26);
    for (const { bars } of data) {
      const seg = segment(bars, from, to);
      line += seg ? paint(lpad(pct(seg.ret), 10), pctColor(seg.ret)) : lpad('—', 10);
    }
    console.log(line);
    let vline = '  ' + paint(rpad('  vol / maxDD', 26), C.dim);
    for (const { bars } of data) {
      const seg = segment(bars, from, to);
      vline += paint(lpad(seg ? `${seg.vol.toFixed(0)}/${seg.dd.toFixed(0)}` : '—', 10), C.dim);
    }
    console.log(vline);
  }
}

function printCorr(data) {
  console.log(paint('\nCORRELATION — daily returns, full overlap', C.bold));
  console.log(paint('  ' + rpad('', 6) + data.map(({ sym }) => lpad(sym, 8)).join(''), C.dim));
  for (const a of data) {
    let line = '  ' + paint(rpad(a.sym, 6), C.bold);
    for (const b of data) {
      const c = a.sym === b.sym ? 1 : correlate(a.bars, b.bars);
      line += lpad(c == null ? '—' : c.toFixed(2), 8);
    }
    console.log(line);
  }
}

// ── CSV ─────────────────────────────────────────────────────────────────────

function writeCsvs(dir, data) {
  fs.mkdirSync(dir, { recursive: true });
  const w = (name, rows) => {
    fs.writeFileSync(path.join(dir, name), rows.map((r) => r.join(',')).join('\n') + '\n');
    console.log(paint(`  wrote ${path.join(dir, name)}`, C.dim));
  };
  w('summary.csv', [
    ['ticker', 'start', 'end', 'last', 'ath', 'ath_date', 'pct_off_ath', 'atl', 'atl_date',
     'total_pct', 'cagr_pct', 'vol_pct', 'maxdd_pct', 'maxdd_from', 'maxdd_to'],
    ...data.map(({ sym, s }) => [sym, s.start, s.end, s.endPx.toFixed(4), s.high.close.toFixed(4),
      s.high.date, s.offHigh.toFixed(2), s.low.close.toFixed(4), s.low.date,
      s.total.toFixed(2), s.cagr?.toFixed(2), s.vol?.toFixed(2),
      s.dd.pct.toFixed(2), s.dd.from, s.dd.to]),
  ]);
  w('monthly.csv', [
    ['ticker', 'month', 'return_pct'],
    ...data.flatMap(({ sym, m }) => m.map((r) => [sym, r.key, r.ret.toFixed(4)])),
  ]);
  w('yearly.csv', [
    ['ticker', 'year', 'return_pct', 'partial'],
    ...data.flatMap(({ sym, y }) => y.map((r) => [sym, r.year, r.ret.toFixed(4), r.partial])),
  ]);
  w('seasonality.csv', [
    ['ticker', 'month', 'n', 'avg_pct', 'median_pct', 'hit_rate_pct', 'best_pct', 'worst_pct'],
    ...data.flatMap(({ sym, seas }) => seas.filter((s) => s.n).map((s) => [sym, MONTHS[s.month - 1],
      s.n, s.avg.toFixed(3), s.median.toFixed(3), s.hit.toFixed(1),
      s.best.toFixed(2), s.worst.toFixed(2)])),
  ]);
}

// ── self-test ───────────────────────────────────────────────────────────────

function runCheck() {
  let pass = 0, fail = 0;
  const t = (name, cond) => {
    if (cond) { pass++; console.log(`  ok   ${name}`); }
    else { fail++; console.log(`  FAIL ${name}`); }
  };
  const approx = (a, b, e = 1e-6) => Number.isFinite(a) && Math.abs(a - b) < e;
  const bar = (date, close) => ({ date, close });

  console.log('parsing');
  const fixture = {
    timestamp: [1577836800, 1577923200, 1578009600],
    indicators: { quote: [{ close: [10, 11, 12] }], adjclose: [{ adjclose: [9, 10, null] }] },
  };
  const parsed = parseDaily(fixture);
  t('prefers adjusted close', parsed[0].close === 9 && parsed[1].close === 10);
  t('falls back to raw close', parsed[2].close === 12);
  t('drops non-finite and non-positive',
    parseDaily({ timestamp: [1, 2], indicators: { quote: [{ close: [0, null] }] } }).length === 0);

  console.log('month/year aggregation');
  const bars = [
    bar('2020-01-15', 100), bar('2020-01-31', 110),
    bar('2020-02-14', 120), bar('2020-02-28', 99),
    bar('2020-03-31', 132),
    bar('2021-01-29', 66),
  ];
  const me = monthEnds(bars);
  t('month ends picked', me.length === 4 && me[0].close === 110 && me[1].close === 99);
  const m = monthlyReturns(bars);
  t('first month skipped (no prior close)', m.length === 3 && m[0].key === '2020-02');
  t('feb return', approx(m[0].ret, -10));
  t('mar return', approx(m[1].ret, (132 - 99) / 99 * 100));
  const y = yearlyReturns(bars);
  t('first year partial from inception', y[0].partial === true && approx(y[0].ret, 32));
  t('later year chains off prior close', y[1].partial === false && approx(y[1].ret, -50));

  console.log('risk stats');
  const dd = maxDrawdown([bar('a', 100), bar('b', 50), bar('c', 75), bar('d', 200), bar('e', 100)]);
  t('max drawdown magnitude', approx(dd.pct, -50));
  t('max drawdown is the first, deeper one', dd.from === 'a' && dd.to === 'b');
  t('no drawdown on a rising series',
    approx(maxDrawdown([bar('a', 1), bar('b', 2)]).pct, 0));
  t('vol of a flat series is zero',
    approx(annualizedVol(Array.from({ length: 40 }, (_, i) => bar(`d${i}`, 100))), 0));
  t('vol needs history', annualizedVol([bar('a', 1)]) === null);

  console.log('seasonality');
  const synth = [];
  for (let yr = 2011; yr <= 2020; yr++) {
    for (let mo = 1; mo <= 12; mo++) synth.push({ year: yr, month: mo, ret: mo === 6 ? 5 : -1 });
  }
  const seas = seasonality(synth);
  t('june average', approx(seas[5].avg, 5) && seas[5].n === 10);
  t('june hit rate 100%', approx(seas[5].hit, 100));
  t('other months negative', approx(seas[0].avg, -1) && approx(seas[0].hit, 0));
  t('median equals value when constant', approx(seas[5].median, 5));

  console.log('correlation');
  const a = Array.from({ length: 100 }, (_, i) => bar(`2020-01-${i}`, 100 + i));
  const bUp = a.map((x) => bar(x.date, x.close * 2));
  const zig = [bar('d0', 100)];
  const mirror = [bar('d0', 100)];
  for (let i = 1; i < 100; i++) {
    const r = i % 2 ? 1.02 : 1 / 1.02;
    zig.push(bar(`d${i}`, zig[i - 1].close * r));
    mirror.push(bar(`d${i}`, mirror[i - 1].close / r));
  }
  t('identical shape correlates ~1', Math.abs(correlate(a, bUp) - 1) < 1e-6);
  t('mirrored returns correlate ~-1', Math.abs(correlate(zig, mirror) + 1) < 1e-6);
  t('no overlap -> null',
    correlate(a, [bar('1999-01-01', 5), bar('1999-01-02', 6)]) === null);

  console.log('segments');
  const segBars = [bar('2019-01-01', 100), bar('2020-01-01', 150), bar('2023-01-01', 75)];
  t('era slice respects bounds', approx(segment(segBars, '1900-01-01', '2020-02-20').ret, 50));
  t('empty era -> null', segment(segBars, '2030-01-01', '2031-01-01') === null);

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

// ── main ────────────────────────────────────────────────────────────────────

async function main() {
  if (hasFlag('--check')) return runCheck();
  const want = flagValue('--ticker', '').toUpperCase();
  const tickers = want ? want.split(',').map((s) => s.trim()).filter(Boolean) : TICKERS;
  const from = flagValue('--from', null);
  const section = flagValue('--section', 'all');
  const csvDir = flagValue('--csv', null);

  process.stdout.write(`Fetching full daily history for ${tickers.join(' ')}…\n`);
  const data = [];
  for (const sym of tickers) {
    try {
      let bars = await fetchDaily(sym);
      if (from) bars = bars.filter((b) => b.date >= from);
      if (bars.length < 60) throw new Error(`only ${bars.length} bars`);
      data.push({
        sym, bars,
        s: summarize(sym, bars),
        m: monthlyReturns(bars),
        y: yearlyReturns(bars),
        seas: seasonality(monthlyReturns(bars)),
      });
      process.stdout.write(`  ${sym}: ${bars.length} bars from ${bars[0].date}\n`);
    } catch (err) {
      console.error(`  ${sym}: FAILED — ${err.message}`);
    }
  }
  if (!data.length) {
    console.error('No data fetched. Yahoo may be throttling; wait a minute and retry.');
    process.exit(1);
  }

  const show = (name) => section === 'all' || section === name;
  if (show('summary')) printSummary(data);
  if (show('years')) printYears(data);
  if (show('months')) printMonths(data);
  if (show('seasonality')) printSeasonality(data);
  if (show('eras')) printEras(data);
  if (show('corr')) printCorr(data);
  if (csvDir) {
    console.log(paint('\nCSV', C.bold));
    writeCsvs(csvDir, data);
  }
  console.log();
}

main().catch((err) => {
  console.error(`backtest error: ${err.message}`);
  process.exit(1);
});
