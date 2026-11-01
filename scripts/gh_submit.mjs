#!/usr/bin/env node
// gh_submit.mjs — fill and submit ONE Greenhouse application with no model in the loop.
//
//   node scripts/gh_submit.mjs --profile data/gh_profile.json --resume data/resume_security.pdf \
//        --url <greenhouse url> [--extra '{"question_123":"Yes"}'] [--dry]
//
// Prints one JSON line: {state, url, company, pending, log}
//   SUBMITTED   confirmation page reached (security-code email handled automatically)
//   NEED_HUMAN  required questions the rule tables can't answer — `pending` lists them;
//               rerun with --extra once answered, or hand the job to the wave brain
//   BLOCKED     captcha / human-check / code never arrived — never fought, never bypassed
//   CLOSED      no application form (posting closed or moved)
//   ERRORS      submit refused with validation errors (in `pending`)
//   FILLED      --dry run, nothing submitted
//
// Answers come from the profile JSON (built from profile.yaml by gh_runner.py) plus the
// RULES/TEXT tables below. Every answer here is true of the candidate; when a question is
// not covered, the job goes to NEED_HUMAN rather than getting a guess.
import { createRequire } from 'module';
import { readFileSync, existsSync } from 'fs';
import { execFileSync } from 'child_process';
const require = createRequire('/opt/auto-applier/node_modules/');
const { chromium } = require('playwright');

const argv = process.argv.slice(2);
const arg = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const URL_ = arg('--url'); const DRY = argv.includes('--dry');
const P = JSON.parse(readFileSync(arg('--profile', '/opt/auto-applier/data/gh_profile.json'), 'utf8'));
const RESUME = arg('--resume', '/opt/auto-applier/data/resume_security.pdf');
const EXTRA = { ...(P.extra || {}), ...JSON.parse(arg('--extra', '{}')) };
const out = (o) => { console.log(JSON.stringify(o)); };

function proxyUp() {
  try { const ip = execFileSync('curl', ['-s', '--max-time', '8', '--socks5-hostname', '127.0.0.1:1080', 'https://api.ipify.org']).toString(); return /^\d+\.\d+\.\d+\.\d+$/.test(ip); } catch { return false; }
}

async function fetchCode(company, since) {
  const { ImapFlow } = require('imapflow');
  const env = {};
  for (const l of readFileSync('/opt/auto-applier/.env', 'utf8').split('\n')) { const m = l.match(/^([A-Z_][A-Z0-9_]*)=(.+)$/); if (m) env[m[1]] = m[2].trim(); }
  const co = company.toLowerCase();
  for (let i = 0; i < 36; i++) {          // ~3 min of polling for this one email, then BLOCKED
    await new Promise(r => setTimeout(r, 5000));
    const c = new ImapFlow({ host: env.IMAP_HOST, port: 993, secure: true, auth: { user: env.IMAP_USER, pass: env.IMAP_PASS }, logger: false });
    let code = null;
    try {
      await c.connect(); const lock = await c.getMailboxLock('INBOX');
      try {
        const uids = await c.search({ from: 'greenhouse-mail.io', since: new Date(Date.now() - 86400000) });
        for await (const m of c.fetch(uids.slice(-20), { envelope: true, source: true })) {
          if (!m.envelope.subject.toLowerCase().includes(co) || new Date(m.envelope.date) < since) continue;
          const h = m.source.toString().replace(/=\r?\n/g, '').replace(/=3D/g, '=').replace(/<[^>]+>/g, ' ');
          const k = h.match(/application:?\s+([A-Za-z0-9]{8})\b/) || h.match(/\b([A-Za-z0-9]{8})\b\s+After you enter/i);
          if (k) code = k[1];
        }
      } finally { lock.release(); await c.logout(); }
    } catch { /* transient IMAP error: poll again */ }
    if (code) return code;
  }
  return null;
}

const CHROME = ['/home/claude/.cache/ms-playwright/chromium-1252/chrome-linux64/chrome'].find(existsSync);
// same fingerprint as the MCP browser (browser_launch.sh): headed under xvfb-run, desktop UA, stealth init,
// persistent profile — headless chromium scores low on Greenhouse's invisible reCAPTCHA
const ctx = await chromium.launchPersistentContext('/opt/auto-applier/data/gh_browser_profile', {
  headless: !process.env.DISPLAY, args: ['--no-sandbox'], ...(CHROME ? { executablePath: CHROME } : {}),
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  viewport: { width: 1366, height: 768 },
  ...(proxyUp() ? { proxy: { server: 'socks5://127.0.0.1:1080' } } : {}),
});
await ctx.addInitScript({ path: '/opt/auto-applier/stealth_init.js' });
const browser = ctx;
const page = ctx.pages()[0] || await ctx.newPage();
page.setDefaultTimeout(8000);  // per click/fill: an unclickable field should fail over to NEED_HUMAN, not stall
try {
  await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 60000 }); await page.waitForTimeout(2500);
  let f = page.mainFrame();
  const findForm = async () => { for (const fr of page.frames()) if (await fr.$('#first_name')) return fr; return null; };
  f = await findForm();
  if (!f) { const b = page.getByRole('button', { name: /apply/i }).first(); if (await b.count()) { await b.click().catch(() => {}); await page.waitForTimeout(1500); } f = await findForm(); }
  const jid = (URL_.match(/gh_jid=(\d+)/) || [])[1];
  if (!f && jid) {  // company-hosted page that didn't render the embed: open the hosted Greenhouse form directly
    const html = await page.content();
    const slugs = [(html.match(/[?&]for=([\w-]+)/) || [])[1], (html.match(/greenhouse\.io\/([\w-]+)\/jobs/) || [])[1],
      arg('--company', '').toLowerCase().replace(/[^a-z0-9]/g, '')].filter(Boolean);
    for (const slug of [...new Set(slugs)]) {
      await page.goto(`https://job-boards.greenhouse.io/embed/job_app?for=${slug}&token=${jid}`, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
      await page.waitForTimeout(2000); f = await findForm(); if (f) break;
    }
  }
  if (!f) { out({ state: 'CLOSED', url: page.url() }); process.exit(0); }
  const title = await page.title();
  const company = (title.match(/ at (.+?)\s*$/) || title.match(/ [-|] (.+?)\s*$/) || [])[1] || arg('--company', '');

  const log = [];
  const opts = async () => (await f.locator('.select__menu [role=option]:visible').allInnerTexts()).map(s => s.trim());
  const clickOpt = async (hit) => f.locator('.select__menu [role=option]:visible', { hasText: hit }).first().click({ timeout: 3000 });
  const pick = async (inp, prefs) => {
    await inp.click(); await page.waitForTimeout(400);
    let o = await opts();
    if (!o.length) { await inp.press('ArrowDown').catch(() => {}); await page.waitForTimeout(300); o = await opts(); }
    for (const p of prefs) {
      const re = p instanceof RegExp ? p : new RegExp('^' + String(p).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
      const hit = o.find(t => re.test(t));
      if (hit) { await clickOpt(hit); return hit; }
    }
    await page.keyboard.press('Escape');
    return null;
  };
  const typeIn = async (inp, v) => { await inp.click(); await inp.fill(''); await inp.type(String(v), { delay: 1 }); await inp.evaluate(e => e.blur()); };
  const typeahead = async (sel, v, prefs) => {
    const inp = await f.$(sel); if (!inp) return null;
    if (await inp.evaluate(e => e.closest('.select__control')?.querySelector('.select__single-value')?.innerText || '')) return null;
    await inp.click(); await inp.fill(''); await inp.type(v, { delay: 30 }); await page.waitForTimeout(1500);
    const o = await opts(); const hit = o.find(t => prefs.some(r => r.test(t))) || o[0];
    if (hit) await clickOpt(hit).catch(() => {});
    return hit;
  };

  // standard fields
  for (const [sel, v] of [['#first_name', P.first], ['#last_name', P.last], ['#email', P.email], ['#phone', P.phone], ['#preferred_name', P.preferred]]) {
    const e = await f.$(sel); if (e && v && (await e.isVisible()) && !(await e.inputValue())) await typeIn(e, v).catch(x => log.push(`!${sel} ${String(x).slice(0, 60)}`));
  }
  const up = await f.$('input[type=file]#resume') || await f.$('input[type=file]');
  if (up) await up.setInputFiles(RESUME);
  await typeahead('#candidate-location', P.city, [new RegExp(`${P.city}, ${P.state_long}`, 'i')]).catch(x => log.push(`!location ${String(x).slice(0, 60)}`));
  await typeahead('#school--0', P.school, [new RegExp(`^${P.school}$`, 'i')]).catch(x => log.push(`!school ${String(x).slice(0, 60)}`));

  const NO = [/^no/i], YES = [/^yes/i], DECLINE = [/decline|wish|prefer not/i];
  const SKILLS = (P.skills || []).map(s => [s.split(/[ (]/)[0].toLowerCase(), /\(basic\)/i.test(s)]);
  const toolAnswer = (t) => {   // 'experience with X?' -> Yes only if X is a listed skill, and not when asked for strong/expert use of a basic one
    const hit = SKILLS.find(([k]) => k.length > 2 && t.toLowerCase().includes(k));
    return hit && !(hit[1] && /strong|expert|advanced|extensive|deep/i.test(t)) ? YES : NO;
  };
  const RULES = [
    [/^degree\b/i, [/^master'?s/i, /master of science/i, /^m\.?s\.?\b/i]],
    [/^discipline/i, [/cyber/i, /information security/i, /information (systems|technology)/i, /other/i]],
    [/(live|reside|located) in one of the following states|following states/i, (t) => new RegExp(`\\b${P.state_long}\\b`, 'i').test(t) ? YES : NO],
    [/^(have you|do you have).*\b(personally )?(led|lead|managed|owned|directed|supervised|built and ran)\b/i, NO],  // leadership history: he has none yet
    [/(experience|familiar|proficien)\w* (with|in|using|supporting)\b/i, toolAnswer],
    [/managed service provider|\bmsp\b/i, NO],
    [/sponsor/i, NO],
    [/reside in the bay area|currently (live|reside) in (the )?(bay|san francisco|sf|new york|nyc|austin|chicago)/i, NO],
    [/^location\s*\*?$/i, [/^california/i, /san francisco/i, /^remote/i, /^other/i]],
    [/relatives?|family member/i, NO],
    [/alaska|hawaii|puerto rico/i, NO],
    [/ever been employed|have you (ever )?worked (at|for)|worked for .* before|been employed (by|at)/i, NO],
    [/notice period/i, [/2 weeks|two weeks/i, /immediately/i]],
    [/\bai\b|artificial intelligence/i, [/^moderately/i, /occasionally/i]],
    [/think of yourself as|identify as/i, DECLINE],
    [/non-?compete|restrictive covenant/i, NO],
    [/u\.s\. person|export.control|u\.s\. citizen/i, YES],
    [/meet this requirement/i, NO],
    [/government (employee|official)|procurement|contract award/i, NO],
    [/referr/i, NO],
    [/authori[sz](ed|ation)|legally (eligible|able|permitted)|right to work|eligible to work/i, YES],
    [/based in the (united states|us)|reside in the (us|united states)|located in the (us|united states)|currently (live|reside) in the u/i, YES],
    [/relocat/i, YES],
    [/(hybrid|in[- ]office|on[- ]?site|commute|days a week|days per week)/i, YES],
    [/previously (been )?(employed|worked)|ever worked (for|at)|former employee|current(ly)? employ(ed|ee) (by|of|at)/i, NO],
    [/how did you (hear|learn|find)|source/i, [/linkedin/i, /job board/i, /other/i]],
    [/(keep|retain).*(details|information|data|on file)|future (job )?opportunit|talent (pool|community)/i, YES],
    [/privacy|consent|acknowledge|agree|certify|i understand|attest/i, [/^(yes|i agree|i acknowledge|agree|acknowledge|i consent|i have read|i certify|i understand)/i, /^yes/i]],
    [/18 years|at least 18|over the age/i, YES],
    [/clearance/i, [/secret/i, /in progress|pending|adjudicat/i, /no/i]],
    [/time ?zone/i, [/eastern|^est|^et\b|edt/i]],
    [/gender/i, DECLINE], [/hispanic|latino/i, DECLINE], [/race|ethnic/i, DECLINE], [/veteran/i, DECLINE],
    [/disabilit/i, DECLINE], [/sexual orientation|transgender|pronoun/i, DECLINE],
    [/state.*(reside|live|located)|which state|state of residence|^state\s*\*?$/i, [new RegExp(P.state_long, 'i')]],
    [/country/i, [/united states/i, /^usa$/i, /^us$/i, /^u\.s\./i]],
    [/background (check|screen)|drug (test|screen)/i, YES],
    [/years of (relevant |professional )?experience|how many years/i, [/^\W*(0|1|2)\b|0.2|1.2|1-3|less than/i]],
    [/(highest|level of) (level of )?education|degree/i, [/bachelor/i]],
    [/text message|sms/i, NO],
    [/accommodation/i, NO],
    [/start date month/i, [P.job_start_month]], [/end date month/i, [P.job_end_month]],
    [/comfortable|willing to|are you able|able to (provide|work|commit|travel)|can you (provide|work|commit)|onboarding|i-9|e-verify/i, YES],
  ];
  const TEXT = [
    [/confirm you are human|type the word|captcha/i, 'HUMAN_CHECK'],
    [/linkedin/i, P.linkedin], [/github/i, P.github], [/website|portfolio|personal site/i, P.website],
    [/^company name/i, P.job_company], [/^title\s*\*?$/i, P.job_title],
    [/start date year/i, P.job_start_year], [/end date year/i, P.job_end_year],
    [/graduation (year|date)/i, null],
    [/start date|earliest.*start|when can you start|availability to start|notice period/i, 'Two weeks after an offer'],
    [/salary|compensation|pay expectation/i, 'Open; in line with the posted range for the role'],
    [/^(current|most recent) (company|employer)/i, P.current_company],
    [/(current|most recent) (job )?title|title held/i, P.current_title],
    [/not applicable|n\/a|^if (yes|so)\b/i, 'Not applicable'],
    [/managed service provider|\bmsp\b/i, 'No.'],
    [/are you (local to|located in|based in)|do you (currently )?live in/i, `No. I'm based in ${P.city}, ${P.state} and open to relocating.`], [/referral|referred|who referred|student organi/i, null], [/^(school|university|college)/i, P.school],
    [/primary residence|city and state|city, state|current location/i, `${P.city}, ${P.state}`],
    [/address line 2|apt|suite|unit/i, null], [/^city/i, P.city], [/^state/i, P.state_long], [/zip|postal/i, P.zip], [/address/i, P.address_full],
    [/pronoun/i, P.pronouns || null],
    [/(full|legal) name|signature/i, P.name],
    [/clearance/i, P.clearance],
    [/years of (relevant |professional )?experience|how many years/i, '2'],
    [/how did you (hear|learn|find)/i, 'LinkedIn'],
  ];

  let pending = [];
  for (let pass = 0; pass < 2; pass++) {  // second pass retries whatever a flaky menu left unanswered
  pending = [];
  for (const l of await f.$$('label')) {
    const t = (await l.innerText()).trim().replace(/\s+/g, ' ');
    const id = await l.getAttribute('for');
    if (!id || ['first_name', 'last_name', 'email', 'phone', 'preferred_name', 'resume', 'cover_letter', 'candidate-location', 'school--0'].includes(id)) continue;
    const inp = await f.$(`[id="${id}"]`);
    if (!inp || !(await inp.isVisible().catch(() => false)) && (await inp.getAttribute('role')) !== 'combobox') continue;
    const kind = await inp.evaluate(e => e.getAttribute('role') || e.type || e.tagName.toLowerCase());
    if (kind === 'file') continue;
    const req = /\*\s*$/.test(t);
    if (/--\d+$/.test(id) && /month|year/i.test(t) && !req) continue;  // education dates: optional, and the work-history dates don't apply
    try {
    let ex = EXTRA[id];
    if (ex === undefined) for (const k of Object.keys(EXTRA)) { if (!/^question_/.test(k) && new RegExp(k, 'i').test(t)) { ex = EXTRA[k]; break; } }
    if (kind === 'combobox') {
      const cur = await inp.evaluate(e => e.closest('.select__control')?.querySelector('.select__single-value, .select__multi-value')?.innerText || '');
      if (cur) continue;
      let got = null;
      if (ex !== undefined) got = await pick(inp, [ex].flat().map(x => typeof x === 'string' && x.startsWith('/') ? new RegExp(x.slice(1, -1), 'i') : x));
      else {
        for (const r of RULES.filter(([re]) => re.test(t))) { got = await pick(inp, typeof r[1] === 'function' ? r[1](t) : r[1]); if (got) break; }
        if (!got && req) got = await pick(inp, [/(^|\/ )(acknowledged?|i acknowledge|i agree|agreed?|i consent|i understand|confirmed?|true)\b/i]);  // sole-option consent boxes
      }
      if (got) log.push(`${t.slice(0, 60)} => ${got}`);
      else if (req) { await inp.click(); await page.waitForTimeout(300); const o = await opts(); await page.keyboard.press('Escape'); pending.push({ id, t, o: o.slice(0, 15) }); }
    } else if (kind === 'checkbox' || kind === 'radio') {
      if (ex) await inp.check().catch(() => {});
    } else {
      if (await inp.inputValue()) continue;
      let v = ex;
      if (v === undefined) { const r = TEXT.find(([re]) => re.test(t)); if (r) v = r[1]; }
      if (v === 'HUMAN_CHECK') { out({ state: 'BLOCKED', reason: `human check: ${t.slice(0, 80)}`, url: URL_, company }); process.exit(0); }
      if (v === null || (v === undefined && !req)) continue;
      if (v !== undefined) { await typeIn(inp, v); log.push(`${t.slice(0, 60)} := ${String(v).slice(0, 40)}`); }
      else pending.push({ id, t, kind });
    }
    } catch (e) {
      await page.keyboard.press('Escape').catch(() => {}); if (req) pending.push({ id, t, err: String(e).split('\n')[0].slice(0, 120) });
      if (pending.filter(p => p.err).length >= 3) break;  // form won't take clicks (overlay/custom page): hand it off now
    }
  }
  if (!pending.length || pending.filter(p => p.err).length >= 3) break;
  }
  // checkbox groups: consent → agree, demographics → decline, single "I agree" boxes → check
  const groups = await f.$$eval('fieldset', fs => fs.map(x => ({ legend: (x.querySelector('legend')?.innerText || '').trim(), boxes: [...x.querySelectorAll('input[type=checkbox],input[type=radio]')].map(b => ({ id: b.id, l: (b.closest('label')?.innerText || document.querySelector(`label[for="${b.id}"]`)?.innerText || '').trim(), c: b.checked })) })).filter(g => g.boxes.length));
  for (const g of groups) {
    if (g.boxes.some(b => b.c)) continue;
    let target = null, ex;
    for (const k of Object.keys(EXTRA)) if (!/^question_/.test(k) && new RegExp(k, 'i').test(g.legend)) { ex = EXTRA[k]; break; }
    if (ex) target = g.boxes.find(b => new RegExp(ex, 'i').test(b.l));
    else if (/gender|race|ethnic|veteran|disab|orientation|transgender|pronoun|identity/i.test(g.legend)) target = g.boxes.find(b => /decline|wish|prefer not/i.test(b.l));
    else target = g.boxes.find(b => /^\W*(i )?(agree|acknowledge|consent|accept|understand|confirm|certify|yes)/i.test(b.l))
      || (/privacy|consent|acknowledg|agree|certify|confidential/i.test(g.legend) ? g.boxes[0] : null);
    if (target) { await f.locator(`[id="${target.id}"]`).check({ force: true }).catch(() => {}); log.push(`[${g.legend.slice(0, 50)}] x ${target.l.slice(0, 40)}`); }
    else if (/\*/.test(g.legend)) pending.push({ group: g.legend, o: g.boxes.map(b => b.l) });
  }
  for (const cb of await f.$$('input[type=checkbox][id^=gdpr]')) if (!(await cb.isChecked())) await cb.check({ force: true }).catch(() => {});

  if (pending.length) { out({ state: 'NEED_HUMAN', url: URL_, company, title, pending, log }); process.exit(0); }
  if (DRY) { out({ state: 'FILLED', url: URL_, company, title, log }); process.exit(0); }

  const since = new Date(Date.now() - 30000);
  const isDone = async () => /confirmation/.test(page.url()) || /thank you for (applying|your (application|interest))|application (has been )?(received|submitted)/i.test(await page.locator('body').innerText());
  await f.getByRole('button', { name: /submit/i }).last().click();
  await page.waitForTimeout(7000);
  if (await isDone()) { out({ state: 'SUBMITTED', url: URL_, company, title, log }); process.exit(0); }
  if (await f.$('#security-input-0')) {
    const code = await fetchCode(company, since);
    if (!code) { out({ state: 'BLOCKED', reason: 'security code email never arrived', url: URL_, company }); process.exit(0); }
    for (let i = 0; i < 8; i++) await f.locator(`#security-input-${i}`).fill(code[i]);
    await f.getByRole('button', { name: /submit/i }).last().click();
    await page.waitForTimeout(7000);
    out({ state: (await isDone()) ? 'SUBMITTED' : 'ERRORS', url: URL_, company, title, log, code_used: true }); process.exit(0);
  }
  const body = await page.locator('body').innerText();
  if (/recaptcha|captcha|verify you are human/i.test(body) || (await page.$('iframe[src*="captcha"]'))) { out({ state: 'BLOCKED', reason: 'captcha on submit', url: URL_, company }); process.exit(0); }
  const errs = (await f.locator('.helper-text--error, [class*=error]').allInnerTexts()).filter(Boolean).slice(0, 12);
  out({ state: 'ERRORS', url: URL_, company, title, pending: errs, log });
} catch (e) {
  out({ state: 'ERRORS', url: URL_, pending: [String(e).slice(0, 300)] });
} finally {
  await browser.close();
}
