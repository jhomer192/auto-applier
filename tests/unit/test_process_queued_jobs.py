"""Integration tests for process_queued_jobs — the autonomous apply pipeline.

All Playwright browser activity, LLM calls, and Telegram sends are mocked.
Tests verify routing decisions (hard-pass, needs-review, auto-apply) and
DB state transitions without submitting a real application.
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.auto_apply import process_queued_jobs
from bot.db import ApplicationDB
from bot.models import (
    ApplicationRecord,
    ApplicationResult,
    FitReport,
    FormField,
    JobAnalysis,
    JobInfo,
    QueuedJob,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def _make_db(tmp_path) -> ApplicationDB:
    db = ApplicationDB(str(tmp_path / "test.db"))
    run(db.init())
    return db


def _profile(auto_apply_threshold: int = 0, excluded_companies: list[str] | None = None) -> dict:
    return {
        "name": "Jane Smith",
        "email": "jane@example.com",
        "phone": "555-0000",
        "location": "San Francisco, CA",
        "resume_path": "/tmp/resume.pdf",
        "work_history": [],
        "education": [],
        "skills": ["Python"],
        "job_preferences": {
            "desired_roles": ["Software Engineer"],
            "auto_apply_threshold": auto_apply_threshold,
            "excluded_companies": excluded_companies or [],
            "auto_search": True,
        },
    }


def _queued_job(url: str = "https://boards.greenhouse.io/acme/jobs/1/application",
                title: str = "Backend Engineer",
                company: str = "Acme Corp") -> QueuedJob:
    return QueuedJob(url=url, title=title, company=company, search_id=None)


def _job_info(title: str = "Backend Engineer", company: str = "Acme Corp",
              url: str = "https://boards.greenhouse.io/acme/jobs/1/application") -> JobInfo:
    return JobInfo(title=title, company=company, url=url, raw_html="<html>job</html>")


def _job_analysis(match_score: int = 85, hard_pass: bool = False) -> JobAnalysis:
    return JobAnalysis(
        title="Backend Engineer",
        company="Acme Corp",
        match_score=match_score,
        tailored_summary="Great fit",
    )


def _fit(hard_pass: bool = False, auto_apply: bool = True) -> FitReport:
    return FitReport(
        hard_pass=hard_pass,
        hard_pass_reason="excluded company" if hard_pass else "",
        auto_apply=auto_apply,
    )


def _make_app(db: ApplicationDB, profile: dict, adapter=None) -> MagicMock:
    """Build a minimal PTB Application mock with the bot_data structure process_queued_jobs expects."""
    registry = MagicMock()
    registry.get.return_value = adapter

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()

    app = MagicMock()
    app.bot = bot
    app.bot_data = {
        "db": db,
        "profile": profile,
        "registry": registry,
        "authorized_user_id": 12345,
        "screenshot_dir": "/tmp/screenshots",
    }
    return app


def _make_adapter(job_info=None, analysis=None, fields=None, result=None):
    """Build a mock adapter for the given scenario."""
    adapter = MagicMock()
    adapter.name = "greenhouse"
    adapter.fetch_job_info = AsyncMock(return_value=job_info or _job_info())
    adapter.extract_fields = AsyncMock(return_value=fields or [])
    adapter.submit_application = AsyncMock(return_value=result or ApplicationResult(
        success=True,
        screenshot_path=None,
        submitted_fields={"First Name": "Jane"},
        error=None,
        submission_confirmed=True,
    ))
    return adapter


# ---------------------------------------------------------------------------
# Tests — no pending jobs
# ---------------------------------------------------------------------------

def test_empty_queue_returns_immediately(tmp_path):
    db = _make_db(tmp_path)
    app = _make_app(db, _profile())
    run(process_queued_jobs(app, "auth.json"))
    app.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — hard-pass routing
# ---------------------------------------------------------------------------

def test_hard_pass_job_is_dismissed(tmp_path):
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    adapter = _make_adapter()
    profile = _profile(excluded_companies=["Acme Corp"])
    app = _make_app(db, profile, adapter=adapter)

    with patch("bot.auto_apply.analyze_job", AsyncMock(return_value=_job_analysis())), \
         patch("bot.auto_apply.evaluate_fit", return_value=_fit(hard_pass=True)):
        run(process_queued_jobs(app, "auth.json"))

    # Queue entry should be dismissed
    pending = run(db.get_pending_queue())
    assert len(pending) == 0

    # A "skipped" record should exist in applications
    history = run(db.get_recent(limit=10))
    assert any(r.status == "skipped" for r in history)


def test_hard_pass_does_not_send_notification(tmp_path):
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    adapter = _make_adapter()
    app = _make_app(db, _profile(), adapter=adapter)

    with patch("bot.auto_apply.analyze_job", AsyncMock(return_value=_job_analysis())), \
         patch("bot.auto_apply.evaluate_fit", return_value=_fit(hard_pass=True)):
        run(process_queued_jobs(app, "auth.json"))

    # No Telegram message for silently-dismissed hard-pass
    app.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — needs-review routing (auto-apply disabled or score too low)
# ---------------------------------------------------------------------------

def test_low_score_goes_to_batch_review(tmp_path):
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    adapter = _make_adapter()
    app = _make_app(db, _profile(auto_apply_threshold=0), adapter=adapter)

    with patch("bot.auto_apply.analyze_job", AsyncMock(return_value=_job_analysis(match_score=60))), \
         patch("bot.auto_apply.evaluate_fit", return_value=_fit(auto_apply=False)), \
         patch("bot.auto_apply._build_batch_message", return_value="Batch review"):
        run(process_queued_jobs(app, "auth.json"))

    # Job stays pending (not dismissed/applied)
    pending = run(db.get_pending_queue())
    assert len(pending) == 1

    # Batch review message sent
    app.bot.send_message.assert_called_once()
    call_kwargs = app.bot.send_message.call_args
    assert "Batch review" in str(call_kwargs)


def test_threshold_zero_always_routes_to_review(tmp_path):
    """auto_apply_threshold=0 means always ask the user, regardless of fit score."""
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    adapter = _make_adapter()
    app = _make_app(db, _profile(auto_apply_threshold=0), adapter=adapter)

    # High score, would auto-apply if threshold were set
    with patch("bot.auto_apply.analyze_job", AsyncMock(return_value=_job_analysis(match_score=95))), \
         patch("bot.auto_apply.evaluate_fit", return_value=_fit(auto_apply=False)), \
         patch("bot.auto_apply._build_batch_message", return_value="Review needed"):
        run(process_queued_jobs(app, "auth.json"))

    pending = run(db.get_pending_queue())
    assert len(pending) == 1  # still pending, not auto-applied


# ---------------------------------------------------------------------------
# Tests — fetch/analyze failures route to review
# ---------------------------------------------------------------------------

def test_fetch_failure_routes_to_review(tmp_path):
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    adapter = _make_adapter()
    adapter.fetch_job_info = AsyncMock(side_effect=RuntimeError("timeout"))
    app = _make_app(db, _profile(), adapter=adapter)

    with patch("bot.auto_apply._build_batch_message", return_value="Review"):
        run(process_queued_jobs(app, "auth.json"))

    # Job stays in pending queue (fetch failed, routed to review)
    pending = run(db.get_pending_queue())
    assert len(pending) == 1


def test_analyze_failure_routes_to_review(tmp_path):
    from bot.llm import LLMError
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    adapter = _make_adapter()
    app = _make_app(db, _profile(), adapter=adapter)

    with patch("bot.auto_apply.analyze_job", AsyncMock(side_effect=LLMError("bad json"))), \
         patch("bot.auto_apply._build_batch_message", return_value="Review"):
        run(process_queued_jobs(app, "auth.json"))

    pending = run(db.get_pending_queue())
    assert len(pending) == 1


# ---------------------------------------------------------------------------
# Tests — already-applied dedup
# ---------------------------------------------------------------------------

def test_already_applied_url_is_dismissed(tmp_path):
    db = _make_db(tmp_path)
    url = "https://boards.greenhouse.io/acme/jobs/1/application"

    # Pre-seed an "applied" record for this URL
    run(db.insert_application(ApplicationRecord(
        url=url, title="Old", company="Acme Corp", site="greenhouse",
        status="applied", applied_at="2025-01-01T00:00:00",
    )))

    job = _queued_job(url=url)
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    adapter = _make_adapter()
    app = _make_app(db, _profile(), adapter=adapter)

    run(process_queued_jobs(app, "auth.json"))

    # Queue entry dismissed, no Telegram message
    pending = run(db.get_pending_queue())
    assert len(pending) == 0
    app.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — no adapter registered for URL
# ---------------------------------------------------------------------------

def test_no_adapter_dismisses_job(tmp_path):
    db = _make_db(tmp_path)
    job = _queued_job(url="https://unknown-site.example/job/42")
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    # registry.get returns None (no adapter)
    app = _make_app(db, _profile(), adapter=None)

    run(process_queued_jobs(app, "auth.json"))

    pending = run(db.get_pending_queue())
    assert len(pending) == 0
    app.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — auto-apply happy path (success)
# ---------------------------------------------------------------------------

def test_auto_apply_success_records_application(tmp_path):
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    fields = [
        FormField(label="First Name", field_type="text", required=True, selector="#first"),
        FormField(label="Email", field_type="text", required=True, selector="#email"),
    ]
    adapter = _make_adapter(fields=fields)
    app = _make_app(db, _profile(auto_apply_threshold=80), adapter=adapter)

    with patch("bot.auto_apply.analyze_job", AsyncMock(return_value=_job_analysis(match_score=90))), \
         patch("bot.auto_apply.evaluate_fit", return_value=_fit(auto_apply=True)), \
         patch("bot.auto_apply.tailor_resume", AsyncMock(return_value="tailored resume text")), \
         patch("bot.auto_apply.generate_cover_letter", AsyncMock(return_value="cover letter text")), \
         patch("bot.auto_apply.generate_field_answer", AsyncMock(return_value="Jane")):
        run(process_queued_jobs(app, "auth.json"))

    # Application record inserted with "applied" status
    history = run(db.get_recent(limit=10))
    assert any(r.status == "applied" for r in history)

    # Telegram notification sent (success msg comes before interview prep msg)
    app.bot.send_message.assert_called()
    all_texts = [c[1].get("text", "") for c in app.bot.send_message.call_args_list]
    assert any("Auto-applied" in t for t in all_texts)


def test_auto_apply_success_dismisses_queue_entry(tmp_path):
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    fields = [
        FormField(label="First Name", field_type="text", required=True, selector="#first"),
    ]
    adapter = _make_adapter(fields=fields)
    app = _make_app(db, _profile(auto_apply_threshold=80), adapter=adapter)

    with patch("bot.auto_apply.analyze_job", AsyncMock(return_value=_job_analysis(match_score=90))), \
         patch("bot.auto_apply.evaluate_fit", return_value=_fit(auto_apply=True)), \
         patch("bot.auto_apply.tailor_resume", AsyncMock(return_value="")), \
         patch("bot.auto_apply.generate_cover_letter", AsyncMock(return_value="")), \
         patch("bot.auto_apply.generate_field_answer", AsyncMock(return_value="Jane")):
        run(process_queued_jobs(app, "auth.json"))

    pending = run(db.get_pending_queue())
    assert len(pending) == 0


# ---------------------------------------------------------------------------
# Tests — auto-apply with blocking field → routes to review
# ---------------------------------------------------------------------------

def test_blocking_field_routes_to_review(tmp_path):
    """If LLM returns NEEDS_USER_INPUT for a required field, job goes to review."""
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    fields = [
        FormField(label="Cover Letter", field_type="textarea", required=True, selector="#cl"),
    ]
    adapter = _make_adapter(fields=fields)
    app = _make_app(db, _profile(auto_apply_threshold=80), adapter=adapter)

    with patch("bot.auto_apply.analyze_job", AsyncMock(return_value=_job_analysis(match_score=90))), \
         patch("bot.auto_apply.evaluate_fit", return_value=_fit(auto_apply=True)), \
         patch("bot.auto_apply.tailor_resume", AsyncMock(return_value="")), \
         patch("bot.auto_apply.generate_cover_letter", AsyncMock(return_value="")), \
         patch("bot.auto_apply.generate_field_answer", AsyncMock(return_value="NEEDS_USER_INPUT:Cover Letter")), \
         patch("bot.auto_apply._build_batch_message", return_value="Review message"):
        run(process_queued_jobs(app, "auth.json"))

    # Job still pending — not auto-applied
    pending = run(db.get_pending_queue())
    assert len(pending) == 1

    # Batch review message sent
    app.bot.send_message.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — submit failure
# ---------------------------------------------------------------------------

def test_submit_failure_records_failed_application(tmp_path):
    db = _make_db(tmp_path)
    job = _queued_job()
    run(db.enqueue_job(job.url, job.title, job.company, job.search_id))

    failed_result = ApplicationResult(
        success=False,
        screenshot_path=None,
        submitted_fields={},
        error="Form timed out",
    )
    fields = [
        FormField(label="First Name", field_type="text", required=True, selector="#first"),
    ]
    adapter = _make_adapter(fields=fields, result=failed_result)
    app = _make_app(db, _profile(auto_apply_threshold=80), adapter=adapter)

    with patch("bot.auto_apply.analyze_job", AsyncMock(return_value=_job_analysis(match_score=90))), \
         patch("bot.auto_apply.evaluate_fit", return_value=_fit(auto_apply=True)), \
         patch("bot.auto_apply.tailor_resume", AsyncMock(return_value="")), \
         patch("bot.auto_apply.generate_cover_letter", AsyncMock(return_value="")), \
         patch("bot.auto_apply.generate_field_answer", AsyncMock(return_value="Jane")):
        run(process_queued_jobs(app, "auth.json"))

    history = run(db.get_recent(limit=10))
    assert any(r.status == "failed" for r in history)

    # Failure notification sent
    app.bot.send_message.assert_called()
    text = app.bot.send_message.call_args_list[-1][1].get("text", "")
    assert "Auto-apply failed" in text or "failed" in text.lower()


# ---------------------------------------------------------------------------
# Tests — multiple jobs in same cycle
# ---------------------------------------------------------------------------

def test_processes_at_most_five_per_cycle(tmp_path):
    """Queue has 7 jobs but only 5 are processed per cycle."""
    db = _make_db(tmp_path)
    for i in range(7):
        j = _queued_job(url=f"https://boards.greenhouse.io/acme/jobs/{i}/application")
        run(db.enqueue_job(j.url, j.title, j.company, j.search_id))

    app = _make_app(db, _profile())  # adapter=None → all dismissed

    run(process_queued_jobs(app, "auth.json"))

    # 5 dismissed, 2 still pending
    pending = run(db.get_pending_queue())
    assert len(pending) == 2


def test_mix_hard_pass_and_review_in_same_cycle(tmp_path):
    """Hard-pass and review jobs coexist: hard-pass dismissed, review batched."""
    db = _make_db(tmp_path)

    excluded_url = "https://boards.greenhouse.io/badco/jobs/1/application"
    normal_url = "https://boards.greenhouse.io/goodco/jobs/2/application"

    j1 = _queued_job(url=excluded_url, company="BadCo")
    j2 = _queued_job(url=normal_url, company="GoodCo")
    run(db.enqueue_job(j1.url, j1.title, j1.company, j1.search_id))
    run(db.enqueue_job(j2.url, j2.title, j2.company, j2.search_id))

    def fit_side_effect(analysis, prefs):
        if analysis.company == "BadCo":
            return FitReport(hard_pass=True, hard_pass_reason="excluded")
        return FitReport(hard_pass=False, auto_apply=False)

    adapter = _make_adapter()
    adapter.fetch_job_info = AsyncMock(side_effect=lambda url: JobInfo(
        title="Engineer",
        company="BadCo" if "badco" in url else "GoodCo",
        url=url,
        raw_html="<html/>",
    ))

    registry = MagicMock()
    registry.get.return_value = adapter

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()

    app = MagicMock()
    app.bot = bot
    app.bot_data = {
        "db": db,
        "profile": _profile(),
        "registry": registry,
        "authorized_user_id": 12345,
        "screenshot_dir": "/tmp",
    }

    # analyze_job returns alternating companies matching the fetch order
    analyze_calls = [0]

    async def analyze_side_effect(html, profile):
        # Jobs are processed in insertion order: BadCo first, GoodCo second
        companies = ["BadCo", "GoodCo"]
        company = companies[analyze_calls[0] % 2]
        analyze_calls[0] += 1
        return JobAnalysis(title="Engineer", company=company, match_score=70, tailored_summary="ok")

    with patch("bot.auto_apply.analyze_job", side_effect=analyze_side_effect), \
         patch("bot.auto_apply.evaluate_fit", side_effect=fit_side_effect), \
         patch("bot.auto_apply._build_batch_message", return_value="batch"):
        run(process_queued_jobs(app, "auth.json"))

    # Only GoodCo job remains pending
    pending = run(db.get_pending_queue())
    assert len(pending) == 1
    assert pending[0].company == "GoodCo"

    # BadCo has a skipped record
    history = run(db.get_recent(limit=10))
    assert any(r.status == "skipped" and r.company == "BadCo" for r in history)
