"""Tests for passive job discovery: job_queue table, stats, batch message formatting."""
import asyncio
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from bot.db import ApplicationDB
from bot.models import ApplicationRecord, QueuedJob
from bot.telegram_bot import _build_batch_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_counter = itertools.count(1)


def _db(tmp_db_path: str) -> ApplicationDB:
    d = ApplicationDB(tmp_db_path)
    asyncio.run(d.init())
    return d


def _app(url: str = None, status: str = "applied", **kwargs) -> ApplicationRecord:
    n = next(_counter)
    defaults = dict(
        url=url or f"https://example.com/job/{n}",
        title="Software Engineer",
        company="Acme",
        site="greenhouse",
        status=status,
    )
    defaults.update(kwargs)
    return ApplicationRecord(**defaults)


# ---------------------------------------------------------------------------
# job_queue CRUD
# ---------------------------------------------------------------------------


def test_enqueue_job_inserts(tmp_db_path):
    db = _db(tmp_db_path)
    inserted = asyncio.run(db.enqueue_job("https://example.com/1", "SWE", "Google"))
    assert inserted is True


def test_enqueue_job_duplicate_ignored(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/dupe", "SWE", "Google"))
    inserted_again = asyncio.run(db.enqueue_job("https://example.com/dupe", "SWE", "Google"))
    assert inserted_again is False


def test_get_pending_queue_returns_pending(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/a", "SWE", "A Corp"))
    asyncio.run(db.enqueue_job("https://example.com/b", "MLE", "B Corp"))
    jobs = asyncio.run(db.get_pending_queue())
    assert len(jobs) == 2


def test_get_pending_queue_excludes_dismissed(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/c", "SWE", "C Corp"))
    asyncio.run(db.enqueue_job("https://example.com/d", "SWE", "D Corp"))
    jobs = asyncio.run(db.get_pending_queue())
    asyncio.run(db.update_queued_job_status(jobs[0].id, "dismissed"))
    remaining = asyncio.run(db.get_pending_queue())
    assert len(remaining) == 1
    assert remaining[0].url == "https://example.com/d"


def test_get_queue_count_empty(tmp_db_path):
    db = _db(tmp_db_path)
    count = asyncio.run(db.get_queue_count())
    assert count == 0


def test_get_queue_count_after_enqueue(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/e", "SWE", "E Corp"))
    asyncio.run(db.enqueue_job("https://example.com/f", "PM", "F Corp"))
    count = asyncio.run(db.get_queue_count())
    assert count == 2


def test_get_queue_count_excludes_dismissed(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/g", "SWE", "G Corp"))
    jobs = asyncio.run(db.get_pending_queue())
    asyncio.run(db.update_queued_job_status(jobs[0].id, "dismissed"))
    assert asyncio.run(db.get_queue_count()) == 0


def test_dismiss_all_queued_returns_count(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/h1", "SWE", "H Corp"))
    asyncio.run(db.enqueue_job("https://example.com/h2", "SWE", "H Corp"))
    n = asyncio.run(db.dismiss_all_queued())
    assert n == 2


def test_dismiss_all_queued_clears_pending(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/i", "SWE", "I Corp"))
    asyncio.run(db.dismiss_all_queued())
    assert asyncio.run(db.get_queue_count()) == 0


def test_queued_job_has_correct_fields(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "ML Engineer", "DeepMind", search_id=42))
    jobs = asyncio.run(db.get_pending_queue())
    assert jobs[0].title == "ML Engineer"
    assert jobs[0].company == "DeepMind"
    assert jobs[0].search_id == 42
    assert jobs[0].status == "pending"


def test_queued_job_oldest_first(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/first", "SWE", "Alpha"))
    asyncio.run(db.enqueue_job("https://example.com/second", "SWE", "Beta"))
    jobs = asyncio.run(db.get_pending_queue())
    assert jobs[0].company == "Alpha"
    assert jobs[1].company == "Beta"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_get_stats_empty(tmp_db_path):
    db = _db(tmp_db_path)
    stats = asyncio.run(db.get_stats())
    assert stats == {}


def test_get_stats_counts_by_status(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.insert_application(_app(status="applied")))
    asyncio.run(db.insert_application(_app(status="applied")))
    asyncio.run(db.insert_application(_app(status="skipped")))
    stats = asyncio.run(db.get_stats())
    assert stats["applied"] == 2
    assert stats["skipped"] == 1


def test_get_stats_with_since_filters(tmp_db_path):
    """Since we can't easily backdate rows, verify since=future returns empty."""
    db = _db(tmp_db_path)
    asyncio.run(db.insert_application(_app(status="applied")))
    future_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    stats = asyncio.run(db.get_stats(since_iso=future_iso))
    assert stats == {}


def test_get_stats_with_since_includes_recent(tmp_db_path):
    """since=past should include current records."""
    db = _db(tmp_db_path)
    asyncio.run(db.insert_application(_app(status="applied")))
    past_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    stats = asyncio.run(db.get_stats(since_iso=past_iso))
    assert stats.get("applied", 0) >= 1


def test_get_top_companies_empty(tmp_db_path):
    db = _db(tmp_db_path)
    result = asyncio.run(db.get_top_companies(limit=5))
    assert result == []


def test_get_top_companies_ranks_correctly(tmp_db_path):
    db = _db(tmp_db_path)
    for _ in range(3):
        asyncio.run(db.insert_application(_app(company="Google", status="applied")))
    for _ in range(2):
        asyncio.run(db.insert_application(_app(company="Stripe", status="applied")))
    asyncio.run(db.insert_application(_app(company="Meta", status="applied")))
    top = asyncio.run(db.get_top_companies(limit=3))
    assert top[0] == ("Google", 3)
    assert top[1] == ("Stripe", 2)
    assert top[2] == ("Meta", 1)


def test_get_top_companies_only_applied(tmp_db_path):
    """Skipped applications should not count toward top companies."""
    db = _db(tmp_db_path)
    asyncio.run(db.insert_application(_app(company="SkippedCo", status="skipped")))
    asyncio.run(db.insert_application(_app(company="AppliedCo", status="applied")))
    top = asyncio.run(db.get_top_companies(limit=5))
    companies = [c for c, _ in top]
    assert "AppliedCo" in companies
    assert "SkippedCo" not in companies


def test_get_top_companies_respects_limit(tmp_db_path):
    db = _db(tmp_db_path)
    for co in ["A", "B", "C", "D", "E", "F"]:
        asyncio.run(db.insert_application(_app(company=co, status="applied")))
    top = asyncio.run(db.get_top_companies(limit=3))
    assert len(top) == 3


# ---------------------------------------------------------------------------
# _build_batch_message
# ---------------------------------------------------------------------------


def _make_queued_jobs(n: int) -> list[QueuedJob]:
    return [
        QueuedJob(url=f"https://example.com/{i}", title=f"Role {i}", company=f"Co {i}", id=i)
        for i in range(1, n + 1)
    ]


def test_build_batch_message_contains_job_titles():
    jobs = _make_queued_jobs(3)
    msg = _build_batch_message(jobs, new_count=3)
    assert "Role 1" in msg
    assert "Role 2" in msg
    assert "Role 3" in msg


def test_build_batch_message_contains_companies():
    jobs = _make_queued_jobs(2)
    msg = _build_batch_message(jobs, new_count=2)
    assert "Co 1" in msg
    assert "Co 2" in msg


def test_build_batch_message_includes_instructions():
    jobs = _make_queued_jobs(2)
    msg = _build_batch_message(jobs)
    assert "skip all" in msg.lower()
    assert "all" in msg.lower()


def test_build_batch_message_numbered_list():
    jobs = _make_queued_jobs(3)
    msg = _build_batch_message(jobs)
    assert "1." in msg
    assert "2." in msg
    assert "3." in msg


def test_build_batch_message_new_count_header():
    jobs = _make_queued_jobs(1)
    msg = _build_batch_message(jobs, new_count=1)
    assert "1 new job" in msg


def test_build_batch_message_plural():
    jobs = _make_queued_jobs(2)
    msg = _build_batch_message(jobs, new_count=2)
    assert "new jobs" in msg


def test_build_batch_message_queue_header_without_new_count():
    jobs = _make_queued_jobs(4)
    msg = _build_batch_message(jobs)
    assert "pending" in msg.lower()


# ---------------------------------------------------------------------------
# QueuedJob model defaults
# ---------------------------------------------------------------------------


def test_queued_job_default_status():
    j = QueuedJob(url="https://example.com", title="SWE", company="Acme")
    assert j.status == "pending"


def test_queued_job_default_search_id_none():
    j = QueuedJob(url="https://example.com", title="SWE", company="Acme")
    assert j.search_id is None


def test_queued_job_has_queued_at():
    j = QueuedJob(url="https://example.com", title="SWE", company="Acme")
    assert j.queued_at is not None
    assert "T" in j.queued_at  # ISO format check
