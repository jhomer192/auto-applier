"""Multi-turn Claude-with-tools conversation runtime for the Telegram bot.

Replaces the one-shot intent-parsing _handle_conversational. Each freeform
user message goes through a JSON-loop where Claude can:
  - call tools that mutate state (set prefs, dismiss jobs, add searches, ...),
  - record lessons that persist across conversations,
  - reply in natural language.

Up to MAX_INNER_ITERS rounds per user turn so a single message can compose
multiple actions ("don't do manager unless series A startup, the rest
investigate, also down for SWE" → roles update + dismiss-by-filter +
mark-for-investigate + reply, in one turn).

Design notes:
  - All Claude calls go through bot.llm.claude_call (claude CLI subprocess).
  - History is persisted in conversation_messages (sqlite); pruned to the
    most recent N rows when rendered into the prompt.
  - Lessons live in data/lessons.jsonl; preloaded into the system prompt
    so the bot doesn't re-ask things it already knows about Jack.
  - Per-chat asyncio.Lock prevents interleaved turns racing on history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiosqlite
from telegram import Update
from telegram.ext import ContextTypes

from bot.db import ApplicationDB
from bot.llm import claude_call, LLMError
from bot.models import SavedSearch
from bot.profile import load_preferences, save_preferences

logger = logging.getLogger(__name__)


# ── Configuration ───────────────────────────────────────────────────────────

import sys

# No artificial cap — match claude-bot's maxTurns: Number.MAX_SAFE_INTEGER.
# The model decides when it's done; runtime stops only when Claude returns
# no tool calls.
MAX_INNER_ITERS = sys.maxsize
HISTORY_LIMIT = 60                  # rows pulled from conversation_messages per turn
HISTORY_RENDER_CHAR_BUDGET = 12000  # cap on the rendered transcript section
LESSONS_PATH = "data/lessons.jsonl"
LESSONS_FIFO_CAP = 1000             # trim the file to most-recent N entries
LESSONS_PRELOAD_LIMIT = 30          # how many lessons to splice into system prompt
TYPING_KEEPALIVE_SECONDS = 4.5
LOCK_ACQUIRE_TIMEOUT = 60

# Per-chat lock so two messages from the same user never run inner loops in parallel.
_chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ── Tool implementations ────────────────────────────────────────────────────
#
# Each tool is `async fn(context, args) -> dict`. The dict is shown back to
# Claude verbatim as a tool result — keep returns compact; truncate big lists.

# Reused state-key from telegram_bot.py — set the bot up to flow into the
# existing batch-processing pipeline after a tool dispatches a selection.
BATCH_QUEUE = "batch_queue"


async def _tool_list_queue(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Return the user's pending job queue."""
    db: ApplicationDB = context.bot_data["db"]
    limit = int(args.get("limit", 50))
    jobs = await db.get_pending_queue()
    return {
        "count": len(jobs),
        "jobs": [
            {"id": j.id, "title": j.title, "company": j.company, "url": j.url}
            for j in jobs[:limit]
        ],
    }


async def _tool_get_prefs(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Return current user preferences."""
    profile: dict = context.bot_data["profile"]
    prefs = load_preferences(profile)
    return {
        "desired_roles": prefs.desired_roles,
        "excluded_companies": prefs.excluded_companies,
        "excluded_title_keywords": prefs.excluded_title_keywords,
        "min_salary": prefs.min_salary,
        "target_salary": prefs.target_salary,
        "seniority": prefs.seniority,
        "work_arrangement": prefs.work_arrangement,
        "auto_apply_threshold": prefs.auto_apply_threshold,
        "requires_sponsorship": prefs.requires_sponsorship,
        "auto_search": prefs.auto_search,
    }


async def _tool_update_pref(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Update a single preference field. args = {key, value}.
    Supported keys: roles, exclude_company, exclude_title, unexclude_company,
    unexclude_title, min_salary, target_salary, seniority, arrangement,
    autoapply, autosearch, sponsorship.
    """
    profile: dict = context.bot_data["profile"]
    profile_path: str = context.bot_data["profile_path"]
    prefs = load_preferences(profile)

    key = (args.get("key") or "").lower().replace("-", "_")
    value = args.get("value")

    if key == "roles":
        roles = value if isinstance(value, list) else [r.strip() for r in str(value).split(",") if r.strip()]
        prefs.desired_roles = [r.lower() for r in roles if r]
    elif key in ("exclude_company", "exclude_companies"):
        cos = value if isinstance(value, list) else [str(value)]
        for co in cos:
            co = str(co).strip()
            if co and co not in prefs.excluded_companies:
                prefs.excluded_companies.append(co)
    elif key in ("unexclude_company", "unexclude_companies"):
        cos = value if isinstance(value, list) else [str(value)]
        for co in cos:
            prefs.excluded_companies = [c for c in prefs.excluded_companies if c.lower() != str(co).strip().lower()]
    elif key in ("exclude_title", "exclude_title_keyword", "exclude_title_keywords"):
        kws = value if isinstance(value, list) else [str(value)]
        for kw in kws:
            kw = str(kw).strip().lower()
            if kw and kw not in prefs.excluded_title_keywords:
                prefs.excluded_title_keywords.append(kw)
    elif key in ("unexclude_title", "unexclude_title_keyword", "unexclude_title_keywords"):
        kws = value if isinstance(value, list) else [str(value)]
        for kw in kws:
            prefs.excluded_title_keywords = [k for k in prefs.excluded_title_keywords if k != str(kw).strip().lower()]
    elif key == "min_salary":
        prefs.min_salary = int(value)
    elif key == "target_salary":
        prefs.target_salary = int(value)
    elif key == "seniority":
        levels = value if isinstance(value, list) else [s.strip() for s in str(value).split(",") if s.strip()]
        prefs.seniority = [s.lower() for s in levels]
    elif key in ("arrangement", "work_arrangement"):
        modes = value if isinstance(value, list) else [m.strip() for m in str(value).split(",") if m.strip()]
        prefs.work_arrangement = [m.lower() for m in modes]
    elif key in ("autoapply", "auto_apply", "auto_apply_threshold"):
        prefs.auto_apply_threshold = int(value)
    elif key in ("autosearch", "auto_search"):
        prefs.auto_search = bool(value) if isinstance(value, bool) else str(value).lower() in ("on", "true", "yes", "1")
    elif key == "sponsorship":
        prefs.requires_sponsorship = bool(value) if isinstance(value, bool) else str(value).lower() in ("yes", "true", "1")
    else:
        return {"error": f"unknown pref key {key!r}",
                "supported": ["roles", "exclude_company", "exclude_title", "unexclude_company",
                              "unexclude_title", "min_salary", "target_salary", "seniority",
                              "arrangement", "autoapply", "autosearch", "sponsorship"]}

    save_preferences(profile, prefs, profile_path)
    context.bot_data["profile"] = profile
    return {"ok": True, "updated": key, "current": await _tool_get_prefs(context, {})}


async def _tool_dismiss_jobs(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Dismiss queued jobs by id. args = {ids: [int, ...]} OR {all: true}."""
    db: ApplicationDB = context.bot_data["db"]
    if args.get("all"):
        n = await db.dismiss_all_queued()
        context.bot_data.pop(BATCH_QUEUE, None)
        return {"dismissed": n, "scope": "all"}
    ids = args.get("ids") or []
    n = 0
    for jid in ids:
        try:
            await db.update_queued_job_status(int(jid), "dismissed")
            n += 1
        except Exception as e:
            logger.debug("dismiss job %s failed: %s", jid, e)
    return {"dismissed": n, "ids": ids}


async def _tool_investigate_jobs(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Mark a list of queue ids for sequential investigation. args = {ids: [int, ...]}.
    Drops non-selected pending items as 'dismissed' and queues the selected for the
    bot's existing batch-processing flow."""
    db: ApplicationDB = context.bot_data["db"]
    ids = {int(i) for i in (args.get("ids") or [])}
    if not ids:
        return {"error": "no job ids provided"}
    pending = await db.get_pending_queue()
    selected = [j for j in pending if j.id in ids]
    selected_ids = {j.id for j in selected}
    dismissed = 0
    for j in pending:
        if j.id not in selected_ids:
            await db.update_queued_job_status(j.id, "dismissed")
            dismissed += 1
    context.bot_data[BATCH_QUEUE] = list(selected)
    return {
        "investigating": len(selected),
        "dismissed_others": dismissed,
        "selected": [{"id": j.id, "title": j.title, "company": j.company} for j in selected],
    }


async def _tool_add_search(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Add a saved LinkedIn search. args = {query, location?}."""
    db: ApplicationDB = context.bot_data["db"]
    query = (args.get("query") or "").strip()
    location = (args.get("location") or "").strip()
    if not query:
        return {"error": "query is required"}
    s = SavedSearch(query=query, location=location)
    sid = await db.insert_search(s)
    return {"ok": True, "search_id": sid, "query": query, "location": location}


async def _tool_list_searches(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    db: ApplicationDB = context.bot_data["db"]
    searches = await db.get_all_searches()
    return {
        "count": len(searches),
        "searches": [
            {"id": s.id, "query": s.query, "location": s.location, "active": s.active}
            for s in searches
        ],
    }


async def _tool_remove_search(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    db: ApplicationDB = context.bot_data["db"]
    sid = args.get("id")
    if sid is None:
        return {"error": "id is required"}
    try:
        await db.deactivate_search(int(sid))
        return {"ok": True, "deactivated": int(sid)}
    except Exception as e:
        return {"error": str(e)}


async def _tool_get_application_history(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    db: ApplicationDB = context.bot_data["db"]
    limit = int(args.get("limit", 20))
    apps = await db.get_recent(limit=limit)
    return {
        "count": len(apps),
        "applications": [
            {"id": a.id, "title": a.title, "company": a.company, "status": a.status,
             "site": a.site, "applied_at": a.applied_at}
            for a in apps
        ],
    }


async def _tool_record_lesson(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Record a persistent lesson. args = {text, tags?}.
    Use this when the user reveals a lasting preference, constraint, or goal
    that should bias future behaviour without them having to repeat themselves.
    Examples: 'never apply to crypto companies', 'prefer Series A over later
    stage', 'avoid roles requiring relocation'."""
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "text is required"}
    tags = args.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    entry = {"ts": int(time.time()), "text": text, "tags": tags}
    Path(LESSONS_PATH).parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if Path(LESSONS_PATH).exists():
        lines = [ln for ln in Path(LESSONS_PATH).read_text().splitlines() if ln.strip()]
    lines.append(json.dumps(entry))
    if len(lines) > LESSONS_FIFO_CAP:
        lines = lines[-LESSONS_FIFO_CAP:]
    Path(LESSONS_PATH).write_text("\n".join(lines) + "\n")
    return {"ok": True, "stored": text}


async def _tool_recall_lessons(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Search lessons. args = {query?, limit?}."""
    query = (args.get("query") or "").strip().lower()
    limit = int(args.get("limit", 30))
    lessons = _load_lessons(limit=1000)
    if query:
        lessons = [
            le for le in lessons
            if query in le.get("text", "").lower() or any(query in t.lower() for t in le.get("tags", []))
        ]
    return {"count": len(lessons[-limit:]), "lessons": lessons[-limit:]}


# ── General-purpose tools (parity with claude-bot's tool surface) ───────────

async def _tool_webfetch(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Fetch a URL. Returns status + first ~8KB of body. Use for job postings,
    company pages, recruiter LinkedIn profiles, etc."""
    import aiohttp as _aiohttp
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "url is required"}
    if not url.startswith(("http://", "https://")):
        return {"error": "url must be http(s)"}
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (auto-applier)"
            }) as r:
                text = await r.text(errors="replace")
                return {"status": r.status, "url": url, "length": len(text),
                        "content": text[:8000]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _tool_read(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Read a file from disk. Returns first ~8KB of content."""
    path = (args.get("file_path") or args.get("path") or "").strip()
    if not path:
        return {"error": "file_path is required"}
    try:
        p = Path(path)
        if not p.exists():
            return {"error": f"no such file: {path}"}
        if p.is_dir():
            return {"error": f"{path} is a directory"}
        if p.stat().st_size > 1_000_000:
            return {"error": f"{path} too large (>{1_000_000} bytes)"}
        content = p.read_text(errors="replace")
        return {"path": str(p), "size": len(content), "content": content[:8000]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _tool_bash(context: ContextTypes.DEFAULT_TYPE, args: dict) -> dict:
    """Run an arbitrary shell command. 60s timeout. Output capped at 8KB stdout / 2KB stderr."""
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return {"error": "command is required"}
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/root/auto-applier",
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {"error": "command timed out after 60s", "command": cmd}
        return {
            "exit_code": proc.returncode,
            "stdout": stdout_b.decode("utf-8", errors="replace")[:8000],
            "stderr": stderr_b.decode("utf-8", errors="replace")[:2000],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# Tool registry: name → (one-line description, JSON-schema args, async fn)
TOOLS: dict[str, dict] = {
    "list_queue": {
        "description": "List the pending job queue (jobs the bot has discovered but not yet acted on).",
        "args": {"limit": "int, default 50"},
        "fn": _tool_list_queue,
    },
    "get_prefs": {
        "description": "Read the user's current preferences (roles, salary, exclusions, etc).",
        "args": {},
        "fn": _tool_get_prefs,
    },
    "update_pref": {
        "description": ("Update one preference. key ∈ {roles, exclude_company, exclude_title, "
                        "unexclude_company, unexclude_title, min_salary, target_salary, seniority, "
                        "arrangement, autoapply, autosearch, sponsorship}. value can be string, "
                        "int, bool, or list as appropriate. exclude_title 'manager' filters out "
                        "any job whose title contains 'manager' (case-insensitive)."),
        "args": {"key": "string", "value": "string|int|bool|list"},
        "fn": _tool_update_pref,
    },
    "dismiss_jobs": {
        "description": "Dismiss specific queued jobs by id, or all of them. args: {ids:[...]} or {all:true}.",
        "args": {"ids": "list[int] (optional)", "all": "bool (optional)"},
        "fn": _tool_dismiss_jobs,
    },
    "investigate_jobs": {
        "description": ("Pick which queued jobs to analyze and apply to. Pass the ids to investigate; "
                        "all other pending jobs in the queue are dismissed. After this returns, the bot "
                        "begins analyzing them one at a time on its own."),
        "args": {"ids": "list[int]"},
        "fn": _tool_investigate_jobs,
    },
    "add_search": {
        "description": "Add a saved LinkedIn search that polls every 30 min for new matches.",
        "args": {"query": "string", "location": "string (optional)"},
        "fn": _tool_add_search,
    },
    "list_searches": {
        "description": "List active saved LinkedIn searches.",
        "args": {},
        "fn": _tool_list_searches,
    },
    "remove_search": {
        "description": "Deactivate a saved search by id.",
        "args": {"id": "int"},
        "fn": _tool_remove_search,
    },
    "get_application_history": {
        "description": "List recent applications the bot has submitted or attempted.",
        "args": {"limit": "int, default 20"},
        "fn": _tool_get_application_history,
    },
    "record_lesson": {
        "description": ("Persist a lesson about the user's lasting preferences/constraints. Use when "
                        "the user states a rule that should apply to all future turns (e.g. 'never crypto', "
                        "'prefer Series A startups', 'always remote'). The lesson is loaded into context "
                        "for every future conversation."),
        "args": {"text": "string", "tags": "list[string] (optional)"},
        "fn": _tool_record_lesson,
    },
    "recall_lessons": {
        "description": "Search recorded lessons by substring. Usually unnecessary — top lessons are pre-loaded into your context.",
        "args": {"query": "string (optional)", "limit": "int, default 30"},
        "fn": _tool_recall_lessons,
    },
    # ─ general-purpose (matches claude-bot's tool names so emoji / styling
    #   map cleanly) ──────────────────────────────────────────────────────
    "WebFetch": {
        "description": ("Fetch a URL via HTTP GET. Use to look at the actual "
                        "job posting page, company about pages, recruiter "
                        "profiles, etc. Returns first ~8KB of body."),
        "args": {"url": "string (http or https)"},
        "fn": _tool_webfetch,
    },
    "Read": {
        "description": ("Read a file from the auto-applier filesystem. Useful "
                        "for inspecting profile.yaml, lessons.jsonl, "
                        "applications.db queries, recent screenshots, etc."),
        "args": {"file_path": "string"},
        "fn": _tool_read,
    },
    "Bash": {
        "description": ("Run an arbitrary shell command from the auto-applier "
                        "repo root. 60s timeout. Use sparingly — the bot runs "
                        "as root on the VPS."),
        "args": {"command": "string"},
        "fn": _tool_bash,
    },
}


# ── Lessons file helpers ────────────────────────────────────────────────────

def _load_lessons(limit: int = LESSONS_PRELOAD_LIMIT) -> list[dict]:
    path = Path(LESSONS_PATH)
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for ln in path.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    except Exception as e:
        logger.warning("lessons load failed: %s", e)
        return []
    return out[-limit:]


# ── Conversation history persistence ────────────────────────────────────────

CREATE_CONVERSATION_TABLE = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tool_name   TEXT,
    iteration   INTEGER NOT NULL DEFAULT 0
);
"""
CREATE_CONVERSATION_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_conv_chat_ts ON conversation_messages(chat_id, ts);"
)

# Tracks whether we've already initialized a Claude-side session for a chat.
# Separate from conversation_messages because the Claude CLI's session
# storage is on its own filesystem; our local message log can outlive an
# uninitialized claude-CLI session (e.g. after an upgrade that introduces
# session resumption like this one).
CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_id     INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Stable namespace UUID (valid v4 format, all hex) so per-chat session IDs
# are deterministic across process restarts.
_SESSION_NAMESPACE = uuid.UUID("3a7c9f28-1c1f-4f2a-9b1a-3373ee2a1d4f")


def _session_id_for_chat(chat_id: int) -> str:
    """Stable per-chat session UUID for `claude --session-id` / `claude --resume`."""
    return str(uuid.uuid5(_SESSION_NAMESPACE, f"auto-applier-chat-{chat_id}"))


async def _get_session_state(db: ApplicationDB, chat_id: int) -> tuple[str, bool]:
    """Return (session_id, is_resume). is_resume=True iff we've successfully
    initialized this session at least once before."""
    sid = _session_id_for_chat(chat_id)
    async with aiosqlite.connect(db._path) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM chat_sessions WHERE chat_id=?",
            (chat_id,),
        )
        row = await cur.fetchone()
    return (sid, row is not None)


async def _mark_session_initialized(db: ApplicationDB, chat_id: int) -> None:
    """Record that a Claude session now exists for this chat, so future
    turns use --resume instead of --session-id."""
    sid = _session_id_for_chat(chat_id)
    async with aiosqlite.connect(db._path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO chat_sessions (chat_id, session_id) VALUES (?, ?)",
            (chat_id, sid),
        )
        await conn.commit()


async def _ensure_table(db: ApplicationDB) -> None:
    """Idempotent — run by the runtime on first turn so a brand-new DB picks up
    the table without needing a migration step. Cheap CREATE IF NOT EXISTS."""
    async with aiosqlite.connect(db._path) as conn:
        await conn.execute(CREATE_CONVERSATION_TABLE)
        await conn.execute(CREATE_CONVERSATION_IDX)
        await conn.execute(CREATE_SESSIONS_TABLE)
        await conn.commit()


async def _append_conversation(
    db: ApplicationDB, chat_id: int, role: str, content: str,
    tool_name: Optional[str] = None, iteration: int = 0,
) -> None:
    async with aiosqlite.connect(db._path) as conn:
        await conn.execute(
            """INSERT INTO conversation_messages (chat_id, role, content, tool_name, iteration)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, role, content, tool_name, iteration),
        )
        await conn.commit()


async def _get_recent_conversation(db: ApplicationDB, chat_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    async with aiosqlite.connect(db._path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT role, content, tool_name, iteration, ts FROM conversation_messages
               WHERE chat_id=? ORDER BY id DESC LIMIT ?""",
            (chat_id, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


# ── Prompt construction ─────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are Jack's job-application assistant on Telegram. Real assistant for a real
job search — not a chatbot demo. Be terse, direct, and decisive. No bullet lists
unless the user explicitly asks. Match the user's energy: if they're brief, be
brief; if they're frustrated, skip pleasantries and act.

You have tools that let you actually DO things: update preferences, dismiss
jobs, queue jobs for investigation, add LinkedIn searches, record lessons that
persist across all future conversations. Use them. Never tell the user "you
should run /prefs roles ..." — just call update_pref and tell them what you did.

When the user says something like "skip all manager roles", call update_pref
with key=exclude_title and value=manager — don't ask for clarification.

Compose multiple tool calls in one turn when the user gives you multiple
intents. Example: "investigate the rest, also add SWE to my roles, never crypto"
is THREE tool calls (investigate_jobs, update_pref, record_lesson) followed by
ONE concise reply.

When the user reveals a lasting preference ("never crypto", "Series A only",
"always remote"), call record_lesson so you remember it permanently. Don't
record one-off opinions — only durable rules.

You also have general-purpose tools — WebFetch, Read, Bash — for cases the
domain tools don't cover. WebFetch a job posting URL to read the description
yourself before deciding. Read profile.yaml or lessons.jsonl to recall facts.
Bash for anything else (queries, scripts). Use them when they actually help;
don't reach for Bash when a domain tool fits.

# Output format

Respond with exactly one JSON object inside <json>...</json> tags. Nothing
outside the tags. No prose, no markdown fences, no commentary.

The shape:

  <json>
  {
    "tool_calls": [
      {"name": "tool_name", "args": {...}}
    ],
    "reply": "natural-language message to send the user, or null if you want to call more tools next iteration"
  }
  </json>

If you have a final answer for the user, set tool_calls to [] and put the
message in reply. If you need to take actions, list them in tool_calls;
results are fed back to you on the next iteration so you can decide whether
to call more tools or finalize with reply. Up to 3 iterations per user turn.
"""


def _render_tool_catalog() -> str:
    lines = ["# Available tools", ""]
    for name, spec in TOOLS.items():
        lines.append(f"- {name}({json.dumps(spec['args'])}): {spec['description']}")
    return "\n".join(lines)


def _render_lessons(lessons: list[dict]) -> str:
    if not lessons:
        return "# What you know about Jack\n\n(no lessons recorded yet)"
    out = ["# What you know about Jack", ""]
    for le in lessons:
        out.append(f"- {le.get('text','')}")
    return "\n".join(out)


def _render_history(history: list[dict], char_budget: int = HISTORY_RENDER_CHAR_BUDGET) -> str:
    """Render history as a transcript, dropping oldest tool rows first to fit budget."""
    rows = list(history)

    def _format(r: dict) -> str:
        role = r["role"]
        content = r["content"] or ""
        if role == "tool":
            tn = r.get("tool_name") or "?"
            if len(content) > 600:
                content = content[:600] + "...(truncated)"
            return f"[tool {tn}] {content}"
        return f"[{role}] {content}"

    rendered = [_format(r) for r in rows]
    blob = "\n".join(rendered)
    while len(blob) > char_budget and rows:
        # Drop the oldest tool row first; if none, drop the oldest message
        tool_idx = next((i for i, r in enumerate(rows) if r["role"] == "tool"), None)
        drop = tool_idx if tool_idx is not None else 0
        rows.pop(drop)
        rendered = [_format(r) for r in rows]
        blob = "\n".join(rendered)
    return "# Conversation history\n\n" + blob


def _build_prompt(history: list[dict], lessons: list[dict], force_json: bool = False) -> str:
    extra = ""
    if force_json:
        extra = ("\n\n# IMPORTANT: Your last reply was not valid JSON inside <json> tags. "
                 "Reply ONLY with the JSON object inside <json>...</json>. No prose outside the tags.")
    return "\n\n".join([
        _SYSTEM_PROMPT,
        _render_tool_catalog(),
        _render_lessons(lessons),
        _render_history(history),
        extra,
    ])


def _build_resume_prompt(history: list[dict]) -> str:
    """Prompt for a resumed Claude session — Claude already has the system
    prompt + tools + prior history in its own session memory, so we only need
    to nudge the JSON output convention and surface any tool results from the
    last iteration.
    """
    # Render only the most recent few rows (current iteration's user msg +
    # any tool results since last assistant turn).
    recent = history[-6:]

    def _format(r: dict) -> str:
        role = r["role"]
        content = r.get("content") or ""
        if role == "tool":
            tn = r.get("tool_name") or "?"
            if len(content) > 600:
                content = content[:600] + "...(truncated)"
            return f"[tool {tn}] {content}"
        return f"[{role}] {content}"

    lines = [_format(r) for r in recent]
    return ("Continue. Reply ONLY with one <json>{...}</json> object as before.\n\n"
            + "\n".join(lines))


# ── Response parsing ────────────────────────────────────────────────────────

_JSON_FENCE = re.compile(r"<json>\s*(\{.*?\})\s*</json>", re.DOTALL | re.IGNORECASE)
_JSON_BARE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_response(raw: str) -> Optional[dict]:
    """Extract a JSON object from Claude's response. Returns None on failure."""
    if not raw:
        return None
    m = _JSON_FENCE.search(raw)
    candidate = m.group(1) if m else None
    if candidate is None:
        # Tolerate Claude omitting the fence — try to find the largest {…} blob
        m2 = _JSON_BARE.search(raw.strip())
        candidate = m2.group(0) if m2 else None
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Normalize shape
    tool_calls = data.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []
    reply = data.get("reply")
    if reply is not None and not isinstance(reply, str):
        reply = str(reply)
    return {"tool_calls": tool_calls, "reply": reply}


# ── Telegram typing keepalive ───────────────────────────────────────────────

async def _typing_keepalive(bot, chat_id: int) -> None:
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(TYPING_KEEPALIVE_SECONDS)
    except asyncio.CancelledError:
        pass


# ── Live status / tool-call visibility ──────────────────────────────────────

def _tool_emoji(name: str, args: dict) -> str:
    """Per-tool emoji prefix.

    Ported verbatim from claude-bot (/opt/claude-bot/src/telegram.ts:toolEmoji)
    so the two bots share identical iconography for any built-in / MCP tools
    they have in common. Auto-applier's domain-specific tools (list_queue,
    update_pref, etc.) get their own mappings appended below.
    """
    if not isinstance(args, dict):
        args = {}
    # Built-in claude tools (verbatim from claude-bot's switch)
    if name == "Bash":
        cmd = args.get("command")
        if isinstance(cmd, str) and re.match(r"^\s*gh(\s|$)", cmd):
            return "🐙"
        return "💻"
    if name == "Read":
        return "📖"
    if name in ("Write", "Edit"):
        return "✏️"
    if name in ("Glob", "Grep"):
        return "🔎"
    if name == "WebFetch":
        return "🌐"
    if name == "WebSearch":
        return "🔍"
    if name == "Task":
        return "🧙"
    if name == "TodoWrite":
        return "📋"
    if name == "AskUserQuestion":
        return "❓"
    if name.startswith("mcp__playwright__"):
        return "🎭"
    if name.startswith("mcp__github__"):
        return "🐙"
    if name.startswith("mcp__"):
        return "🔌"
    # Auto-applier domain tools
    if name in ("list_queue", "list_searches", "get_application_history", "get_prefs"):
        return "📖"
    if name == "update_pref":
        return "✏️"
    if name == "dismiss_jobs":
        return "🗑"
    if name == "investigate_jobs":
        return "🎯"
    if name == "add_search":
        return "➕"
    if name == "remove_search":
        return "➖"
    if name == "record_lesson":
        return "🧠"
    if name == "recall_lessons":
        return "🔖"
    return "🤖"


def _tool_input_preview(name: str, args: dict) -> str:
    """Compact preview of the most relevant arg.

    Ported from claude-bot (/opt/claude-bot/src/telegram.ts:previewToolInput)
    plus auto-applier's domain tools.
    """
    if not isinstance(args, dict):
        return ""
    pick = lambda k: args[k] if isinstance(args.get(k), str) else ""
    # Built-in claude tools
    if name == "Bash":
        return pick("command")
    if name in ("Edit", "Write", "Read"):
        return pick("file_path")
    if name in ("Glob", "Grep"):
        return pick("pattern")
    if name in ("WebFetch", "WebSearch"):
        return pick("url") or pick("query")
    if name == "Task":
        agent_type = pick("subagent_type")
        desc = pick("description")
        if agent_type and desc:
            return f"{agent_type} — {desc}"
        return desc or agent_type
    if "browser_navigate" in name:
        return pick("url")
    if "browser_click" in name:
        return pick("element") or pick("ref")
    if "browser_type" in name or "browser_fill" in name:
        return pick("text") or pick("element")
    if "browser_evaluate" in name:
        return pick("function")[:80]
    # Auto-applier domain tools
    if name == "update_pref":
        key = args.get("key", "")
        value = args.get("value", "")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        return f"{key}={value}" if key else str(value)
    if name == "add_search":
        q = args.get("query", "")
        loc = args.get("location", "")
        return f"{q} in {loc}" if loc else q
    if name == "remove_search":
        return str(args.get("id", ""))
    if name == "dismiss_jobs":
        if args.get("all"):
            return "all"
        ids = args.get("ids", [])
        if isinstance(ids, list) and ids:
            return f"{len(ids)} job{'s' if len(ids) != 1 else ''}"
        return ""
    if name == "investigate_jobs":
        ids = args.get("ids", [])
        if isinstance(ids, list):
            return f"{len(ids)} job{'s' if len(ids) != 1 else ''}"
        return ""
    if name in ("record_lesson", "recall_lessons"):
        return str(args.get("text") or args.get("query") or "")
    if name in ("list_queue", "get_application_history"):
        limit = args.get("limit")
        return f"limit={limit}" if limit else ""
    return ""


def _summarize_args(args: dict) -> str:
    """Fallback arg dump — only used for unknown tools."""
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    if len(s) > 70:
        s = s[:67] + "..."
    return s


def _summarize_result(result: Any) -> str:
    """Compact one-line result preview for the status message."""
    if isinstance(result, dict):
        if "error" in result:
            return f"✗ {str(result['error'])[:60]}"
        if "ok" in result:
            return "✓"
        # Pull a notable scalar if present
        for key in ("count", "dismissed", "investigating", "search_id", "stored"):
            if key in result:
                return f"✓ {key}={result[key]}"
    s = str(result)
    return s[:80] + "..." if len(s) > 80 else s


class _StatusBoard:
    """A single Telegram message edited in-place to show tool calls as they fire.
    Mirrors the streaming tool-call visibility of the user's claude-bot."""

    def __init__(self, bot, chat_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._msg_id: Optional[int] = None
        self._lines: list[str] = []
        self._last_text = ""

    async def open(self, header: str = "thinking…") -> None:
        try:
            msg = await self._bot.send_message(chat_id=self._chat_id, text=header)
            self._msg_id = msg.message_id
            self._lines = [header]
            self._last_text = header
        except Exception as e:
            logger.debug("status open failed: %s", e)

    async def _flush(self) -> None:
        if self._msg_id is None:
            return
        text = "\n".join(self._lines).strip() or "..."
        if text == self._last_text:
            return
        # Telegram has a 4096 char message limit; keep it well under.
        if len(text) > 3500:
            text = text[:3000] + "\n…(truncated, full reply below)"
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id, message_id=self._msg_id, text=text,
            )
            self._last_text = text
        except Exception as e:
            # Telegram returns 400 when the new text is identical to the
            # current — silently fine. Other errors we just log and move on.
            if "not modified" not in str(e).lower():
                logger.debug("status edit failed: %s", e)

    async def begin_call(self, name: str, args: dict) -> int:
        emoji = _tool_emoji(name, args)
        preview = _tool_input_preview(name, args)
        line = f"{emoji} {name}"
        if preview:
            line += f": {preview[:120]}"
        self._lines.append(line)
        await self._flush()
        return len(self._lines) - 1

    async def end_call(self, idx: int, name: str, args: dict, result: Any) -> None:
        if 0 <= idx < len(self._lines):
            emoji = _tool_emoji(name, args)
            preview = _tool_input_preview(name, args)
            line = f"{emoji} {name}"
            if preview:
                line += f": {preview[:120]}"
            line += f" → {_summarize_result(result)}"
            self._lines[idx] = line
            await self._flush()

    async def note(self, line: str) -> None:
        self._lines.append(line)
        await self._flush()

    async def close(self, footer: Optional[str] = None) -> None:
        if self._msg_id is None:
            return
        if footer:
            self._lines.append(footer)
            await self._flush()


# ── Main entry ──────────────────────────────────────────────────────────────

async def run_conversation_turn(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_text: str,
) -> None:
    """Handle a freeform user message via the multi-turn Claude-with-tools loop."""
    chat_id = update.effective_chat.id if update.effective_chat else 0
    db: ApplicationDB = context.bot_data["db"]

    await _ensure_table(db)
    lock = _chat_locks[chat_id]

    # If the bot is already processing for this chat, send a "queued" notice
    # and wait. asyncio.Lock has FIFO wakeup so messages are processed in
    # order naturally — no separate queue datastructure needed.
    if lock.locked():
        try:
            await update.message.reply_text("📋 queued behind your last message…")
        except Exception:
            pass
    await lock.acquire()

    keepalive: Optional[asyncio.Task] = None
    final_reply: Optional[str] = None
    status = _StatusBoard(context.bot, chat_id)

    try:
        await _append_conversation(db, chat_id, "user", user_text)
        keepalive = asyncio.create_task(_typing_keepalive(context.bot, chat_id))
        await status.open(header="thinking…")

        # First call ever for this chat → pin a new claude-CLI session with
        # --session-id and pass the full system prompt + tool catalog. Every
        # subsequent call (including across process restarts) → --resume so
        # Claude retains its server-side context. Same shape as claude-bot's
        # Agent-SDK `resume: sessionId` pattern.
        session_id, is_resume = await _get_session_state(db, chat_id)
        sent_system_already = is_resume

        for it in range(MAX_INNER_ITERS):
            history = await _get_recent_conversation(db, chat_id)
            lessons = _load_lessons()
            if not sent_system_already:
                # Full preamble on the very first call; afterwards we let
                # Claude rely on its own session memory + the iteration's
                # tool-result transcript fed back in.
                prompt = _build_prompt(history, lessons)
                sent_system_already = True
            else:
                prompt = _build_resume_prompt(history)
            try:
                raw = await claude_call(prompt, session_id=session_id, resume=is_resume)
                # Mark session as initialized once first call succeeds, so
                # future turns (this iter or new ones) use --resume.
                if not is_resume:
                    await _mark_session_initialized(db, chat_id)
                    is_resume = True
            except LLMError as e:
                logger.warning("claude_call failed (resume=%s, sid=%s): %s",
                               is_resume, session_id, e)
                # If the session-id was unknown (resume mismatch), wipe our
                # bookkeeping and retry once with --session-id (fresh session).
                if "no such session" in str(e).lower() or "session not found" in str(e).lower() or "no conversation found" in str(e).lower():
                    logger.info("session resume mismatch; restarting session")
                    async with aiosqlite.connect(db._path) as conn:
                        await conn.execute("DELETE FROM chat_sessions WHERE chat_id=?", (chat_id,))
                        await conn.commit()
                    is_resume = False
                    sent_system_already = False
                    continue
                final_reply = "My brain just hiccuped. Send that again?"
                break

            parsed = _parse_response(raw)
            if parsed is None:
                # One re-prompt with stricter instruction
                logger.info("conversation: parse failed iter %d, re-prompting", it)
                try:
                    raw2 = await claude_call(
                        _build_prompt(history, lessons, force_json=True),
                        session_id=session_id, resume=is_resume,
                    )
                except LLMError:
                    final_reply = "My brain just hiccuped. Send that again?"
                    break
                parsed = _parse_response(raw2)
                if parsed is None:
                    logger.warning("conversation: re-prompt also failed; raw=%r", raw2[:300])
                    final_reply = "I got confused. Can you rephrase?"
                    break

            await _append_conversation(
                db, chat_id, "assistant",
                json.dumps(parsed, ensure_ascii=False), iteration=it,
            )

            tool_calls = parsed.get("tool_calls") or []
            reply = parsed.get("reply")

            # If no tool calls, this is the terminal turn.
            if not tool_calls:
                final_reply = reply or "Done."
                break

            # Execute each tool sequentially; persist results as `tool` rows
            # AND stream the call+result into the status message so the user
            # sees what's happening in real time (parity with claude-bot's UX).
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                name = call.get("name")
                args = call.get("args") or {}
                if not isinstance(args, dict):
                    args = {"_raw": args}
                slot = await status.begin_call(name or "?", args)
                if name not in TOOLS:
                    result: dict = {"error": f"unknown tool: {name}",
                                    "available": list(TOOLS.keys())}
                else:
                    try:
                        result = await TOOLS[name]["fn"](context, args)
                    except Exception as e:
                        logger.exception("tool %s raised", name)
                        result = {"error": f"{type(e).__name__}: {e}"}
                await status.end_call(slot, name or "?", args, result)
                logger.info("conversation tool %s args=%s -> %s", name, args, str(result)[:200])
                await _append_conversation(
                    db, chat_id, "tool",
                    json.dumps({"args": args, "result": result}, ensure_ascii=False),
                    tool_name=name, iteration=it,
                )

            # If the model also gave a reply alongside tool calls, surface it
            # immediately so the user gets the running narration. The next
            # iteration (with tool results in history) decides next steps.
            if reply:
                final_reply = reply  # remember last; we'll send only the *final* one below

        else:
            # for-else: ran all iters without breaking. Use whatever reply we
            # last captured, or a fallback.
            if not final_reply:
                final_reply = ("I took several actions but ran out of steps — "
                               "check the result and tell me if you want me to keep going.")

        if final_reply:
            try:
                await update.message.reply_text(final_reply)
            except Exception as e:
                logger.error("failed to send final reply: %s", e)

    finally:
        if keepalive is not None:
            keepalive.cancel()
        lock.release()
