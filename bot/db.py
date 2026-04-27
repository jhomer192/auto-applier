import aiosqlite
import logging
from pathlib import Path
from bot.models import ApplicationRecord, EmailThread, QueuedJob, ReferralCandidate, SavedSearch

logger = logging.getLogger(__name__)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    site            TEXT NOT NULL,
    status          TEXT NOT NULL,
    submitted_fields TEXT NOT NULL DEFAULT '{}',
    screenshot_path TEXT,
    applied_at      TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    cover_letter    TEXT NOT NULL DEFAULT '',
    tailored_resume TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
CREATE_IDX_STATUS = "CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);"
CREATE_IDX_SITE = "CREATE INDEX IF NOT EXISTS idx_applications_site ON applications(site);"

CREATE_SEARCHES_TABLE = """
CREATE TABLE IF NOT EXISTS saved_searches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT NOT NULL,
    location        TEXT NOT NULL DEFAULT '',
    site            TEXT NOT NULL DEFAULT 'linkedin',
    active          INTEGER NOT NULL DEFAULT 1,
    last_checked    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_SEEN_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    search_id   INTEGER REFERENCES saved_searches(id),
    seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
CREATE_SEEN_JOBS_IDX = "CREATE INDEX IF NOT EXISTS idx_seen_jobs_url ON seen_jobs(url);"

CREATE_EMAIL_TABLE = """
CREATE TABLE IF NOT EXISTS email_threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT NOT NULL UNIQUE,
    thread_id       TEXT NOT NULL,
    app_id          INTEGER REFERENCES applications(id),
    from_address    TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body_preview    TEXT NOT NULL DEFAULT '',
    direction       TEXT NOT NULL DEFAULT 'inbound',
    notified        INTEGER NOT NULL DEFAULT 0,
    received_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
CREATE_EMAIL_IDX = "CREATE INDEX IF NOT EXISTS idx_email_thread_id ON email_threads(thread_id);"

CREATE_JOB_QUEUE_TABLE = """
CREATE TABLE IF NOT EXISTS job_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    search_id   INTEGER REFERENCES saved_searches(id),
    queued_at   TEXT NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL DEFAULT 'pending'
);
"""
CREATE_JOB_QUEUE_IDX = "CREATE INDEX IF NOT EXISTS idx_job_queue_status ON job_queue(status);"

CREATE_REJECTED_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS rejected_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    search_id   INTEGER,
    scam_score  INTEGER NOT NULL DEFAULT 0,
    signals     TEXT NOT NULL DEFAULT '',
    rejected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
CREATE_REJECTED_JOBS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_rejected_jobs_at ON rejected_jobs(rejected_at);"
)

CREATE_REFERRAL_CANDIDATES_TABLE = """
CREATE TABLE IF NOT EXISTS referral_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER REFERENCES applications(id),
    name TEXT NOT NULL,
    headline TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '',
    connection_type TEXT DEFAULT '',
    shared_name TEXT DEFAULT '',
    draft_message TEXT DEFAULT '',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""
CREATE_REFERRAL_CANDIDATES_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_referral_candidates_app ON referral_candidates(app_id);"
)


class ApplicationDB:
    def __init__(self, db_path: str = "data/applications.db") -> None:
        self._path = db_path

    async def init(self) -> None:
        # Ensure the directory exists before aiosqlite tries to create the file
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(CREATE_TABLE)
            await db.execute(CREATE_IDX_STATUS)
            await db.execute(CREATE_IDX_SITE)
            await db.execute(CREATE_EMAIL_TABLE)
            await db.execute(CREATE_EMAIL_IDX)
            await db.execute(CREATE_SEARCHES_TABLE)
            await db.execute(CREATE_SEEN_JOBS_TABLE)
            await db.execute(CREATE_SEEN_JOBS_IDX)
            await db.execute(CREATE_JOB_QUEUE_TABLE)
            await db.execute(CREATE_JOB_QUEUE_IDX)
            await db.execute(CREATE_REFERRAL_CANDIDATES_TABLE)
            await db.execute(CREATE_REFERRAL_CANDIDATES_IDX)
            await db.execute(CREATE_REJECTED_JOBS_TABLE)
            await db.execute(CREATE_REJECTED_JOBS_IDX)
            # Migrate existing DBs: add new columns if missing
            for col, defn in [
                ("cover_letter", "TEXT NOT NULL DEFAULT ''"),
                ("tailored_resume", "TEXT NOT NULL DEFAULT ''"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE applications ADD COLUMN {col} {defn}")
                except Exception:
                    logger.debug("Migration: column %r already present, skipping", col)
            # Scam detection + retry-tracking columns on job_queue (idempotent migrations)
            for col, typedef in [
                ("scam_score", "INTEGER NOT NULL DEFAULT 0"),
                ("scam_flag", "INTEGER NOT NULL DEFAULT 0"),
                ("scam_signals", "TEXT NOT NULL DEFAULT ''"),
                # audit fix #3 — distinguish permanent failures from intentional dismissals
                # and allow retrying transient failures.
                ("last_error", "TEXT NOT NULL DEFAULT ''"),
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("last_attempted_at", "TEXT"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE job_queue ADD COLUMN {col} {typedef}")
                except Exception as exc:
                    # Column already exists — log at DEBUG so legitimate migration
                    # errors aren't lost.
                    logger.debug("Migration: job_queue.%s: %s", col, exc)
            await db.commit()

    async def insert_application(self, app: ApplicationRecord) -> int:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """INSERT INTO applications
                   (url, title, company, site, status, submitted_fields,
                    screenshot_path, applied_at, notes, cover_letter, tailored_resume, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (app.url, app.title, app.company, app.site, app.status,
                 app.submitted_fields, app.screenshot_path, app.applied_at,
                 app.notes, app.cover_letter, app.tailored_resume, app.created_at),
            )
            await db.commit()
            return cursor.lastrowid

    async def update_status(self, app_id: int, status: str, notes: str = "") -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE applications SET status=?, notes=? WHERE id=?",
                (status, notes, app_id),
            )
            await db.commit()

    async def get_recent(self, limit: int = 10) -> list[ApplicationRecord]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM applications ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            return [_row_to_record(row) for row in rows]

    async def get_by_id(self, app_id: int) -> ApplicationRecord | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM applications WHERE id=?", (app_id,))
            row = await cursor.fetchone()
            return _row_to_record(row) if row else None


    async def insert_email(self, email: EmailThread) -> int:
        """Insert an inbound email. Returns new row id. Ignores duplicates (by message_id)."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO email_threads
                   (message_id, thread_id, app_id, from_address, subject,
                    body_preview, direction, notified, received_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (email.message_id, email.thread_id, email.app_id, email.from_address,
                 email.subject, email.body_preview, email.direction, 0, email.received_at),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def get_unnotified_emails(self) -> list[EmailThread]:
        """Return inbound emails that haven't been sent to Telegram yet."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM email_threads WHERE direction='inbound' AND notified=0 ORDER BY received_at ASC"
            )
            rows = await cursor.fetchall()
            return [_row_to_email(row) for row in rows]

    async def mark_email_notified(self, email_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("UPDATE email_threads SET notified=1 WHERE id=?", (email_id,))
            await db.commit()

    async def get_email_by_id(self, email_id: int) -> EmailThread | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM email_threads WHERE id=?", (email_id,))
            row = await cursor.fetchone()
            return _row_to_email(row) if row else None

    async def insert_outbound_email(self, thread_id: str, to_address: str, subject: str, body: str) -> None:
        """Record an outbound reply for audit trail."""
        from datetime import datetime, timezone
        async with aiosqlite.connect(self._path) as db:
            import uuid
            msg_id = f"<out-{uuid.uuid4()}@auto-applier>"
            await db.execute(
                """INSERT INTO email_threads
                   (message_id, thread_id, from_address, subject, body_preview, direction, notified, received_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (msg_id, thread_id, to_address, subject, body[:500], "outbound", 1,
                 datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    # --- Saved searches ---

    async def insert_search(self, search: "SavedSearch") -> int:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """INSERT INTO saved_searches (query, location, site, active, created_at)
                   VALUES (?,?,?,?,?)""",
                (search.query, search.location, search.site, int(search.active), search.created_at),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_active_searches(self) -> list["SavedSearch"]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM saved_searches WHERE active=1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            return [_row_to_search(row) for row in rows]

    async def get_all_searches(self) -> list["SavedSearch"]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM saved_searches ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            return [_row_to_search(row) for row in rows]

    async def deactivate_search(self, search_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("UPDATE saved_searches SET active=0 WHERE id=?", (search_id,))
            await db.commit()

    async def touch_search(self, search_id: int, last_checked: str) -> None:
        """Update the last_checked timestamp after a poll."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE saved_searches SET last_checked=? WHERE id=?",
                (last_checked, search_id),
            )
            await db.commit()

    async def is_already_applied(self, url: str) -> bool:
        """Return True if we have a successful application record for this URL."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM applications WHERE url=? AND status='applied' LIMIT 1",
                (url,),
            )
            return await cursor.fetchone() is not None

    async def insert_if_not_applied(self, app: "ApplicationRecord") -> tuple[bool, int]:
        """Atomically check for a prior 'applied' record and insert if none exists.

        Returns (inserted, row_id). inserted=False means a duplicate was found and
        nothing was written. Uses a single connection + BEGIN IMMEDIATE to eliminate
        the TOCTOU race between is_already_applied() and insert_application().
        """
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT 1 FROM applications WHERE url=? AND status='applied' LIMIT 1",
                (app.url,),
            )
            if await cursor.fetchone() is not None:
                await db.execute("ROLLBACK")
                return False, -1
            cursor = await db.execute(
                """INSERT INTO applications
                   (url, title, company, site, status, submitted_fields,
                    screenshot_path, applied_at, notes, cover_letter, tailored_resume, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (app.url, app.title, app.company, app.site, app.status,
                 app.submitted_fields, app.screenshot_path, app.applied_at,
                 app.notes, app.cover_letter, app.tailored_resume, app.created_at),
            )
            await db.commit()
            return True, cursor.lastrowid

    async def is_job_seen(self, url: str) -> bool:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute("SELECT 1 FROM seen_jobs WHERE url=?", (url,))
            return await cursor.fetchone() is not None

    async def mark_job_seen(self, url: str, search_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO seen_jobs (url, search_id) VALUES (?,?)",
                (url, search_id),
            )
            await db.commit()

    async def save_cover_letter(self, app_id: int, cover_letter: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE applications SET cover_letter=? WHERE id=?",
                (cover_letter, app_id),
            )
            await db.commit()

    async def save_tailored_resume(self, app_id: int, tailored_resume: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE applications SET tailored_resume=? WHERE id=?",
                (tailored_resume, app_id),
            )
            await db.commit()

    # --- Job queue ---

    async def enqueue_job(
        self,
        url: str,
        title: str,
        company: str,
        search_id: int | None = None,
        scam_score: int = 0,
        scam_flag: int = 0,
        scam_signals: str = "",
    ) -> bool:
        """Add a job to the review queue. Returns True if newly inserted, False if already present.

        Args:
            url: Job posting URL.
            title: Job title.
            company: Company name.
            search_id: Associated saved search ID, if any.
            scam_score: Heuristic scam confidence score (0-100).
            scam_flag: 1 if job is suspected scam (score 40-79), else 0.
            scam_signals: Pipe-separated list of triggered scam signal names.
        """
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO job_queue
                   (url, title, company, search_id, scam_score, scam_flag, scam_signals)
                   VALUES (?,?,?,?,?,?,?)""",
                (url, title, company, search_id, scam_score, scam_flag, scam_signals),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_pending_queue(self) -> list["QueuedJob"]:
        """Return all pending (un-reviewed) queued jobs excluding scam-flagged, oldest first."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM job_queue WHERE status='pending' AND scam_flag=0 ORDER BY queued_at ASC"
            )
            rows = await cursor.fetchall()
            return [_row_to_queued_job(row) for row in rows]

    async def get_flagged_queue(self) -> list["QueuedJob"]:
        """Return all pending jobs that are scam-flagged (score 40-79), oldest first."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM job_queue WHERE scam_flag=1 AND status='pending' ORDER BY queued_at ASC"
            )
            rows = await cursor.fetchall()
            return [_row_to_queued_job(row) for row in rows]

    async def clear_scam_flag(self, job_id: int) -> None:
        """Remove the scam flag from a queued job so it enters normal processing.

        Args:
            job_id: The job_queue row ID to un-flag.
        """
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE job_queue SET scam_flag=0 WHERE id=?",
                (job_id,),
            )
            await db.commit()

    async def insert_rejected_job(
        self,
        url: str,
        title: str,
        company: str,
        scam_score: int,
        signals: str,
        search_id: int | None = None,
    ) -> None:
        """Record a job that was rejected by the scam detector.

        Args:
            url: Job posting URL (unique key).
            title: Job title.
            company: Company name.
            scam_score: Heuristic scam confidence score (0-100).
            signals: Pipe-separated list of triggered signal names.
            search_id: Associated saved search ID, if any.
        """
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO rejected_jobs
                   (url, title, company, search_id, scam_score, signals)
                   VALUES (?,?,?,?,?,?)""",
                (url, title, company, search_id, scam_score, signals),
            )
            await db.commit()

    async def get_rejected_jobs(self, limit: int = 50) -> list[dict]:
        """Return the most recent rejected jobs, newest first.

        Args:
            limit: Maximum rows to return (bounded; no unbounded queries).

        Returns:
            List of dicts with keys: id, url, title, company, scam_score, signals, rejected_at.
        """
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM rejected_jobs ORDER BY rejected_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_queue_count(self) -> int:
        """Return count of pending jobs in the queue."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM job_queue WHERE status='pending'"
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def update_queued_job_status(self, job_id: int, status: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("UPDATE job_queue SET status=? WHERE id=?", (status, job_id))
            await db.commit()

    async def mark_queued_job_failed(self, job_id: int, error: str) -> None:
        """Record a failure for a queued job (audit fix #3).

        Sets status='failed', stores the error message, increments attempts,
        and stamps last_attempted_at. The job stays in this state until either:
          - retry_failed_jobs() puts it back into 'pending' (if attempts < 3
            and last_attempted_at > 1h ago), or
          - attempts hits the cap of 3, at which point it stays 'failed'
            permanently.
        """
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """UPDATE job_queue
                   SET status='failed',
                       last_error=?,
                       attempts=attempts+1,
                       last_attempted_at=?
                   WHERE id=?""",
                (error[:500], now_iso, job_id),
            )
            await db.commit()

    async def retry_eligible_failed_jobs(self, max_attempts: int = 3, cooldown_hours: int = 1) -> int:
        """Re-queue jobs that failed transiently and deserve another attempt.

        A failed job is eligible if:
          - status='failed' (not 'dismissed' — those were intentional skips), AND
          - attempts < max_attempts, AND
          - last_attempted_at older than cooldown_hours.

        Returns the count of jobs that were moved back to 'pending'.
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).isoformat()
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """UPDATE job_queue
                   SET status='pending'
                   WHERE status='failed'
                     AND attempts < ?
                     AND (last_attempted_at IS NULL OR last_attempted_at < ?)""",
                (max_attempts, cutoff),
            )
            await db.commit()
            return cursor.rowcount

    async def get_failed_jobs(self, retryable_only: bool = False, max_attempts: int = 3) -> list["QueuedJob"]:
        """Return jobs in status='failed', optionally only those eligible for retry."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            if retryable_only:
                cursor = await db.execute(
                    "SELECT * FROM job_queue WHERE status='failed' AND attempts < ? ORDER BY queued_at ASC",
                    (max_attempts,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM job_queue WHERE status='failed' ORDER BY queued_at ASC"
                )
            rows = await cursor.fetchall()
            return [_row_to_queued_job(row) for row in rows]

    async def get_failed_counts(self, max_attempts: int = 3) -> tuple[int, int]:
        """Return (retryable_count, permanent_count) for failed jobs.

        Retryable: attempts < max_attempts. Permanent: attempts >= max_attempts.
        """
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """SELECT
                       SUM(CASE WHEN attempts < ? THEN 1 ELSE 0 END),
                       SUM(CASE WHEN attempts >= ? THEN 1 ELSE 0 END)
                   FROM job_queue WHERE status='failed'""",
                (max_attempts, max_attempts),
            )
            row = await cursor.fetchone()
            if not row:
                return 0, 0
            return (row[0] or 0, row[1] or 0)

    async def dismiss_all_queued(self) -> int:
        """Mark all pending queued jobs as dismissed. Returns count dismissed."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "UPDATE job_queue SET status='dismissed' WHERE status='pending'"
            )
            await db.commit()
            return cursor.rowcount

    # --- Stats ---

    async def get_stats(self, since_iso: str | None = None) -> dict[str, int]:
        """Return {status: count} for applications, optionally filtered by date."""
        async with aiosqlite.connect(self._path) as db:
            if since_iso:
                cursor = await db.execute(
                    "SELECT status, COUNT(*) FROM applications WHERE created_at >= ? GROUP BY status",
                    (since_iso,),
                )
            else:
                cursor = await db.execute(
                    "SELECT status, COUNT(*) FROM applications GROUP BY status"
                )
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

    # --- Referral candidates ---

    async def insert_referral_candidates(self, app_id: int, candidates: list) -> None:
        """Batch-insert referral candidates for an application.

        Args:
            app_id: The application ID to associate candidates with.
            candidates: List of ReferralCandidate instances.
        """
        if not candidates:
            return
        rows = [
            (app_id, c.name, c.headline, c.linkedin_url, c.connection_type, c.shared_name, c.draft_message)
            for c in candidates
        ]
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                """INSERT INTO referral_candidates
                   (app_id, name, headline, linkedin_url, connection_type, shared_name, draft_message)
                   VALUES (?,?,?,?,?,?,?)""",
                rows,
            )
            await db.commit()

    async def get_referral_candidates(self, app_id: int) -> list:
        """Return referral candidates for the given application ID.

        Args:
            app_id: Application ID to look up.

        Returns:
            List of ReferralCandidate instances ordered by insertion order.
        """
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM referral_candidates WHERE app_id=? ORDER BY id ASC",
                (app_id,),
            )
            rows = await cursor.fetchall()
            return [_row_to_referral_candidate(row) for row in rows]

    async def has_referrals(self, app_id: int) -> bool:
        """Return True if any referral candidates exist for the given application ID.

        Args:
            app_id: Application ID to check.
        """
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM referral_candidates WHERE app_id=? LIMIT 1",
                (app_id,),
            )
            return await cursor.fetchone() is not None

    async def get_top_companies(self, limit: int = 5) -> list[tuple[str, int]]:
        """Return [(company, count)] for the top N most-applied companies."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT company, COUNT(*) as cnt FROM applications WHERE status='applied' "
                "GROUP BY company ORDER BY cnt DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [(row[0], row[1]) for row in rows]


def _row_to_referral_candidate(row: aiosqlite.Row) -> "ReferralCandidate":
    return ReferralCandidate(
        id=row["id"],
        app_id=row["app_id"],
        name=row["name"],
        headline=row["headline"] or "",
        linkedin_url=row["linkedin_url"] or "",
        connection_type=row["connection_type"] or "",
        shared_name=row["shared_name"] or "",
        draft_message=row["draft_message"] or "",
        created_at=row["created_at"] or "",
    )


def _row_to_email(row: aiosqlite.Row) -> EmailThread:
    return EmailThread(
        id=row["id"],
        message_id=row["message_id"],
        thread_id=row["thread_id"],
        app_id=row["app_id"],
        from_address=row["from_address"],
        subject=row["subject"],
        body_preview=row["body_preview"],
        direction=row["direction"],
        received_at=row["received_at"],
    )


def _row_to_record(row: aiosqlite.Row) -> ApplicationRecord:
    return ApplicationRecord(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        site=row["site"],
        status=row["status"],
        submitted_fields=row["submitted_fields"],
        screenshot_path=row["screenshot_path"],
        applied_at=row["applied_at"],
        notes=row["notes"],
        cover_letter=row["cover_letter"] if "cover_letter" in row.keys() else "",
        tailored_resume=row["tailored_resume"] if "tailored_resume" in row.keys() else "",
        created_at=row["created_at"],
    )


def _row_to_queued_job(row: aiosqlite.Row) -> "QueuedJob":
    keys = row.keys()
    return QueuedJob(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        search_id=row["search_id"],
        queued_at=row["queued_at"],
        status=row["status"],
        scam_score=row["scam_score"] if "scam_score" in keys else 0,
        scam_flag=row["scam_flag"] if "scam_flag" in keys else 0,
        scam_signals=row["scam_signals"] if "scam_signals" in keys else "",
        last_error=row["last_error"] if "last_error" in keys else "",
        attempts=row["attempts"] if "attempts" in keys else 0,
        last_attempted_at=row["last_attempted_at"] if "last_attempted_at" in keys else None,
    )


def _row_to_search(row: aiosqlite.Row) -> "SavedSearch":
    from bot.models import SavedSearch
    return SavedSearch(
        id=row["id"],
        query=row["query"],
        location=row["location"],
        site=row["site"],
        active=bool(row["active"]),
        last_checked=row["last_checked"],
        created_at=row["created_at"],
    )
