"""Unit tests for scam-related DB methods."""
import asyncio
import pytest
from bot.db import ApplicationDB


@pytest.fixture
def db(tmp_db_path):
    d = ApplicationDB(tmp_db_path)
    asyncio.run(d.init())
    return d


class TestInsertRejectedJob:
    def test_insert_rejected_job_round_trips(self, db):
        asyncio.run(db.insert_rejected_job(
            url="https://scam.tk/job/1",
            title="Easy Money Rep",
            company="XY LLC",
            scam_score=90,
            signals="URL shortener|Generic contact email",
        ))
        rows = asyncio.run(db.get_rejected_jobs(limit=10))
        assert len(rows) == 1
        row = rows[0]
        assert row["url"] == "https://scam.tk/job/1"
        assert row["title"] == "Easy Money Rep"
        assert row["company"] == "XY LLC"
        assert row["scam_score"] == 90
        assert row["signals"] == "URL shortener|Generic contact email"

    def test_insert_rejected_job_duplicate_ignored(self, db):
        asyncio.run(db.insert_rejected_job(
            url="https://scam.tk/job/2",
            title="Rep",
            company="Co",
            scam_score=85,
            signals="URL shortener",
        ))
        asyncio.run(db.insert_rejected_job(
            url="https://scam.tk/job/2",
            title="Rep",
            company="Co",
            scam_score=85,
            signals="URL shortener",
        ))
        rows = asyncio.run(db.get_rejected_jobs(limit=10))
        assert len(rows) == 1


class TestPendingQueueExcludesFlagged:
    def test_pending_queue_excludes_flagged(self, db):
        asyncio.run(db.enqueue_job(
            url="https://boards.greenhouse.io/co/jobs/flagged",
            title="Flagged Job",
            company="SomeCo",
            scam_score=55,
            scam_flag=1,
            scam_signals="Unknown hosting domain",
        ))
        pending = asyncio.run(db.get_pending_queue())
        assert pending == []

    def test_pending_queue_includes_clean(self, db):
        asyncio.run(db.enqueue_job(
            url="https://boards.greenhouse.io/co/jobs/clean",
            title="Clean Job",
            company="Acme",
            scam_score=10,
            scam_flag=0,
            scam_signals="",
        ))
        pending = asyncio.run(db.get_pending_queue())
        assert len(pending) == 1
        assert pending[0].title == "Clean Job"


class TestFlaggedQueueReturnsFlagged:
    def test_flagged_queue_returns_flagged(self, db):
        asyncio.run(db.enqueue_job(
            url="https://boards.greenhouse.io/co/jobs/suspicious",
            title="Suspicious Role",
            company="SketchyCo",
            scam_score=60,
            scam_flag=1,
            scam_signals="Suspicious language: work from home|Very short job description",
        ))
        flagged = asyncio.run(db.get_flagged_queue())
        assert len(flagged) == 1
        job = flagged[0]
        assert job.title == "Suspicious Role"
        assert job.scam_flag == 1
        assert job.scam_score == 60
        assert "work from home" in job.scam_signals

    def test_flagged_queue_excludes_clean(self, db):
        asyncio.run(db.enqueue_job(
            url="https://boards.greenhouse.io/co/jobs/normalrole",
            title="Normal Role",
            company="NormalCo",
            scam_score=0,
            scam_flag=0,
            scam_signals="",
        ))
        flagged = asyncio.run(db.get_flagged_queue())
        assert flagged == []

    def test_clear_scam_flag_moves_to_pending(self, db):
        asyncio.run(db.enqueue_job(
            url="https://boards.greenhouse.io/co/jobs/borderline",
            title="Borderline Job",
            company="BorderCo",
            scam_score=45,
            scam_flag=1,
            scam_signals="Unknown hosting domain",
        ))
        flagged = asyncio.run(db.get_flagged_queue())
        assert len(flagged) == 1
        job_id = flagged[0].id

        asyncio.run(db.clear_scam_flag(job_id))

        flagged_after = asyncio.run(db.get_flagged_queue())
        assert flagged_after == []

        pending = asyncio.run(db.get_pending_queue())
        assert len(pending) == 1
        assert pending[0].title == "Borderline Job"
