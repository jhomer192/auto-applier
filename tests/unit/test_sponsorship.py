"""Tests for visa sponsorship detection and fit evaluation (Tasks 2-3)."""
import pytest
from bot.fit import evaluate_fit, fit_summary_lines
from bot.models import FitReport, JobAnalysis, JobPreferences


def make_job(**overrides) -> JobAnalysis:
    defaults = dict(
        title="Software Engineer",
        company="Acme Corp",
        match_score=80,
        tailored_summary="Strong match.",
        role_type="software engineer",
        seniority_level="mid",
        work_arrangement="remote",
        salary_min=0,
        salary_max=0,
        salary_currency="USD",
        salary_is_estimated=False,
        sponsors_visa=None,
    )
    defaults.update(overrides)
    return JobAnalysis(**defaults)


def make_prefs(**overrides) -> JobPreferences:
    defaults = dict(
        desired_roles=[],
        min_salary=0,
        target_salary=0,
        seniority=[],
        work_arrangement=[],
        excluded_companies=[],
        auto_apply_threshold=0,
        requires_sponsorship=False,
    )
    defaults.update(overrides)
    return JobPreferences(**defaults)


# ── Hard pass: needs sponsorship, job explicitly says no ───────────────────────

def test_hard_pass_when_needs_sponsorship_and_job_says_no():
    job = make_job(sponsors_visa=False)
    prefs = make_prefs(requires_sponsorship=True)
    report = evaluate_fit(job, prefs)
    assert report.hard_pass is True
    assert "visa sponsorship" in report.hard_pass_reason.lower()


# ── Soft warn: needs sponsorship, posting silent on sponsorship ────────────────

def test_sponsorship_warning_when_not_mentioned():
    job = make_job(sponsors_visa=None)
    prefs = make_prefs(requires_sponsorship=True)
    report = evaluate_fit(job, prefs)
    assert report.hard_pass is False
    assert report.sponsorship_ok is False
    assert "verify" in report.sponsorship_note.lower()


# ── All clear: needs sponsorship, job explicitly sponsors ─────────────────────

def test_no_sponsorship_issue_when_job_sponsors():
    job = make_job(sponsors_visa=True)
    prefs = make_prefs(requires_sponsorship=True)
    report = evaluate_fit(job, prefs)
    assert report.hard_pass is False
    assert report.sponsorship_ok is True
    assert report.sponsorship_note == ""


# ── User doesn't need sponsorship: never penalised ────────────────────────────

def test_no_sponsorship_penalty_when_user_does_not_need_it():
    job = make_job(sponsors_visa=False)
    prefs = make_prefs(requires_sponsorship=False)
    report = evaluate_fit(job, prefs)
    assert report.hard_pass is False
    assert report.sponsorship_ok is True


# ── fit_summary_lines ──────────────────────────────────────────────────────────

def test_fit_summary_includes_warning_when_not_mentioned():
    job = make_job(sponsors_visa=None)
    prefs = make_prefs(requires_sponsorship=True)
    report = evaluate_fit(job, prefs)
    lines = fit_summary_lines(job, report, prefs)
    assert any("verify" in l.lower() for l in lines)


def test_fit_summary_shows_confirmed_when_job_sponsors():
    job = make_job(sponsors_visa=True)
    prefs = make_prefs(requires_sponsorship=True)
    report = evaluate_fit(job, prefs)
    lines = fit_summary_lines(job, report, prefs)
    assert any("confirmed" in l.lower() for l in lines)


def test_fit_summary_no_sponsorship_line_when_user_does_not_need_it():
    job = make_job(sponsors_visa=True)
    prefs = make_prefs(requires_sponsorship=False)
    report = evaluate_fit(job, prefs)
    lines = fit_summary_lines(job, report, prefs)
    assert not any("sponsor" in l.lower() for l in lines)


def test_fit_summary_no_sponsorship_line_when_prefs_not_passed():
    """Legacy callers that omit prefs param should see no sponsorship lines."""
    job = make_job(sponsors_visa=None)
    prefs = make_prefs(requires_sponsorship=True)
    report = evaluate_fit(job, prefs)
    lines = fit_summary_lines(job, report)  # prefs omitted
    assert not any("sponsor" in l.lower() for l in lines)


# ── Auto-apply gated by sponsorship_ok ────────────────────────────────────────

def test_auto_apply_blocked_when_sponsorship_unconfirmed():
    job = make_job(match_score=95, sponsors_visa=None)
    prefs = make_prefs(requires_sponsorship=True, auto_apply_threshold=80)
    report = evaluate_fit(job, prefs)
    assert report.auto_apply is False


def test_auto_apply_allowed_when_sponsorship_confirmed():
    job = make_job(match_score=95, sponsors_visa=True)
    prefs = make_prefs(requires_sponsorship=True, auto_apply_threshold=80)
    report = evaluate_fit(job, prefs)
    assert report.auto_apply is True
