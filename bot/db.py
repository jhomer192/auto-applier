import aiosqlite
from bot.models import ApplicationRecord

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


class ApplicationDB:
    def __init__(self, db_path: str = "data/applications.db") -> None:
        self._path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(CREATE_TABLE)
            await db.execute(CREATE_IDX_STATUS)
            await db.execute(CREATE_IDX_SITE)
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
