#!/usr/bin/env node
/**
 * Discord ↔ claude session router for the auto-applier.
 *
 * PERSISTENT CONTEXT: uses `claude -p --output-format stream-json --resume <id>` so
 * the applier remembers the full conversation across messages, AND every action it
 * takes (tool calls, progress notes) is streamed live to Discord as it happens — no
 * more silent multi-hour turns.
 *
 * CANCELLABLE: the running `claude` child is tracked per channel, so `/stop` can
 * interrupt an in-flight turn at any time. Full autonomy, always surfaced, always
 * cancellable.
 *
 * MCP (Playwright browser) is attached via .mcp.json.
 */
import { Client, GatewayIntentBits } from 'discord.js'
import { spawn } from 'child_process'
import { createInterface } from 'readline'
import {
  readFileSync, writeFileSync, appendFileSync,
  existsSync, mkdirSync,
} from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ── .env → process.env ──────────────────────────────────────────────────────
function loadEnv() {
  const p = join(__dirname, '.env')
  if (!existsSync(p)) return
  for (const line of readFileSync(p, 'utf8').split('\n')) {
    const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.+)$/)
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim()
  }
}
loadEnv()

const TOKEN = process.env.DISCORD_BOT_TOKEN
if (!TOKEN) { console.error('No DISCORD_BOT_TOKEN in .env'); process.exit(1) }

const ALLOWED = new Set(
  (process.env.DISCORD_ALLOWED_USER_IDS ?? '')
    .split(',').map(s => s.trim()).filter(Boolean)
)
if (ALLOWED.size === 0) console.warn('WARN: DISCORD_ALLOWED_USER_IDS unset — will respond to everyone')

const CHANNEL_ID = process.env.DISCORD_CHANNEL_ID ?? null

const LOG_DIR = join(__dirname, 'logs')
if (!existsSync(LOG_DIR)) mkdirSync(LOG_DIR, { recursive: true })

// ── Persistent sessions: channel_id → claude session UUID ───────────────────
const SESSIONS_FILE = join(__dirname, 'data', 'discord_sessions.json')

function loadSessions() {
  try { return JSON.parse(readFileSync(SESSIONS_FILE, 'utf8')) } catch { return {} }
}

function saveSessions(sessions) {
  try {
    mkdirSync(join(__dirname, 'data'), { recursive: true })
    writeFileSync(SESSIONS_FILE, JSON.stringify(sessions, null, 2))
  } catch (e) { logLine(`SESSIONS_SAVE_ERR: ${e.message}`) }
}

const sessions = loadSessions()   // { [channelId]: sessionId }

// ── Logging ─────────────────────────────────────────────────────────────────
function logLine(s) {
  const line = `[${new Date().toISOString()}] ${s}`
  console.log(line)
  try { appendFileSync(join(LOG_DIR, 'discord_bot.log'), line + '\n') } catch {}
}

// ── Human-readable label for a streamed tool call ────────────────────────────
const short = (s, n = 70) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s }
const firstLine = (s) => short(String(s ?? '').split('\n').find(l => l.trim()) || '', 90)

function toolLabel(name, input) {
  name = name || ''
  if (name === 'Bash') return `⚙️ ${short(input?.command, 90)}`
  if (name === 'Read') return `📖 read ${short(input?.file_path, 60)}`
  if (name === 'Write') return `✍️ write ${short(input?.file_path, 60)}`
  if (name === 'Edit') return `✏️ edit ${short(input?.file_path, 60)}`
  if (name === 'WebFetch') return `🌐 fetch ${short(input?.url, 70)}`
  if (name === 'WebSearch') return `🔎 search "${short(input?.query, 55)}"`
  if (name.includes('browser_navigate')) return `🌐 open ${short(input?.url, 70)}`
  if (name.includes('browser_click')) return `🖱 click ${short(input?.element || input?.ref, 45)}`
  if (name.includes('browser_type')) return `⌨️ type ${short(input?.text, 45)}`
  if (name.includes('browser_fill_form')) return `📝 fill form`
  if (name.includes('browser_file_upload')) return `📎 upload resume`
  if (name.includes('browser_snapshot')) return `👁 snapshot page`
  if (name.includes('browser_select_option')) return `▾ select option`
  if (name.includes('browser_press_key')) return `⌨️ press ${short(input?.key, 20)}`
  if (name.startsWith('mcp__playwright__')) return `🎭 ${name.replace('mcp__playwright__browser_', '').replace('mcp__playwright__', '')}`
  return `🔧 ${name}`
}

// ── Active claude children (per channel) so /stop can interrupt ──────────────
const active = new Map() // channelId → { child, killed, kill() }

function stopChannel(channelId) {
  // Drop any queued (not-yet-started) messages for this channel.
  let dropped = 0
  for (let i = queue.length - 1; i >= 0; i--) {
    if (queue[i].channelId === channelId) { queue.splice(i, 1); dropped++ }
  }
  const entry = active.get(channelId)
  if (entry) { entry.kill(); return { stopped: true, dropped } }
  return { stopped: false, dropped }
}

// ── Run claude, streaming events; surface each action via onActivity ─────────
// Returns { text, sessionId, stopped }
function runClaude(prompt, channelId, onActivity) {
  const existingSession = sessions[channelId]

  return new Promise((resolve) => {
    const args = [
      '-p',
      '--output-format', 'stream-json',
      '--verbose',
      '--dangerously-skip-permissions',
      '--mcp-config', join(__dirname, '.mcp.json'),
      '--strict-mcp-config',
    ]

    if (existingSession) {
      args.push('--resume', existingSession)
      logLine(`RESUME session=${existingSession} channel=${channelId}`)
    } else {
      logLine(`NEW SESSION channel=${channelId}`)
    }

    args.push(prompt)

    const child = spawn('claude', args, {
      cwd: __dirname,
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    })

    const entry = {
      child,
      killed: false,
      kill() {
        this.killed = true
        try { child.kill('SIGINT') } catch {}
        // Escalate if SIGINT didn't land.
        setTimeout(() => { try { if (!child.killed) child.kill('SIGTERM') } catch {} }, 1500)
        setTimeout(() => { try { if (!child.killed) child.kill('SIGKILL') } catch {} }, 4000)
      },
    }
    active.set(channelId, entry)

    let sessionId = existingSession
    let finalText = null
    const assistantText = []
    let stderr = ''

    const rl = createInterface({ input: child.stdout })
    rl.on('line', (raw) => {
      const line = raw.trim()
      if (!line) return
      let ev
      try { ev = JSON.parse(line) } catch { return }
      if (ev.session_id) sessionId = ev.session_id

      if (ev.type === 'assistant' && ev.message?.content) {
        for (const b of ev.message.content) {
          if (b.type === 'text' && b.text?.trim()) {
            assistantText.push(b.text.trim())
            onActivity?.('💬 ' + firstLine(b.text))
          } else if (b.type === 'tool_use') {
            onActivity?.(toolLabel(b.name, b.input))
          }
        }
      } else if (ev.type === 'result') {
        if (typeof ev.result === 'string') finalText = ev.result
      }
    })

    child.stderr.on('data', d => { stderr += d })

    child.on('close', (code) => {
      active.delete(channelId)

      if (sessionId && sessionId !== sessions[channelId]) {
        sessions[channelId] = sessionId
        saveSessions(sessions)
        logLine(`SESSION STORED: channel=${channelId} session=${sessionId}`)
      }

      let text = finalText ?? (assistantText.join('\n\n').trim() || null)
      if (entry.killed) {
        text = (text ? text + '\n\n' : '') + '🛑 stopped by user.'
      } else if (text == null) {
        text = code !== 0
          ? `⚠️ claude exited with code ${code}. ${stderr.trim().slice(0, 500)}`
          : '(no output)'
      }

      if (!entry.killed && (finalText == null || code !== 0)) {
        const _blob = `${finalText||''} ${stderr}`
        if (/usage limit reached|session limit[^\n]*reset|Claude AI usage limit/i.test(_blob)) {
          const _m = _blob.match(/reset[s]?[^\n.]*/i)
          text = `⏸ paused — Claude usage limit reached${_m ? ' (' + _m[0].trim() + ')' : ''}. A pause, not a failure — will resume when it resets.`
          logLine(`USAGE_LIMIT_PAUSE channel=${channelId}`)
        }
      }
      resolve({ text, sessionId, stopped: entry.killed })
    })

    child.on('error', e => {
      active.delete(channelId)
      resolve({ text: `Error spawning claude: ${e.message}`, sessionId, stopped: false })
    })
  })
}

// ── Discord message chunking (2000 char limit) ──────────────────────────────
async function sendChunked(channel, text, replyToMsg = null) {
  const chunks = []
  for (let i = 0; i < text.length; i += 1900) chunks.push(text.slice(i, i + 1900))
  for (let i = 0; i < chunks.length; i++) {
    if (i === 0 && replyToMsg) {
      await replyToMsg.reply(chunks[i]).catch(() => channel.send(chunks[i]).catch(() => {}))
    } else {
      await channel.send(chunks[i]).catch(() => {})
    }
  }
}

// ── Message handler ──────────────────────────────────────────────────────────
let busy = false
const queue = []

async function handleMessage(msg) {
  logLine(`MSG ${msg.author.username}: ${msg.content.slice(0, 120)}`)

  const workingMsg = await msg.channel.send('⏳ working… (`/stop` to cancel)').catch(() => null)
  const t0 = Date.now()

  // Live progress: keep a rolling activity log, edit the working message (throttled).
  const activity = []
  let lastEdit = 0
  const render = () => {
    const secs = Math.round((Date.now() - t0) / 1000)
    const head = `🔧 working — ${secs}s · ${activity.length} actions · \`/stop\` to cancel`
    const tail = activity.slice(-8).join('\n')
    return (tail ? `${head}\n${tail}` : head).slice(0, 1900)
  }
  const onActivity = (line) => {
    activity.push(line)
    const now = Date.now()
    if (workingMsg && now - lastEdit > 2500) {
      lastEdit = now
      workingMsg.edit(render()).catch(() => {})
    }
  }

  const { text, sessionId } = await runClaude(msg.content, msg.channelId, onActivity)
  const secs = Math.round((Date.now() - t0) / 1000)

  const footer = sessionId ? `\n\n*session: \`${sessionId.slice(0, 8)}…\` · ${secs}s · ${activity.length} actions*` : ''
  const fullReply = text + footer

  if (workingMsg) {
    const first = fullReply.slice(0, 1900)
    await workingMsg.edit(first).catch(() => null)
    for (let i = 1900; i < fullReply.length; i += 1900) {
      await msg.channel.send(fullReply.slice(i, i + 1900)).catch(() => null)
    }
  } else {
    await sendChunked(msg.channel, fullReply)
  }

  logLine(`REPLY ${text.length} chars in ${secs}s | session=${sessionId?.slice(0, 8)}`)
}

async function processQueue() {
  if (busy || queue.length === 0) return
  busy = true
  const msg = queue.shift()
  try { await handleMessage(msg) } catch (e) { logLine('HANDLE_ERR ' + e.message) }
  busy = false
  processQueue()
}

// ── Slash commands (handled immediately, never queued) ───────────────────────
const HELP = [
  '**Auto-applier commands**',
  '`/stop` (or `/cancel`) — interrupt the current turn and drop anything queued',
  '`/status` — is it running, how long, how many queued',
  '`/help` — this message',
  '',
  'Otherwise just talk to me — I apply autonomously across Zach\'s target lanes,',
  'surface every action live, and you can `/stop` at any time.',
].join('\n')

async function handleCommand(msg, cmd) {
  if (cmd === '/stop' || cmd === '/cancel' || cmd === '/halt') {
    const { stopped, dropped } = stopChannel(msg.channelId)
    const extra = dropped ? ` (dropped ${dropped} queued)` : ''
    await msg.channel.send(
      stopped ? `🛑 Stopping the current turn…${extra}` : `Nothing is running${extra ? extra : ''}.`
    ).catch(() => {})
    logLine(`STOP by ${msg.author.username}: stopped=${stopped} dropped=${dropped}`)
    return true
  }
  if (cmd === '/status') {
    const running = active.has(msg.channelId)
    const sess = sessions[msg.channelId]
    await msg.channel.send(
      `${running ? '🟢 running' : '⚪️ idle'} · queued: ${queue.filter(m => m.channelId === msg.channelId).length}` +
      (sess ? ` · session \`${sess.slice(0, 8)}…\`` : '')
    ).catch(() => {})
    return true
  }
  if (cmd === '/help') {
    await msg.channel.send(HELP).catch(() => {})
    return true
  }
  return false
}

// ── Discord client ───────────────────────────────────────────────────────────
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages,
  ],
})

client.on('ready', () => {
  const activeSessions = Object.keys(sessions).length
  logLine(`Bot ready: ${client.user.tag} | channel=${CHANNEL_ID ?? 'any'} | allowlist=${ALLOWED.size || 'OFF'} | active_sessions=${activeSessions}`)
})

client.on('messageCreate', async (msg) => {
  if (msg.author.bot) return
  if (CHANNEL_ID && msg.channelId !== CHANNEL_ID) return
  if (ALLOWED.size && !ALLOWED.has(msg.author.id)) {
    logLine(`DENY user=${msg.author.username} (${msg.author.id})`)
    return
  }
  const content = msg.content?.trim()
  if (!content) return

  // Slash commands run immediately, even mid-turn — this is how /stop cancels.
  const cmd = content.toLowerCase()
  if (cmd.startsWith('/')) {
    if (await handleCommand(msg, cmd)) return
  }

  queue.push(msg)
  processQueue()
})

client.login(TOKEN).catch(err => {
  console.error('Discord login failed:', err.message)
  process.exit(1)
})
