"""Tests for saved-search DB operations, search model, and seen-job deduplication."""
import asyncio
import pytest
from bot.db import ApplicationDB
from bot.models import SavedSearch


@pytest.fixture
def db(tmp_db_path):
    d = ApplicationDB(tmp_db_path)
    asyncio.run(d.init())
    return d


# ── SavedSearch CRUD ───────────────────────────────────────────────────────────

def test_insert_search_returns_id(db):
    s = SavedSearch(query="ML Engineer", location="San Francisco, CA")
    sid = asyncio.run(db.insert_search(s))
    assert sid > 0


def test_get_active_searches_includes_active(db):
    asyncio.run(db.insert_search(SavedSearch(query="Backend Engineer")))
    searches = asyncio.run(db.get_active_searches())
    assert any(s.query == "Backend Engineer" for s in searches)


def test_get_active_searches_excludes_deactivated(db):
    sid = asyncio.run(db.insert_search(SavedSearch(query="Data Scientist")))
    asyncio.run(db.deactivate_search(sid))
    searches = asyncio.run(db.get_active_searches())
    assert not any(s.query == "Data Scientist" for s in searches)


def test_get_all_searches_includes_inactive(db):
    sid = asyncio.run(db.insert_search(SavedSearch(query="DevOps")))
    asyncio.run(db.deactivate_search(sid))
    all_searches = asyncio.run(db.get_all_searches())
    assert any(s.query == "DevOps" for s in all_searches)


def test_touch_search_updates_last_checked(db):
    sid = asyncio.run(db.insert_search(SavedSearch(query="SRE")))
    asyncio.run(db.touch_search(sid, "2025-01-01T00:00:00+00:00"))
    searches = asyncio.run(db.get_all_searches())
    match = next(s for s in searches if s.id == sid)
    assert match.last_checked == "2025-01-01T00:00:00+00:00"


def test_search_stores_location(db):
    asyncio.run(db.insert_search(SavedSearch(query="Product Manager", location="New York, NY")))
    searches = asyncio.run(db.get_active_searches())
    match = next(s for s in searches if s.query == "Product Manager")
    assert match.location == "New York, NY"


# ── Seen-job deduplication ─────────────────────────────────────────────────────

def test_is_job_seen_false_for_new_url(db):
    result = asyncio.run(db.is_job_seen("https://linkedin.com/jobs/view/999"))
    assert result is False


def test_mark_job_seen_makes_is_job_seen_true(db):
    url = "https://linkedin.com/jobs/view/12345"
    sid = asyncio.run(db.insert_search(SavedSearch(query="Test")))
    asyncio.run(db.mark_job_seen(url, sid))
    assert asyncio.run(db.is_job_seen(url)) is True


def test_mark_job_seen_idempotent(db):
    url = "https://linkedin.com/jobs/view/99999"
    sid = asyncio.run(db.insert_search(SavedSearch(query="Test2")))
    asyncio.run(db.mark_job_seen(url, sid))
    asyncio.run(db.mark_job_seen(url, sid))  # second call must not raise
    assert asyncio.run(db.is_job_seen(url)) is True


# ── Cover letter and tailored resume persistence ───────────────────────────────

def test_cover_letter_roundtrip(db):
    from bot.models import ApplicationRecord
    record = ApplicationRecord(
        url="https://example.com/job/cl",
        title="Frontend Engineer",
        company="TechCorp",
        site="greenhouse",
        status="applied",
        cover_letter="Dear Hiring Manager, ...",
    )
    app_id = asyncio.run(db.insert_application(record))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert fetched.cover_letter == "Dear Hiring Manager, ..."


def test_tailored_resume_roundtrip(db):
    from bot.models import ApplicationRecord
    record = ApplicationRecord(
        url="https://example.com/job/tr",
        title="Staff Engineer",
        company="BigCo",
        site="lever",
        status="applied",
        tailored_resume="# Jane Doe\n\n## Summary\n...",
    )
    app_id = asyncio.run(db.insert_application(record))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert "Jane Doe" in fetched.tailored_resume


def test_save_cover_letter_updates_existing(db):
    from bot.models import ApplicationRecord
    app_id = asyncio.run(db.insert_application(ApplicationRecord(
        url="https://example.com/job/upd",
        title="Engineer",
        company="Co",
        site="greenhouse",
        status="applied",
    )))
    asyncio.run(db.save_cover_letter(app_id, "Updated cover letter text."))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert fetched.cover_letter == "Updated cover letter text."


def test_save_tailored_resume_updates_existing(db):
    from bot.models import ApplicationRecord
    app_id = asyncio.run(db.insert_application(ApplicationRecord(
        url="https://example.com/job/tr2",
        title="Engineer",
        company="Co2",
        site="greenhouse",
        status="applied",
    )))
    asyncio.run(db.save_tailored_resume(app_id, "# Tailored Resume"))
    fetched = asyncio.run(db.get_by_id(app_id))
    assert fetched.tailored_resume == "# Tailored Resume"
