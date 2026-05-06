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

MAX_INNER_ITERS = 25                # effectively unlimited; wall-clock is the real bound
TURN_WALLCLOCK_BUDGET_SEC = 300     # 5 min hard limit per user turn (LLM cost + UX)
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


async def _ensure_table(db: ApplicationDB) -> None:
    """Idempotent — run by the runtime on first turn so a brand-new DB picks up
    the table without needing a migration step. Cheap CREATE IF NOT EXISTS."""
    async with aiosqlite.connect(db._path) as conn:
        await conn.execute(CREATE_CONVERSATION_TABLE)
        await conn.execute(CREATE_CONVERSATION_IDX)
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

    try:
        await asyncio.wait_for(lock.acquire(), timeout=LOCK_ACQUIRE_TIMEOUT)
    except asyncio.TimeoutError:
        await update.message.reply_text("Still working on your last message — give me a sec and try again.")
        return

    keepalive: Optional[asyncio.Task] = None
    final_reply: Optional[str] = None

    try:
        await _append_conversation(db, chat_id, "user", user_text)
        keepalive = asyncio.create_task(_typing_keepalive(context.bot, chat_id))
        turn_started = time.time()

        for it in range(MAX_INNER_ITERS):
            if time.time() - turn_started > TURN_WALLCLOCK_BUDGET_SEC:
                logger.info("conversation: wall-clock budget hit at iter %d", it)
                if not final_reply:
                    final_reply = ("I took a lot of actions but ran out of time on this turn — "
                                   "tell me to keep going if you want more.")
                break
            history = await _get_recent_conversation(db, chat_id)
            lessons = _load_lessons()
            prompt = _build_prompt(history, lessons)
            try:
                raw = await claude_call(prompt)
            except LLMError as e:
                logger.warning("claude_call failed: %s", e)
                final_reply = "My brain just hiccuped. Send that again?"
                break

            parsed = _parse_response(raw)
            if parsed is None:
                # One re-prompt with stricter instruction
                logger.info("conversation: parse failed iter %d, re-prompting", it)
                try:
                    raw2 = await claude_call(_build_prompt(history, lessons, force_json=True))
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

            # Execute each tool sequentially; persist results as `tool` rows.
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                name = call.get("name")
                args = call.get("args") or {}
                if not isinstance(args, dict):
                    args = {"_raw": args}
                if name not in TOOLS:
                    result: dict = {"error": f"unknown tool: {name}",
                                    "available": list(TOOLS.keys())}
                else:
                    try:
                        result = await TOOLS[name]["fn"](context, args)
                    except Exception as e:
                        logger.exception("tool %s raised", name)
                        result = {"error": f"{type(e).__name__}: {e}"}
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
