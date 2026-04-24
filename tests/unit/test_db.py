import asyncio
import pytest
from bot.db import ApplicationDB
from bot.models import ApplicationRecord


@pytest.fixture
def db(tmp_db_path):
    d = ApplicationDB(tmp_db_path)
    asyncio.run(d.init())
    return d


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
