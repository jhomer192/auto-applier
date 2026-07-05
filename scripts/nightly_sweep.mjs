#!/usr/bin/env node
/**
 * Nightly inbox sweep — reads the inbox(es), maintains data/action_items.csv,
 * posts the open-items digest to Discord.
 *
 * Run by applier-nightly.timer. Waits (never skips) while the main brain has a
 * claude run in flight, and retries forever on the Claude usage limit — never
 * gives up, never caps. No browser: this session is read-and-record only.
 */
import { spawn, execSync } from 'child_process'
import { readFileSync, existsSync, appendFileSync, mkdirSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))

function loadEnv() {
  const p = join(ROOT, '.env')
  if (!existsSync(p)) return
  for (const line of readFileSync(p, 'utf8').split('\n')) {
    const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.+)$/)
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim()
  }
}
loadEnv()

const TOKEN = process.env.DISCORD_BOT_TOKEN
const CHANNEL_ID = process.env.DISCORD_CHANNEL_ID
if (!TOKEN || !CHANNEL_ID) {
  console.error('nightly_sweep: DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID missing in .env')
  process.exit(1)
}

function logLine(s) {
  const line = `[${new Date().toISOString()}] ${s}`
  console.log(line)
  try {
    mkdirSync(join(ROOT, 'logs'), { recursive: true })
    appendFileSync(join(ROOT, 'logs', 'nightly_sweep.log'), line + '\n')
  } catch {}
}

const secondInbox = process.env.IMAP2_HOST
  ? `\n   - Jack's inbox:           node scripts/check_email.cjs --account 2 --since 48h --limit 30`
  : ''

const PROMPT = `NIGHTLY INBOX SWEEP (automated run, no user present — do not ask questions).
This run is READ-AND-RECORD ONLY: do not apply to jobs, do not open any browser,
do not follow links from emails. Never send email.

1. Read the last 48h of the inbox${secondInbox ? 'es' : ''} (read-only):
   - Zach's Gmail:            node scripts/check_email.cjs --since 48h --limit 30${secondInbox}
   Use scripts/dump_email_body.cjs when a snippet isn't enough to judge an email.

2. Find everything that needs HUMAN follow-up: recruiter replies, interview
   invitations or scheduling links, requests for more info/documents, deadlines
   ("reply by", "schedule by"), and rejections worth recording.

3. Maintain data/action_items.csv (create with header if missing):
   date_added,due,who,contact,action,status,source
   - Append new items with status=open. Don't duplicate an existing open item
     (same contact + same action).
   - Flip status to done only when the thread clearly shows it was handled.

4. Your final output becomes a Discord post — keep it tight:
   - First line: 📬 Nightly sweep — <count> new, <count> open
   - New items tonight, one bullet each: **who** — action (due: when) [which inbox]
   - Then ALL open items as short lines: \`due\` who — action
   - If there is nothing new AND nothing open, output exactly:
     "📬 Nightly sweep: inbox clear, no open action items."`

// Same detection as discord_bot.mjs — the limit notice usually arrives as a
// NORMAL final reply with exit code 0.
const LIMIT_RE = /(you'?ve|you have) hit your (usage )?limit|\d+-hour limit reached|usage limit reached|session limit[^\n]*reset|Claude AI usage limit/i
const LIMIT_AT_START_RE = /^[\s>*_`⏸-]*((you'?ve|you have) hit your (usage )?limit|\d+-hour limit reached|usage limit reached|Claude AI usage limit)/i
const RECHECK_MS = 30 * 60 * 1000

function detectUsageLimit(blob) {
  if (!LIMIT_RE.test(blob)) return null
  const epoch = blob.match(/\|(\d{10,13})/)
  if (epoch) {
    let t = Number(epoch[1])
    if (t < 1e12) t *= 1000
    return { resetAt: Math.max(t, Date.now()) }
  }
  const m = blob.match(/reset[s]?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)/i)
  if (m) {
    let h = Number(m[1]) % 12
    if (m[3].toLowerCase() === 'pm') h += 12
    const d = new Date()
    d.setHours(h, Number(m[2] || 0), 0, 0)
    if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1)
    if (d.getTime() - Date.now() > 6 * 3600 * 1000) return { resetAt: Date.now() + RECHECK_MS }
    return { resetAt: d.getTime() }
  }
  return { resetAt: Date.now() + RECHECK_MS }
}

// No browser for the sweep: empty MCP config (must be explicit — dropping the
// flag would let .claude/settings.json load the playwright server instead).
const EMPTY_MCP = join(ROOT, '.mcp.empty.json')

function brainBusy() {
  try {
    // [-] keeps the pattern from matching its own shell wrapper's cmdline
    execSync('pgrep -f "claude [-]p"', { stdio: 'pipe' })
    return true
  } catch { return false }
}

function runClaude() {
  return new Promise((resolve) => {
    const child = spawn('claude', [
      '-p',
      '--output-format', 'text',
      '--dangerously-skip-permissions',
      '--mcp-config', EMPTY_MCP,
      '--strict-mcp-config',
      PROMPT,
    ], { cwd: ROOT, env: { ...process.env }, stdio: ['ignore', 'pipe', 'pipe'] })

    let out = ''
    let err = ''
    child.stdout.on('data', d => { out += d })
    child.stderr.on('data', d => { err += d })
    child.on('close', code => resolve({ code, out: out.trim(), err: err.trim() }))
    child.on('error', e => resolve({ code: -1, out: '', err: e.message }))
  })
}

async function postToDiscord(text) {
  // Chunk on line boundaries so markdown pairs don't split across messages.
  const chunks = []
  let cur = ''
  for (const line of text.split('\n')) {
    if (cur && cur.length + line.length + 1 > 1900) { chunks.push(cur); cur = '' }
    cur = cur ? `${cur}\n${line}` : line
    while (cur.length > 1900) { chunks.push(cur.slice(0, 1900)); cur = cur.slice(1900) }
  }
  if (cur) chunks.push(cur)
  for (const c of chunks) {
    const res = await fetch(`https://discord.com/api/v10/channels/${CHANNEL_ID}/messages`, {
      method: 'POST',
      headers: { Authorization: `Bot ${TOKEN}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: c }),
    })
    if (!res.ok) logLine(`DISCORD_POST_ERR ${res.status} ${(await res.text()).slice(0, 200)}`)
  }
}

const sleep = ms => new Promise(r => setTimeout(r, ms))

logLine('SWEEP_START')

// Never stack a second claude+MCP on the 1.9GB box while the brain is mid-run —
// wait (not skip) until it's idle.
while (brainBusy()) {
  logLine('SWEEP_WAIT brain has a claude run in flight')
  await sleep(60_000)
}

for (;;) {
  const { code, out, err } = await runClaude()
  const limitIsTheReply = code === 0 && out && out.length < 300 && LIMIT_AT_START_RE.test(out)
  const limit = (limitIsTheReply || code !== 0 || !out) ? detectUsageLimit(`${out} ${err}`) : null
  if (limit) {
    const until = Math.max(Date.now() + 60_000, limit.resetAt + 90_000)
    logLine(`SWEEP_LIMIT_WAIT until=${new Date(until).toISOString()}`)
    await sleep(until - Date.now())
    continue
  }
  if (code !== 0 || !out) {
    logLine(`SWEEP_FAIL code=${code} err=${err.slice(0, 300)}`)
    await postToDiscord(`⚠️ Nightly sweep failed (exit ${code}): ${err.slice(0, 500) || 'no output'}`)
    process.exit(1)
  }
  await postToDiscord(out)
  logLine(`SWEEP_DONE ${out.length} chars`)
  process.exit(0)
}
