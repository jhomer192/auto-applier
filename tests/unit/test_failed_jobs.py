"""Tests for audit fix #3 — distinguishing 'failed' from 'dismissed' on the job queue
and retrying failed jobs after a cooldown.

Before this fix, both adapter exceptions AND result.success==False set the queued job
to status='dismissed'. Failures were indistinguishable from intentional skips, so the
bot never retried — every transient adapter glitch silently lost a job forever.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from bot.auto_apply import process_queued_jobs
from bot.db import ApplicationDB
from bot.models import JobInfo


def _db(path: str) -> ApplicationDB:
    d = ApplicationDB(path)
    asyncio.run(d.init())
    return d


# --------------------------------------------------------------------------- #
# DB-layer behaviour
# --------------------------------------------------------------------------- #


def test_mark_queued_job_failed_sets_status_and_increments(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j1", "Eng", "Acme"))
    pending = asyncio.run(db.get_pending_queue())
    job_id = pending[0].id

    asyncio.run(db.mark_queued_job_failed(job_id, "boom"))

    failed = asyncio.run(db.get_failed_jobs())
    assert len(failed) == 1
    assert failed[0].id == job_id
    assert failed[0].status == "failed"
    assert failed[0].attempts == 1
    assert failed[0].last_error == "boom"
    assert failed[0].last_attempted_at is not None


def test_mark_queued_job_failed_truncates_long_errors(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "Eng", "Acme"))
    job_id = asyncio.run(db.get_pending_queue())[0].id
    asyncio.run(db.mark_queued_job_failed(job_id, "x" * 10_000))
    failed = asyncio.run(db.get_failed_jobs())
    assert len(failed[0].last_error) <= 500


def test_failed_jobs_dont_appear_in_pending_queue(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "Eng", "Acme"))
    job_id = asyncio.run(db.get_pending_queue())[0].id
    asyncio.run(db.mark_queued_job_failed(job_id, "err"))

    assert asyncio.run(db.get_pending_queue()) == []


def test_retry_eligible_failed_jobs_requeues_after_cooldown(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "Eng", "Acme"))
    job_id = asyncio.run(db.get_pending_queue())[0].id
    asyncio.run(db.mark_queued_job_failed(job_id, "transient"))

    # Backdate last_attempted_at to 2 hours ago so the cooldown has expired
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    async def _backdate():
        async with aiosqlite.connect(tmp_db_path) as conn:
            await conn.execute(
                "UPDATE job_queue SET last_attempted_at=? WHERE id=?",
                (past, job_id),
            )
            await conn.commit()
    asyncio.run(_backdate())

    moved = asyncio.run(db.retry_eligible_failed_jobs())
    assert moved == 1

    pending = asyncio.run(db.get_pending_queue())
    assert len(pending) == 1
    assert pending[0].id == job_id
    # attempts is preserved across the requeue
    assert pending[0].attempts == 1


def test_retry_eligible_skips_jobs_inside_cooldown(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "Eng", "Acme"))
    job_id = asyncio.run(db.get_pending_queue())[0].id
    asyncio.run(db.mark_queued_job_failed(job_id, "transient"))

    # last_attempted_at is "now" — within the 1h cooldown
    moved = asyncio.run(db.retry_eligible_failed_jobs())
    assert moved == 0
    assert asyncio.run(db.get_pending_queue()) == []


def test_retry_eligible_skips_jobs_at_attempt_cap(tmp_db_path):
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "Eng", "Acme"))
    job_id = asyncio.run(db.get_pending_queue())[0].id

    # Three attempts, all backdated past cooldown
    async def _seed():
        for _ in range(3):
            await db.mark_queued_job_failed(job_id, "boom")
        async with aiosqlite.connect(tmp_db_path) as conn:
            await conn.execute(
                "UPDATE job_queue SET last_attempted_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), job_id),
            )
            await conn.commit()
    asyncio.run(_seed())

    moved = asyncio.run(db.retry_eligible_failed_jobs())
    assert moved == 0  # attempts == 3, hits the cap

    failed = asyncio.run(db.get_failed_jobs())
    assert failed[0].attempts == 3


def test_get_failed_counts_splits_retryable_and_permanent(tmp_db_path):
    db = _db(tmp_db_path)
    # Three jobs, varying attempt counts
    for url in ["https://a/1", "https://a/2", "https://a/3"]:
        asyncio.run(db.enqueue_job(url, "Eng", "Acme"))

    pending = asyncio.run(db.get_pending_queue())
    # job 1: 1 attempt (retryable), job 2: 3 attempts (permanent), job 3: 0 (still pending)
    asyncio.run(db.mark_queued_job_failed(pending[0].id, "e"))
    for _ in range(3):
        asyncio.run(db.mark_queued_job_failed(pending[1].id, "e"))

    retryable, permanent = asyncio.run(db.get_failed_counts())
    assert retryable == 1
    assert permanent == 1


def test_dismissed_jobs_are_never_retried(tmp_db_path):
    """Dismissed jobs (hard-pass, etc.) must NOT come back via retry_eligible_failed_jobs."""
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "Eng", "Acme"))
    job_id = asyncio.run(db.get_pending_queue())[0].id
    asyncio.run(db.update_queued_job_status(job_id, "dismissed"))

    moved = asyncio.run(db.retry_eligible_failed_jobs())
    assert moved == 0
    assert asyncio.run(db.get_pending_queue()) == []


# --------------------------------------------------------------------------- #
# auto_apply integration — adapter exceptions + unsuccessful submits
# --------------------------------------------------------------------------- #


def _make_app(db: ApplicationDB, registry):
    from bot.profile import load_preferences

    profile = {
        "name": "Jane",
        "email": "jane@example.com",
        "phone": "555-0000",
        "resume_path": "/tmp/resume.pdf",
        "job_preferences": {"auto_apply_threshold": 50},
    }
    bot = MagicMock()
    bot.send_message = AsyncMock()

    app = SimpleNamespace(
        bot=bot,
        bot_data={
            "db": db,
            "profile": profile,
            "registry": registry,
            "authorized_user_id": 12345,
            "screenshot_dir": "/tmp",
        },
    )
    return app


class _FakeAdapter:
    """Raises during submit_application — the case audit fix #3 targets."""
    name = "greenhouse"

    async def fetch_job_info(self, url):
        return JobInfo(url=url, title="Eng", company="Acme", raw_html="<html></html>")

    async def extract_fields(self, url):
        from bot.models import FormField
        return [FormField(label="Name", field_type="text", required=True, selector="#name")]

    async def submit_application(self, url, fields, resume_path):
        raise RuntimeError("network blew up mid-submit")


class _FakeRegistry:
    def __init__(self, adapter):
        self._adapter = adapter

    def get(self, url):
        return self._adapter


def test_adapter_exception_marks_failed_not_dismissed(tmp_db_path):
    """Audit case: when adapter.submit_application raises, the job should land in
    'failed' status (not 'dismissed'), with the error message recorded.
    """
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://greenhouse.io/test", "Eng", "Acme"))

    app = _make_app(db, _FakeRegistry(_FakeAdapter()))

    fit_mock = SimpleNamespace(hard_pass=False, hard_pass_reason="", auto_apply=True)

    with patch("bot.auto_apply.evaluate_fit", return_value=fit_mock), \
         patch("bot.auto_apply.analyze_job", new=AsyncMock(return_value={"score": 90})), \
         patch("bot.auto_apply.tailor_resume", new=AsyncMock(return_value="resume")), \
         patch("bot.auto_apply.generate_cover_letter", new=AsyncMock(return_value="cl")), \
         patch("bot.auto_apply.generate_field_answer", new=AsyncMock(return_value="Jane")), \
         patch("bot.auto_apply.field_answer_hint", return_value=""), \
         patch("bot.auto_apply.fit_summary_lines", return_value=[]), \
         patch("bot.auto_apply.score_breakdown", return_value=""), \
         patch("bot.auto_apply.load_voice_profile", return_value=None):
        asyncio.run(process_queued_jobs(app, linkedin_auth=""))

    failed = asyncio.run(db.get_failed_jobs())
    assert len(failed) == 1, "adapter exception should produce exactly one failed job"
    assert failed[0].status == "failed"
    assert failed[0].attempts == 1
    assert "network blew up" in failed[0].last_error


def test_failed_job_re_queued_after_cooldown(tmp_db_path):
    """Once cooldown elapses, the failed job should come back into pending."""
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "Eng", "Acme"))
    job_id = asyncio.run(db.get_pending_queue())[0].id
    asyncio.run(db.mark_queued_job_failed(job_id, "transient"))

    # Backdate to past the cooldown
    async def _backdate():
        async with aiosqlite.connect(tmp_db_path) as conn:
            await conn.execute(
                "UPDATE job_queue SET last_attempted_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), job_id),
            )
            await conn.commit()
    asyncio.run(_backdate())

    moved = asyncio.run(db.retry_eligible_failed_jobs())
    assert moved == 1

    pending = asyncio.run(db.get_pending_queue())
    assert len(pending) == 1
    assert pending[0].attempts == 1  # attempt counter survives the requeue


def test_failed_job_permanent_after_three_attempts(tmp_db_path):
    """After 3 failed attempts the job should NOT be re-queued."""
    db = _db(tmp_db_path)
    asyncio.run(db.enqueue_job("https://example.com/j", "Eng", "Acme"))
    job_id = asyncio.run(db.get_pending_queue())[0].id

    async def _three_failures():
        for _ in range(3):
            await db.mark_queued_job_failed(job_id, "boom")
        async with aiosqlite.connect(tmp_db_path) as conn:
            await conn.execute(
                "UPDATE job_queue SET last_attempted_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), job_id),
            )
            await conn.commit()
    asyncio.run(_three_failures())

    moved = asyncio.run(db.retry_eligible_failed_jobs())
    assert moved == 0
    retryable, permanent = asyncio.run(db.get_failed_counts())
    assert retryable == 0
    assert permanent == 1
