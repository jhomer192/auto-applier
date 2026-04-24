import aiosqlite
from bot.models import ApplicationRecord, EmailThread

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
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
CREATE_IDX_STATUS = "CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);"
CREATE_IDX_SITE = "CREATE INDEX IF NOT EXISTS idx_applications_site ON applications(site);"

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


class ApplicationDB:
    def __init__(self, db_path: str = "data/applications.db") -> None:
        self._path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(CREATE_TABLE)
            await db.execute(CREATE_IDX_STATUS)
            await db.execute(CREATE_IDX_SITE)
            await db.execute(CREATE_EMAIL_TABLE)
            await db.execute(CREATE_EMAIL_IDX)
            await db.commit()

    async def insert_application(self, app: ApplicationRecord) -> int:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """INSERT INTO applications
                   (url, title, company, site, status, submitted_fields,
                    screenshot_path, applied_at, notes, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (app.url, app.title, app.company, app.site, app.status,
                 app.submitted_fields, app.screenshot_path, app.applied_at,
                 app.notes, app.created_at),
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
        created_at=row["created_at"],
    )
