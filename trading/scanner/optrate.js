#!/usr/bin/env node
/**
 * Option-chain traffic-light rater for call buyers.
 *
 * Reads CBOE-format chain exports (the CSV you get from a broker's "download
 * quotedata"), rates every call on all five greeks plus liquidity, and picks the
 * contracts that pass. Chain exports carry IV, Delta and Gamma but not Theta,
 * Vega or Rho, so those three are computed with Black-Scholes using the chain's
 * own implied vol, with the risk-free rate calibrated so the model reproduces
 * the chain's published deltas. The fit error is printed — check it before
 * trusting the computed greeks.
 *
 *   node optrate.js chain1.csv chain2.csv ...   rate every call
 *   node optrate.js --all chain.csv             include contracts that fail
 *   node optrate.js --spot 27.20 chain.csv      override the underlying price
 *   node optrate.js --check                     offline self-test, exit 0/1
 *
 * Ratings are for BUYING calls: green delta is real participation without
 * paying for stock, green gamma is convexity relative to the best in the chain,
 * and green theta/vega/rho mean small exposure per dollar of premium.
 */
'use strict';

const fs = require('node:fs');

// ── rubric ──────────────────────────────────────────────────────────────────
const RUBRIC = {
  delta:  { green: [0.45, 0.75], yellow: [0.30, 0.85] },   // inside range
  gamma:  { green: 0.70, yellow: 0.40, higherBetter: true }, // share of chain best
  theta:  { green: 0.20, yellow: 0.32 },   // % of premium per day
  vega:   { green: 3.5, yellow: 5.5 },     // % of premium per vol point
  rho:    { green: 2.5, yellow: 5.0 },     // % of premium per 1pp of rates
  spread: { green: 6, yellow: 15 },        // % of mid
  oi:     { green: 500, yellow: 100, higherBetter: true },
};
const GREEKS = ['delta', 'gamma', 'theta', 'vega', 'rho'];

// ── Black-Scholes ───────────────────────────────────────────────────────────
const pdf = (x) => Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
function cdf(x) {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const p = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
            t * (-1.821255978 + t * 1.330274429))));
  const v = 1 - pdf(x) * p;
  return x >= 0 ? v : 1 - v;
}
function bsCall(S, K, T, r, s) {
  if (!(T > 0) || !(s > 0) || !(S > 0) || !(K > 0)) return null;
  const sq = Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r + s * s / 2) * T) / (s * sq);
  const d2 = d1 - s * sq;
  const disc = Math.exp(-r * T);
  return {
    delta: cdf(d1),
    gamma: pdf(d1) / (S * s * sq),
    theta: (-(S * pdf(d1) * s) / (2 * sq) - r * K * disc * cdf(d2)) / 365,
    vega: (S * pdf(d1) * sq) / 100,
    rho: (K * T * disc * cdf(d2)) / 100,
  };
}

// ── parsing ─────────────────────────────────────────────────────────────────
// OCC symbol is ROOT + YYMMDD + C/P + strike(8). An adjusted series appends a
// digit to the root (WEAT1...), which is a different deliverable — never mix
// those with standard contracts.
function splitOcc(sym) {
  const body = sym.replace(/[CP]\d{8}$/, '');
  return { root: body.slice(0, -6), yymmdd: body.slice(-6) };
}

function parseChain(text, asOf) {
  const lines = text.split(/\r?\n/);
  let spot = null, ticker = null;
  for (const l of lines.slice(0, 4)) {
    const m = l.match(/Last:\s*([\d.]+)/);
    if (m) { spot = Number(m[1]); ticker = l.split(',')[0].trim(); }
  }
  const rows = [];
  for (const line of lines) {
    const c = line.split(',');
    if (c.length < 12) continue;
    const sym = (c[1] ?? '').trim();
    // Root may carry a digit on an adjusted series (SOYB1...), so allow more
    // than six digits here and let splitOcc take the last six as the expiry.
    if (!/^[A-Z]{1,6}\d{6,}[CP]\d{8}$/.test(sym)) continue;
    const exp = new Date(c[0].replace(/^\w{3} /, '') + ' UTC');
    if (Number.isNaN(exp.getTime())) continue;
    const { root } = splitOcc(sym);
    rows.push({
      sym, root, exp: exp.toISOString().slice(0, 10),
      dte: Math.round((exp - asOf) / 864e5),
      bid: Number(c[4]), ask: Number(c[5]), volume: Number(c[6]),
      iv: Number(c[7]), delta: Number(c[8]), gamma: Number(c[9]),
      oi: Number(c[10]), strike: Number(c[11]),
    });
  }
  return { ticker, spot, rows };
}

// Pick the rate that best reproduces the chain's own deltas — that both fits the
// curve and proves the model is consistent with the data before it is trusted
// for theta, vega and rho.
function calibrateRate(rows, S) {
  const fit = rows.filter((o) => o.iv > 0.1 && o.iv < 0.6 &&
    o.strike > S * 0.7 && o.strike < S * 1.4 && Number.isFinite(o.delta));
  if (fit.length < 5) return { r: 0.04, rms: null, n: fit.length };
  let best = { r: 0.04, err: Infinity };
  for (let r = 0; r <= 0.08; r += 0.0025) {
    let err = 0, n = 0;
    for (const o of fit) {
      const g = bsCall(S, o.strike, o.dte / 365, r, o.iv);
      if (!g) continue;
      err += (g.delta - o.delta) ** 2; n++;
    }
    if (n && err / n < best.err) best = { r, err: err / n };
  }
  return { r: best.r, rms: Math.sqrt(best.err), n: fit.length };
}

// ── rating ──────────────────────────────────────────────────────────────────
function lightFor(metric, value, spec) {
  if (!Number.isFinite(value)) return 'R';
  if (metric === 'delta') {
    const [gl, gh] = spec.green, [yl, yh] = spec.yellow;
    if (value >= gl && value <= gh) return 'G';
    return value >= yl && value <= yh ? 'Y' : 'R';
  }
  if (spec.higherBetter) return value >= spec.green ? 'G' : value >= spec.yellow ? 'Y' : 'R';
  return value <= spec.green ? 'G' : value <= spec.yellow ? 'Y' : 'R';
}

function rateChain(rows, S, r, opts = {}) {
  const standardRoot = opts.root ?? mode(rows.map((x) => x.root));
  const usable = rows.filter((o) => o.ask > 0 && o.iv > 0 && o.root === standardRoot);
  const gammaBest = Math.max(...usable.map((o) => o.gamma).filter(Number.isFinite), 0);
  const out = [];
  for (const o of usable) {
    const g = bsCall(S, o.strike, o.dte / 365, r, o.iv);
    if (!g) continue;
    const mid = (o.bid + o.ask) / 2;
    const m = {
      delta: o.delta,
      gamma: gammaBest > 0 ? o.gamma / gammaBest : 0,
      theta: Math.abs(g.theta) / o.ask * 100,
      vega: g.vega / o.ask * 100,
      rho: g.rho / o.ask * 100,
      spread: mid > 0 ? ((o.ask - o.bid) / mid) * 100 : Infinity,
      oi: o.oi,
    };
    const rate = {};
    for (const k of Object.keys(RUBRIC)) rate[k] = lightFor(k, m[k], RUBRIC[k]);
    const intrinsic = Math.max(0, S - o.strike);
    out.push({
      ...o, ...g, mid, metrics: m, rate,
      greens: GREEKS.filter((k) => rate[k] === 'G').length,
      reds: Object.values(rate).filter((v) => v === 'R').length,
      redOn: Object.entries(rate).filter(([, v]) => v === 'R').map(([k]) => k),
      yellowGreeks: GREEKS.filter((k) => rate[k] === 'Y'),
      intrinsic, extrinsic: o.ask - intrinsic,
      breakeven: o.strike + o.ask,
      moveNeeded: ((o.strike + o.ask) / S - 1) * 100,
      leverage: (o.delta * S) / o.ask,
      ivCrush5: (-g.vega * 5 / o.ask) * 100,
      ret: (t) => ((Math.max(0, t - o.strike) - o.ask) / o.ask) * 100,
    });
  }
  return out.sort((a, b) => b.greens - a.greens || a.reds - b.reds || a.ask - b.ask);
}

function mode(arr) {
  const counts = new Map();
  for (const x of arr) counts.set(x, (counts.get(x) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
}

// ── output ──────────────────────────────────────────────────────────────────
const NC = process.argv.includes('--no-color') || !!process.env.NO_COLOR;
const L = NC ? { G: ' G ', Y: ' Y ', R: ' R ' }
             : { G: '\x1b[42;30m G \x1b[0m', Y: '\x1b[43;30m Y \x1b[0m', R: '\x1b[41;97m R \x1b[0m' };
const lp = (s, w) => String(s).padStart(w);
const rp = (s, w) => String(s).padEnd(w);
const B = NC ? '' : '\x1b[1m', D = NC ? '' : '\x1b[2m', X = NC ? '' : '\x1b[0m';

function report(ticker, S, cal, cands, showAll) {
  console.log(`\n${B}${ticker} — ${S.toFixed(2)}${X}   r=${(cal.r * 100).toFixed(2)}% calibrated to the chain's own deltas` +
    (cal.rms != null ? ` (RMS error ${cal.rms.toFixed(4)} over ${cal.n} contracts)` : ''));
  if (cal.rms != null && cal.rms > 0.05) {
    console.log(`${D}  warning: fit error is high — the computed theta/vega/rho are less reliable${X}`);
  }
  const shown = showAll ? cands : cands.filter((c) => c.reds === 0);
  console.log(`\n${B}${showAll ? 'ALL CALLS' : 'NO RED ANYWHERE'}${X}  (${shown.length} of ${cands.length} contracts)`);
  console.log(D + rp('expiry', 12) + lp('K', 5) + lp('ask', 7) + lp('Δ', 7) + lp('Γ%', 6) +
    lp('Θ%/d', 7) + lp('V%', 6) + lp('ρ%', 6) + lp('sprd', 7) + lp('OI', 7) +
    '  Δ  Γ  Θ  V  ρ  sp OI  gk  fails' + X);
  for (const c of shown.slice(0, 25)) {
    console.log(rp(c.exp, 12) + lp(c.strike.toFixed(0), 5) + lp(c.ask.toFixed(2), 7) +
      lp(c.metrics.delta.toFixed(3), 7) + lp((c.metrics.gamma * 100).toFixed(0) + '%', 6) +
      lp(c.metrics.theta.toFixed(3), 7) + lp(c.metrics.vega.toFixed(2), 6) +
      lp(c.metrics.rho.toFixed(2), 6) + lp(c.metrics.spread.toFixed(1) + '%', 7) +
      lp(c.oi, 7) + '  ' + L[c.rate.delta] + L[c.rate.gamma] + L[c.rate.theta] +
      L[c.rate.vega] + L[c.rate.rho] + L[c.rate.spread] + L[c.rate.oi] +
      lp(c.greens, 4) + '  ' + (c.redOn.join(',') || '-'));
  }
  if (!shown.length) console.log('  (none)');

  const five = cands.filter((c) => c.reds === 0 && c.greens === 5);
  console.log(`\n${B}VERDICT${X}`);
  console.log(five.length
    ? `  5/5 green greeks, no red: ${five.map((c) => `${c.exp} $${c.strike} @ ${c.ask.toFixed(2)}`).join(' | ')}`
    : '  No contract has 5 green greeks with no red.');
  const rhoOnly = cands.filter((c) => c.reds === 0 && c.greens === 4 && c.yellowGreeks[0] === 'rho');
  if (!five.length) {
    console.log(rhoOnly.length
      ? `  Fallback (only rho yellow): ${rhoOnly.map((c) => `${c.exp} $${c.strike} @ ${c.ask.toFixed(2)}`).join(' | ')}`
      : '  No contract qualifies under the rho-yellow fallback either.');
    for (const c of cands.filter((x) => x.reds === 0 && x.greens === 4)) {
      console.log(`${D}    near miss: ${c.exp} $${c.strike} — yellow on ${c.yellowGreeks.join(', ')}${X}`);
    }
  }

  const clean = cands.filter((c) => c.reds === 0);
  if (clean.length) {
    console.log(`\n${B}COST / UPSIDE${X}  (return on the ask, at expiry)`);
    const t = [1.1, 1.2, 1.35, 1.5].map((k) => S * k);
    console.log(D + rp('expiry', 12) + lp('K', 5) + lp('ask', 7) + lp('lot', 7) + lp('extr', 7) +
      lp('B/E', 8) + lp('need', 7) + lp('lev', 6) + lp('IV', 7) + lp('-5vol', 7) +
      t.map((x) => lp('@' + x.toFixed(0), 7)).join('') + X);
    for (const c of clean.slice(0, 12)) {
      console.log(rp(c.exp, 12) + lp(c.strike.toFixed(0), 5) + lp(c.ask.toFixed(2), 7) +
        lp('$' + (c.ask * 100).toFixed(0), 7) + lp(c.extrinsic.toFixed(2), 7) +
        lp(c.breakeven.toFixed(2), 8) + lp('+' + c.moveNeeded.toFixed(1) + '%', 7) +
        lp(c.leverage.toFixed(1) + 'x', 6) + lp((c.iv * 100).toFixed(1) + '%', 7) +
        lp(c.ivCrush5.toFixed(0) + '%', 7) +
        t.map((x) => lp(c.ret(x).toFixed(0) + '%', 7)).join(''));
    }
  }
}

// ── self-test ───────────────────────────────────────────────────────────────
function runCheck() {
  let pass = 0, fail = 0;
  const t = (n, c) => { if (c) { pass++; console.log(`  ok   ${n}`); } else { fail++; console.log(`  FAIL ${n}`); } };
  const near = (a, b, e = 1e-4) => Number.isFinite(a) && Math.abs(a - b) < e;

  console.log('occ symbols');
  t('standard root', splitOcc('WEAT270115C00028000').root === 'WEAT');
  t('adjusted root keeps its digit', splitOcc('WEAT1270115C00005000').root === 'WEAT1');
  t('root not confused by expiry digits', splitOcc('CORN261120C00020000').root === 'CORN');
  t('expiry extracted', splitOcc('CORN261120C00020000').yymmdd === '261120');

  console.log('black-scholes');
  // S=K=100, T=1, r=0, sigma=20%: d1 = 0.1, so delta = N(0.1) = 0.5398 and
  // vega = S*phi(0.1)*sqrt(T)/100 = 0.39695 per vol point.
  const g = bsCall(100, 100, 1, 0, 0.2);
  t('atm delta', near(g.delta, 0.5398, 5e-4));
  t('atm vega per point', near(g.vega, 0.39695, 5e-4));
  t('theta is negative for a long call', g.theta < 0);
  t('rho is positive for a call', g.rho > 0);
  t('deep itm delta approaches 1', bsCall(100, 10, 1, 0, 0.2).delta > 0.999);
  t('far otm delta approaches 0', bsCall(100, 500, 1, 0, 0.2).delta < 0.001);
  t('rejects nonsense inputs', bsCall(100, 100, -1, 0, 0.2) === null && bsCall(100, 100, 1, 0, 0) === null);

  console.log('parsing');
  const csv = ['', 'Test Fund,Last: 27.2,Change: 0.44',
    '"Date: August 29, 2026 at 3:19 PM EDT",Bid: 27.1,Ask: 27.3',
    'Expiration Date,Calls,Last Sale,Net,Bid,Ask,Volume,IV,Delta,Gamma,Open Interest,Strike,Puts',
    'Fri Nov 20 2026,SOYB261120C00026000,1.7,0.25,1.15,1.95,178,0.1261,0.7402,0.1382,868,26.00,SOYB261120P00026000',
    'Fri Nov 20 2026,SOYB1261120C00005000,0.5,0,0.4,0.6,1,0.3,0.5,0.2,10,5.00,SOYB1261120P00005000',
    'garbage,,,,,,,,,,,,'].join('\n');
  const p = parseChain(csv, new Date('2026-08-29T00:00:00Z'));
  t('spot from header', p.spot === 27.2);
  t('two contracts parsed, garbage skipped', p.rows.length === 2);
  t('fields mapped', p.rows[0].strike === 26 && p.rows[0].oi === 868 && near(p.rows[0].delta, 0.7402));
  t('dte computed', p.rows[0].dte === 83);
  t('adjusted series distinguished', p.rows[1].root === 'SOYB1' && p.rows[0].root === 'SOYB');

  console.log('rating');
  const rated = rateChain(p.rows, 27.2, 0.04);
  t('adjusted series excluded from rating', rated.length === 1 && rated[0].root === 'SOYB');
  t('delta 0.74 rates green', rated[0].rate.delta === 'G');
  t('sole contract is its own gamma benchmark', rated[0].rate.gamma === 'G');
  t('spread 41% rates red', rated[0].rate.spread === 'R');
  t('oi 868 rates green', rated[0].rate.oi === 'G');
  t('breakeven', near(rated[0].breakeven, 27.95));
  t('return at target', near(rated[0].ret(30), ((4 - 1.95) / 1.95) * 100, 1e-6));

  console.log('lights');
  t('delta band', lightFor('delta', 0.60, RUBRIC.delta) === 'G' &&
    lightFor('delta', 0.80, RUBRIC.delta) === 'Y' && lightFor('delta', 0.95, RUBRIC.delta) === 'R');
  t('lower-is-better', lightFor('theta', 0.1, RUBRIC.theta) === 'G' &&
    lightFor('theta', 0.25, RUBRIC.theta) === 'Y' && lightFor('theta', 0.9, RUBRIC.theta) === 'R');
  t('higher-is-better', lightFor('oi', 900, RUBRIC.oi) === 'G' &&
    lightFor('oi', 200, RUBRIC.oi) === 'Y' && lightFor('oi', 5, RUBRIC.oi) === 'R');
  t('non-finite is red', lightFor('vega', NaN, RUBRIC.vega) === 'R');

  console.log('calibration');
  const synth = [];
  for (const K of [24, 26, 28, 30, 32]) {
    const truth = bsCall(28, K, 0.5, 0.045, 0.3);
    synth.push({ strike: K, dte: 182, iv: 0.3, delta: truth.delta, gamma: truth.gamma });
  }
  const cal = calibrateRate(synth, 28);
  t('recovers the rate used to build the data', Math.abs(cal.r - 0.045) <= 0.0025);
  t('fit error is tiny on clean data', cal.rms < 1e-3);
  t('falls back when there is too little to fit', calibrateRate([], 28).r === 0.04);

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

// ── main ────────────────────────────────────────────────────────────────────
function main() {
  const argv = process.argv.slice(2);
  if (argv.includes('--check')) return runCheck();
  const files = argv.filter((a) => !a.startsWith('--') &&
    argv[argv.indexOf(a) - 1] !== '--spot');
  if (!files.length) {
    console.error('usage: node optrate.js [--all] [--spot N] chain1.csv [chain2.csv ...]');
    process.exit(1);
  }
  const spotIdx = argv.indexOf('--spot');
  const spotOverride = spotIdx >= 0 ? Number(argv[spotIdx + 1]) : null;

  let ticker = null, spot = null;
  const rows = [];
  const asOf = new Date(new Date().toISOString().slice(0, 10) + 'T00:00:00Z');
  for (const f of files) {
    const p = parseChain(fs.readFileSync(f, 'utf8'), asOf);
    ticker ??= p.ticker;
    spot ??= p.spot;
    rows.push(...p.rows);
  }
  const S = spotOverride ?? spot;
  if (!(S > 0)) { console.error('Could not determine the underlying price; pass --spot'); process.exit(1); }
  const cal = calibrateRate(rows, S);
  report(ticker ?? 'chain', S, cal, rateChain(rows, S, cal.r), argv.includes('--all'));
  console.log();
}

main();
