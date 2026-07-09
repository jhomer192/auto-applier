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

// Peer Claude bots (e.g. classistant) whose messages count as instructions —
// so "pass it to the applier" handoffs actually land instead of being ignored.
const PEER_BOTS = new Set(
  (process.env.DISCORD_PEER_BOT_IDS ?? '')
    .split(',').map(s => s.trim()).filter(Boolean)
)

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

// ── In-flight working messages, persisted so a restart can finalize them ─────
// (A service restart used to leave "🔧 working — 17174s…" frozen forever.)
const INFLIGHT_FILE = join(__dirname, 'data', 'inflight.json')

function loadInflight() {
  try { return JSON.parse(readFileSync(INFLIGHT_FILE, 'utf8')) } catch { return {} }
}

function saveInflight(map) {
  try {
    mkdirSync(join(__dirname, 'data'), { recursive: true })
    writeFileSync(INFLIGHT_FILE, JSON.stringify(map))
  } catch {}
}

function trackInflight(channelId, messageId) {
  const m = loadInflight()
  if (messageId) m[channelId] = messageId
  else delete m[channelId]
  saveInflight(m)
}

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

// ── Usage-limit detection → auto-resume ──────────────────────────────────────
// When claude -p dies on the subscription usage limit, we pause the queue and
// schedule an automatic resume at the reset time. Never a cap: if we guess the
// reset wrong and hit the limit again, we just detect it again and reschedule.
const LIMIT_RE = /(you'?ve|you have) hit your (usage )?limit|\d+-hour limit reached|usage limit reached|session limit[^\n]*reset|Claude AI usage limit/i
// For exit-0 finals the phrase must START the reply — a normal answer that
// merely mentions "usage limit reached" must not pause the bot.
const LIMIT_AT_START_RE = /^[\s>*_`⏸-]*((you'?ve|you have) hit your (usage )?limit|\d+-hour limit reached|usage limit reached|Claude AI usage limit)/i

const RECHECK_MS = 30 * 60 * 1000

function detectUsageLimit(blob) {
  if (!LIMIT_RE.test(blob)) return null
  // "Claude AI usage limit reached|1751749200" — epoch after a pipe
  const epoch = blob.match(/\|(\d{10,13})/)
  if (epoch) {
    let t = Number(epoch[1])
    if (t < 1e12) t *= 1000
    return { resetAt: Math.max(t, Date.now()) }  // past epoch → resume now
  }
  // "…resets 3am" / "resets at 10:30pm" — interpreted in server-local time,
  // which can be wrong (message may carry another TZ). Too-early is self-healing
  // (re-detect → reschedule); guard against too-LATE by distrusting anything
  // over 6h out (Max limits reset in 5h windows) and rechecking instead.
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
  // Unknown wording: recheck in 30 min (re-detection reschedules if still limited)
  return { resetAt: Date.now() + RECHECK_MS }
}

const CONTINUE_PROMPT =
  'The Claude usage limit has reset. Continue exactly where you left off — finish any ' +
  'application that was mid-flight and keep working through your queue. Check ' +
  'data/applied.csv and data/seen.csv first so nothing gets applied to twice.'

let pausedUntil = 0
let resumeTimer = null

// Pause state persists to disk so a service restart mid-pause still resumes —
// otherwise the "will auto-resume" promise silently dies with the process.
const PAUSE_FILE = join(__dirname, 'data', 'pause.json')

function savePause(channelId) {
  try {
    mkdirSync(join(__dirname, 'data'), { recursive: true })
    writeFileSync(PAUSE_FILE, JSON.stringify({ channelId, pausedUntil }))
  } catch {}
}

function schedulePauseResume(channelId, resetAt) {
  const until = Math.max(Date.now() + 60_000, resetAt + 90_000)
  pausedUntil = Math.max(pausedUntil, until)
  // Resume the interrupted turn first, ahead of anything queued meanwhile.
  if (!queue.some(it => it.synthetic && it.channelId === channelId)) {
    queue.unshift({ channelId, msg: null, synthetic: true, content: CONTINUE_PROMPT })
  }
  savePause(channelId)
  clearTimeout(resumeTimer)
  resumeTimer = setTimeout(() => {
    pausedUntil = 0
    savePause(null)
    logLine('USAGE_LIMIT_RESUME')
    processQueue()
  }, until - Date.now())
  logLine(`USAGE_LIMIT_PAUSE until=${new Date(until).toISOString()}`)
  return until
}

// Discord-native timestamp: renders in the reader's local timezone on any device.
const fmtWhen = (t) => `<t:${Math.floor(t / 1000)}:t>`

// ── Active claude children (per channel) so /stop can interrupt ──────────────
const active = new Map() // channelId → { child, killed, kill() }

function stopChannel(channelId) {
  // Drop any queued (not-yet-started) messages for this channel.
  let dropped = 0
  for (let i = queue.length - 1; i >= 0; i--) {
    if (queue[i].channelId === channelId) { queue.splice(i, 1); dropped++ }
  }
  // /stop is the manual override for everything — including a scheduled pause
  // (a mis-parsed reset time must never brick the bot until someone SSHes in).
  let unpaused = false
  if (Date.now() < pausedUntil) {
    pausedUntil = 0
    clearTimeout(resumeTimer)
    savePause(null)
    unpaused = true
    logLine('PAUSE_CLEARED by /stop')
  }
  const entry = active.get(channelId)
  if (entry) { entry.kill(); return { stopped: true, dropped, unpaused } }
  return { stopped: false, dropped, unpaused }
}

// ── Run claude, streaming events; surface each action via onActivity ─────────
// onMilestone gets apply-result lines ("Applied: …", "BLOCKED …") for the
// durable per-application feed. Returns { text, sessionId, stopped, limitedAt }
function runClaude(prompt, channelId, onActivity, onMilestone) {
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
            const t = b.text.trim()
            assistantText.push(t)
            onActivity?.('💬 ' + firstLine(t))
            // Durable progress: wave summaries ("Applied: 18 | Failed: 2 …")
            // and ⛔-marked blockers survive as their own messages instead of
            // vanishing when the working message is re-edited. Deliberately NOT
            // per-apply lines or plain "blocked/failed …, retrying" narration —
            // a 100-apply run must not be 40 pings.
            if (/^(✅\s*)?applied:\s*\d+|^⛔/i.test(t)) onMilestone?.(t)
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

      // The limit notice usually arrives as a NORMAL final reply with exit code 0
      // ("You've hit your limit · resets 6:40pm (UTC)") — so also test finalText
      // itself, not just error exits.
      let limitedAt = null
      const limitIsTheReply = finalText != null && finalText.length < 300 && LIMIT_AT_START_RE.test(finalText)
      if (!entry.killed && (limitIsTheReply || finalText == null || code !== 0)) {
        const limit = detectUsageLimit(`${finalText || ''} ${stderr}`)
        if (limit) limitedAt = limit.resetAt
      }
      resolve({ text, sessionId, stopped: entry.killed, limitedAt })
    })

    child.on('error', e => {
      active.delete(channelId)
      resolve({ text: `Error spawning claude: ${e.message}`, sessionId, stopped: false })
    })
  })
}

// ── Self-echo guard ───────────────────────────────────────────────────────────
// Peer bots (e.g. classistant) can relay our own status replies back into the
// channel — without this, that relay gets re-queued as a "new instruction" and
// undoes a /stop by re-launching a fresh claude -p turn (seen live 2026-07-09:
// our own "Nothing is running." reply came back via classistant and restarted
// the applier seconds after Jack stopped it). Track recent outgoing text and
// drop any peer message that's just an echo of something we said ourselves.
const recentOwnTexts = []
function trackOwnText(t) {
  if (!t) return
  const trimmed = t.trim()
  if (!trimmed) return
  recentOwnTexts.push({ text: trimmed, at: Date.now() })
  while (recentOwnTexts.length > 20) recentOwnTexts.shift()
}
function isSelfEcho(content) {
  const now = Date.now()
  const trimmed = content.trim()
  return recentOwnTexts.some(e => e.text === trimmed && now - e.at < 5 * 60_000)
}

// ── Discord message chunking (2000 char limit) ──────────────────────────────
async function sendChunked(channel, text, replyToMsg = null) {
  trackOwnText(text)
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

async function handleMessage(item) {
  const who = item.msg ? item.msg.author.username : 'auto-resume'
  logLine(`MSG ${who}: ${item.content.slice(0, 120)}`)

  const channel = item.msg?.channel
    ?? await client.channels.fetch(item.channelId).catch(() => null)
  if (!channel) {
    // Transient Discord hiccup: don't eat the item (a synthetic resume would be
    // lost forever) — put it back and retry in 30s.
    logLine(`NO_CHANNEL ${item.channelId} — requeued, retrying in 30s`)
    queue.unshift(item)
    pausedUntil = Math.max(pausedUntil, Date.now() + 30_000)
    setTimeout(processQueue, 31_000)
    return
  }

  const workingMsg = await channel.send(
    item.synthetic
      ? '▶️ usage limit reset — resuming where I left off… (`/stop` to cancel)'
      : '⏳ working… (`/stop` to cancel)'
  ).catch(() => null)
  if (workingMsg) trackInflight(item.channelId, workingMsg.id)
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

  const onMilestone = (t) => { trackOwnText(t.slice(0, 1900)); channel.send(t.slice(0, 1900)).catch(() => {}) }

  const { text, sessionId, limitedAt } = await runClaude(item.content, item.channelId, onActivity, onMilestone)
  const secs = Math.round((Date.now() - t0) / 1000)

  let body = text
  if (limitedAt) {
    const until = schedulePauseResume(item.channelId, limitedAt)
    body = `⏸ Claude usage limit reached — pausing, will auto-resume ${fmtWhen(until)} and pick up where I left off. Messages sent meanwhile are queued, not lost.`
  }

  const footer = sessionId ? `\n\n*session: \`${sessionId.slice(0, 8)}…\` · ${secs}s · ${activity.length} actions*` : ''
  const fullReply = body + footer

  if (workingMsg) {
    const first = fullReply.slice(0, 1900)
    trackOwnText(first)
    await workingMsg.edit(first).catch(() => null)
    for (let i = 1900; i < fullReply.length; i += 1900) {
      trackOwnText(fullReply.slice(i, i + 1900))
      await channel.send(fullReply.slice(i, i + 1900)).catch(() => null)
    }
  } else {
    await sendChunked(channel, fullReply)
  }
  trackInflight(item.channelId, null)

  logLine(`REPLY ${body.length} chars in ${secs}s | session=${sessionId?.slice(0, 8)}${limitedAt ? ' | LIMITED' : ''}`)
}

async function processQueue() {
  if (busy || queue.length === 0 || Date.now() < pausedUntil) return
  busy = true
  const item = queue.shift()
  try { await handleMessage(item) } catch (e) { logLine('HANDLE_ERR ' + e.message) }
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
  'If I hit the Claude usage limit I pause and auto-resume when it resets —',
  'anything you send meanwhile queues up and runs in order.',
].join('\n')

async function handleCommand(msg, cmd) {
  if (cmd === '/stop' || cmd === '/cancel' || cmd === '/halt') {
    const { stopped, dropped, unpaused } = stopChannel(msg.channelId)
    const bits = []
    if (dropped) bits.push(`dropped ${dropped} queued`)
    if (unpaused) bits.push('cleared the usage-limit pause')
    const extra = bits.length ? ` (${bits.join(', ')})` : ''
    const replyText = stopped ? `🛑 Stopping the current turn…${extra}` : `Nothing is running${extra ? extra : '.'}`
    trackOwnText(replyText)
    await msg.channel.send(replyText).catch(() => {})
    logLine(`STOP by ${msg.author.username}: stopped=${stopped} dropped=${dropped} unpaused=${unpaused}`)
    return true
  }
  if (cmd === '/status') {
    const running = active.has(msg.channelId)
    const sess = sessions[msg.channelId]
    const paused = Date.now() < pausedUntil
    const state = paused ? `⏸ paused (usage limit) — resumes ${fmtWhen(pausedUntil)}` : running ? '🟢 running' : '⚪️ idle'
    const statusText = `${state} · queued: ${queue.filter(m => m.channelId === msg.channelId).length}` +
      (sess ? ` · session \`${sess.slice(0, 8)}…\`` : '')
    trackOwnText(statusText)
    await msg.channel.send(statusText).catch(() => {})
    return true
  }
  if (cmd === '/help') {
    trackOwnText(HELP)
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

client.once('ready', async () => {
  const activeSessions = Object.keys(sessions).length
  logLine(`Bot ready: ${client.user.tag} | channel=${CHANNEL_ID ?? 'any'} | allowlist=${ALLOWED.size || 'OFF'} | active_sessions=${activeSessions}`)

  // Finalize working messages orphaned by a restart instead of leaving them
  // frozen at "🔧 working — …" forever.
  const inflight = loadInflight()
  for (const [chId, msgId] of Object.entries(inflight)) {
    if (active.has(chId)) continue  // genuinely running right now — leave it
    try {
      const ch = await client.channels.fetch(chId)
      const m = await ch.messages.fetch(msgId)
      await m.edit('⚠️ interrupted by a bot restart — the session is preserved; send a message to continue where it left off.')
      logLine(`INFLIGHT_FINALIZED channel=${chId}`)
    } catch { /* message gone — nothing to fix */ }
    trackInflight(chId, null)
  }

  // Recover a usage-limit pause that a restart would otherwise erase: the
  // "will auto-resume" promise must survive the process.
  try {
    const saved = JSON.parse(readFileSync(PAUSE_FILE, 'utf8'))
    if (saved?.channelId && saved.pausedUntil > 0) {
      logLine(`PAUSE_RECOVERED until=${new Date(saved.pausedUntil).toISOString()}`)
      schedulePauseResume(saved.channelId, saved.pausedUntil - 90_000)
    }
  } catch { /* no saved pause */ }
})

client.on('messageCreate', async (msg) => {
  if (msg.author.id === client.user?.id) return
  if (CHANNEL_ID && msg.channelId !== CHANNEL_ID) return

  // Peer Claude bots (classistant relays) may hand work off; other bots are ignored.
  const isPeer = msg.author.bot && PEER_BOTS.has(msg.author.id)
  if (msg.author.bot && !isPeer) return
  if (!msg.author.bot && ALLOWED.size && !ALLOWED.has(msg.author.id)) {
    logLine(`DENY user=${msg.author.username} (${msg.author.id})`)
    return
  }
  const content = msg.content?.trim()
  if (!content) return
  if (isPeer) logLine(`PEER ${msg.author.username}: ${content.slice(0, 120)}`)

  // A peer bot relaying our own status text back at us is not a new instruction —
  // queuing it would re-launch a turn right after e.g. a /stop (see recentOwnTexts).
  if (isPeer && isSelfEcho(content)) {
    logLine(`SELF_ECHO_IGNORED ${msg.author.username}: ${content.slice(0, 120)}`)
    return
  }

  // Slash commands run immediately, even mid-turn — this is how /stop cancels.
  // Humans only: a peer bot must not be able to /stop a run.
  const cmd = content.toLowerCase()
  if (!msg.author.bot && cmd.startsWith('/')) {
    if (await handleCommand(msg, cmd)) return
  }

  queue.push({ channelId: msg.channelId, content, msg })
  if (Date.now() < pausedUntil) {
    const queuedText = `⏸ queued (#${queue.length}) — paused for the Claude usage limit, auto-resumes ${fmtWhen(pausedUntil)}.`
    trackOwnText(queuedText)
    msg.reply(queuedText).catch(() => {})
  } else if (busy || active.size > 0) {
    msg.react('⏳').catch(() => {})
  }
  processQueue()
})

client.login(TOKEN).catch(err => {
  console.error('Discord login failed:', err.message)
  process.exit(1)
})
