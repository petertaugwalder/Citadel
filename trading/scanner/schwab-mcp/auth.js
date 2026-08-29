#!/usr/bin/env node
/**
 * One-time Schwab OAuth helper — mints the refresh token the MCP server needs.
 *
 * Schwab's refresh tokens last about seven days, so expect to re-run this. It
 * prints the token to your terminal and writes nothing to disk; put the value in
 * your shell profile or password manager, never in this repo.
 *
 *   export SCHWAB_APP_KEY=...
 *   export SCHWAB_APP_SECRET=...
 *   export SCHWAB_CALLBACK_URL=https://127.0.0.1        (must match the app exactly)
 *   node auth.js
 */
import readline from 'node:readline/promises';
import { stdin, stdout } from 'node:process';

const TOKEN_URL = 'https://api.schwabapi.com/v1/oauth/token';
const AUTH_URL = 'https://api.schwabapi.com/v1/oauth/authorize';

const key = process.env.SCHWAB_APP_KEY;
const secret = process.env.SCHWAB_APP_SECRET;
const callback = process.env.SCHWAB_CALLBACK_URL || 'https://127.0.0.1';

if (!key || !secret) {
  console.error('Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET first (from developer.schwab.com).');
  process.exit(1);
}

const authorize = `${AUTH_URL}?client_id=${encodeURIComponent(key)}` +
  `&redirect_uri=${encodeURIComponent(callback)}&response_type=code`;

console.log('\n1. Open this URL and log in to Schwab:\n');
console.log(`   ${authorize}\n`);
console.log('2. Approve access. The browser will land on your callback URL — it may show a');
console.log('   connection error, which is fine; the part that matters is in the address bar.\n');

const rl = readline.createInterface({ input: stdin, output: stdout });
const pasted = (await rl.question('3. Paste the FULL redirect URL here: ')).trim();
rl.close();

let code;
try {
  code = new URL(pasted).searchParams.get('code');
} catch {
  console.error('\nThat did not parse as a URL. Paste the whole thing, starting with https://');
  process.exit(1);
}
if (!code) {
  console.error('\nNo "code" parameter in that URL. Make sure you copied the address after the redirect.');
  process.exit(1);
}

const res = await fetch(TOKEN_URL, {
  method: 'POST',
  headers: {
    Authorization: `Basic ${Buffer.from(`${key}:${secret}`).toString('base64')}`,
    'Content-Type': 'application/x-www-form-urlencoded',
  },
  body: new URLSearchParams({
    grant_type: 'authorization_code', code, redirect_uri: callback,
  }),
});

if (!res.ok) {
  const detail = await res.text().catch(() => '');
  console.error(`\nToken exchange failed: HTTP ${res.status}`);
  if (res.status === 400) {
    console.error('Usually the callback URL does not exactly match the one on your Schwab app,');
    console.error('or the code was already used — authorization codes are single-use and short-lived.');
  }
  if (detail) console.error(detail.slice(0, 400));
  process.exit(1);
}

const body = await res.json();
if (!body.refresh_token) {
  console.error('\nNo refresh_token in the response.');
  process.exit(1);
}

console.log('\nSuccess. Add this to your shell profile:\n');
console.log(`   export SCHWAB_REFRESH_TOKEN='${body.refresh_token}'\n`);
console.log(`Access token expires in ${body.expires_in ?? 1800}s; the server refreshes it as needed.`);
console.log('Treat the refresh token like a password — do not commit it or paste it into a chat.');
