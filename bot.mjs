#!/usr/bin/env node
/**
 * Telegram → `claude -p` router for the auto-applier.
 *
 * Each inbound message spawns a fresh `claude -p` in the repo. claude -p uses the
 * same OAuth/Max-subscription billing as interactive Claude (no Agent SDK credits),
 * and a fresh process per message means no context bloat. Durable state lives in
 * data/ CSVs; the browser lives for the duration of one application.
 */
import { spawn } from 'child_process'
import { readFileSync, appendFileSync, existsSync, mkdirSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ── .env → process.env (so claude -p and the helper scripts share creds) ──────
function loadEnv() {
  const p = join(__dirname, '.env')
  if (!existsSync(p)) return
  for (const line of readFileSync(p, 'utf8').split('\n')) {
    const m = line.match(/^([A-Z_]+)=(.+)$/)
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim()
  }
}
loadEnv()

const CHANNELS_ENV = join(process.env.HOME, '.claude/channels/telegram/.env')
const fromChannels = (key) => existsSync(CHANNELS_ENV)
  ? readFileSync(CHANNELS_ENV, 'utf8').match(new RegExp(`${key}=(.+)`))?.[1]?.trim()
  : undefined

const TOKEN = process.env.TELEGRAM_BOT_TOKEN ?? fromChannels('TELEGRAM_BOT_TOKEN')
if (!TOKEN) { console.error('No TELEGRAM_BOT_TOKEN'); process.exit(1) }

// Auth allowlist — only Jack. Without it the bot answers ANYONE who messages it.
const ALLOWED = new Set(
  (process.env.TELEGRAM_CHAT_ID ?? fromChannels('TELEGRAM_CHAT_ID') ?? '')
    .split(',').map((s) => s.trim()).filter(Boolean)
)
if (ALLOWED.size === 0) console.warn('WARN: TELEGRAM_CHAT_ID unset — allowlist disabled, bot will answer anyone')

const API = `https://api.telegram.org/bot${TOKEN}`
const TIMEOUT_MS = 15 * 60 * 1000 // email PIN round-trips can take minutes
const LOG_DIR = join(__dirname, 'logs')
if (!existsSync(LOG_DIR)) mkdirSync(LOG_DIR, { recursive: true })

let offset = 0
let busy = false
const queue = []

function logLine(s) {
  const line = `[${new Date().toISOString()}] ${s}`
  console.log(line)
  try { appendFileSync(join(LOG_DIR, 'bot.log'), line + '\n') } catch {}
}

async function tg(method, body) {
  const r = await fetch(`${API}/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return r.json()
}

async function sendMessage(chatId, text, replyTo) {
  const chunks = []
  for (let i = 0; i < text.length; i += 4000) chunks.push(text.slice(i, i + 4000))
  let lastId
  for (const chunk of chunks) {
    const r = await tg('sendMessage', {
      chat_id: chatId, text: chunk, reply_to_message_id: replyTo, parse_mode: 'Markdown',
    }).catch(() => tg('sendMessage', { chat_id: chatId, text: chunk, reply_to_message_id: replyTo }))
    lastId = r?.result?.message_id
  }
  return lastId
}

async function editMessage(chatId, messageId, text) {
  const chunks = []
  for (let i = 0; i < text.length; i += 4000) chunks.push(text.slice(i, i + 4000))
  await tg('editMessageText', { chat_id: chatId, message_id: messageId, text: chunks[0] }).catch(() => {})
  for (let i = 1; i < chunks.length; i++) await sendMessage(chatId, chunks[i])
}

function runClaude(prompt, cwd) {
  return new Promise((resolve) => {
    const proc = spawn('claude', [
      '-p', '--output-format', 'text',
      '--mcp-config', join(cwd, '.mcp.json'), '--strict-mcp-config',
      prompt,
    ], {
      cwd, env: { ...process.env }, stdio: ['ignore', 'pipe', 'pipe'],
    })
    let out = ''
    proc.stdout.on('data', (d) => (out += d))
    proc.stderr.on('data', (d) => (out += d))
    const timer = setTimeout(() => { proc.kill(); resolve((out.trim() || '') + '\n\n⏱ timed out (15m)') }, TIMEOUT_MS)
    proc.on('close', () => { clearTimeout(timer); resolve(out.trim() || '(no output)') })
    proc.on('error', (e) => { clearTimeout(timer); resolve(`Error: ${e.message}`) })
  })
}

async function handleMessage(msg) {
  const chatId = msg.chat.id
  const text = msg.text
  if (!text) return

  if (ALLOWED.size && !ALLOWED.has(String(chatId))) {
    logLine(`DENY chat=${chatId} from=${msg.from?.first_name}: ${text.slice(0, 80)}`)
    return // silently ignore — don't advertise the bot to strangers
  }

  logLine(`MSG ${msg.from?.first_name}: ${text}`)
  const workingId = await sendMessage(chatId, '⏳ working...', msg.message_id)
  const t0 = Date.now()
  const response = await runClaude(text, __dirname)
  const secs = Math.round((Date.now() - t0) / 1000)
  if (workingId) await editMessage(chatId, workingId, response)
  else await sendMessage(chatId, response)
  logLine(`REPLY ${response.length} chars in ${secs}s`)
}

async function processQueue() {
  if (busy || queue.length === 0) return
  busy = true
  const msg = queue.shift()
  try { await handleMessage(msg) } catch (e) { logLine('HANDLE_ERROR ' + e.message) }
  busy = false
  processQueue()
}

async function poll() {
  while (true) {
    try {
      const r = await tg('getUpdates', { offset, timeout: 30 })
      if (r.ok && r.result?.length) {
        for (const u of r.result) {
          offset = u.update_id + 1
          if (u.message) { queue.push(u.message); processQueue() }
        }
      }
    } catch (e) {
      logLine('POLL_ERROR ' + e.message)
      await new Promise((r) => setTimeout(r, 3000))
    }
  }
}

const me = await tg('getMe')
logLine(`Bot started: @${me.result?.username} | workspace ${__dirname} | allowlist ${ALLOWED.size || 'OFF'}`)
poll()
