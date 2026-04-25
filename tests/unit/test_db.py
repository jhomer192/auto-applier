import asyncio
import itertools
import aiosqlite
import pytest
from bot.db import ApplicationDB
from bot.models import ApplicationRecord, EmailThread


@pytest.fixture
def db(tmp_db_path):
    d = ApplicationDB(tmp_db_path)
    asyncio.run(d.init())
    return d


_email_counter = itertools.count(1)


def make_email_thread(**overrides) -> EmailThread:
    n = next(_email_counter)
    defaults = dict(
        message_id=f"<msg-{n}@test.example.com>",
        thread_id=f"<thread-{n}@test.example.com>",
        from_address="recruiter@company.com",
        subject="Exciting opportunity",
        body_preview="We'd love to connect.",
        direction="inbound",
    )
    defaults.update(overrides)
    return EmailThread(**defaults)


def make_record(**kwargs) -> ApplicationRecord:
    defaults = dict(
        url="https://example.com/job/1",
        title="Software Engineer",
        company="Acme",
        site="greenhouse",
        status="applied",
    )
    defaults.update(kwargs)
    return ApplicationRecord(**defaults)


def test_insert_returns_id(db):
    record = make_record()
    app_id = asyncio.run(db.insert_application(record))
    assert app_id is not None


def test_get_by_id_title(db):
    record = make_record()
    app_id = asyncio.run(db.insert_application(record))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert fetched.title == "Software Engineer"


def test_get_by_id_company(db):
    record = make_record()
    app_id = asyncio.run(db.insert_application(record))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert fetched.company == "Acme"


def test_get_by_id_status(db):
    record = make_record()
    app_id = asyncio.run(db.insert_application(record))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert fetched.status == "applied"


def test_get_by_id_missing_returns_none(db):
    result = asyncio.run(db.get_by_id(99999))
    assert result is None


def test_update_status_changes_status(db):
    app_id = asyncio.run(db.insert_application(make_record(status="applied")))
    asyncio.run(db.update_status(app_id, "failed", notes="Timed out"))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert fetched.status == "failed"


def test_update_status_stores_notes(db):
    app_id = asyncio.run(db.insert_application(make_record(status="applied")))
    asyncio.run(db.update_status(app_id, "failed", notes="Timed out"))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert fetched.notes == "Timed out"


def test_get_recent_returns_correct_limit(db):
    for i in range(5):
        asyncio.run(db.insert_application(make_record(url=f"https://example.com/{i}", title=f"Job {i}")))
    recent = asyncio.run(db.get_recent(limit=3))
    assert len(recent) == 3


def test_get_recent_orders_by_newest_first(db):
    asyncio.run(db.insert_application(make_record(title="Old Job", url="https://a.com/1")))
    asyncio.run(db.insert_application(make_record(title="New Job", url="https://a.com/2")))
    recent = asyncio.run(db.get_recent(limit=2))
    assert recent[0].title == "New Job"


def test_insert_returns_unique_ids(db):
    id1 = asyncio.run(db.insert_application(make_record(url="https://a.com/1")))
    id2 = asyncio.run(db.insert_application(make_record(url="https://a.com/2")))
    assert id1 != id2


# ── EmailThread tests ─────────────────────────────────────────────────────────

def test_insert_email_returns_id(db):
    email = make_email_thread()
    email_id = asyncio.run(db.insert_email(email))
    assert email_id > 0


def test_insert_email_duplicate_ignored(db):
    email = make_email_thread()
    asyncio.run(db.insert_email(email))
    asyncio.run(db.insert_email(email))
    rows = asyncio.run(db.get_unnotified_emails())
    assert len(rows) == 1


def test_get_unnotified_emails_inbound_only(db):
    inbound = make_email_thread(direction="inbound")
    asyncio.run(db.insert_email(inbound))
    asyncio.run(db.insert_outbound_email(
        thread_id="<outbound-thread@test.example.com>",
        to_address="recruiter@company.com",
        subject="Re: Exciting opportunity",
        body="Thanks for reaching out!",
    ))
    rows = asyncio.run(db.get_unnotified_emails())
    assert len(rows) == 1
    assert rows[0].direction == "inbound"


def test_get_unnotified_emails_excludes_notified(db):
    email1 = make_email_thread()
    email2 = make_email_thread()
    id1 = asyncio.run(db.insert_email(email1))
    asyncio.run(db.insert_email(email2))
    asyncio.run(db.mark_email_notified(id1))
    rows = asyncio.run(db.get_unnotified_emails())
    assert len(rows) == 1
    assert rows[0].message_id == email2.message_id


def test_mark_email_notified(db):
    email = make_email_thread()
    email_id = asyncio.run(db.insert_email(email))
    asyncio.run(db.mark_email_notified(email_id))
    rows = asyncio.run(db.get_unnotified_emails())
    assert rows == []


def test_get_email_by_id_found(db):
    email = make_email_thread(from_address="hr@example.org")
    email_id = asyncio.run(db.insert_email(email))
    fetched = asyncio.run(db.get_email_by_id(email_id))
    assert fetched.from_address == "hr@example.org"


def test_get_email_by_id_missing(db):
    result = asyncio.run(db.get_email_by_id(99999))
    assert result is None


def test_insert_outbound_email_stored(db, tmp_db_path):
    asyncio.run(db.insert_outbound_email(
        thread_id="<thread-out@test.example.com>",
        to_address="recruiter@company.com",
        subject="Re: Let's connect",
        body="Thank you for the opportunity.",
    ))

    async def _check():
        async with aiosqlite.connect(tmp_db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM email_threads WHERE direction='outbound'"
            )
            return await cursor.fetchall()

    rows = asyncio.run(_check())
    assert len(rows) == 1
    assert rows[0]["direction"] == "outbound"
