"""Tests for bot/fit.py: evaluate_fit(), fit_summary_lines(), score_breakdown()."""
import pytest
from bot.fit import evaluate_fit, fit_summary_lines, score_breakdown
from bot.models import FitReport, JobAnalysis, JobPreferences


def make_job(**overrides) -> JobAnalysis:
    defaults = dict(
        title="Senior Software Engineer",
        company="Acme Corp",
        match_score=82,
        tailored_summary="Strong match.",
        role_type="software engineer",
        seniority_level="senior",
        work_arrangement="remote",
        salary_min=160000,
        salary_max=200000,
        salary_currency="USD",
        salary_is_estimated=False,
    )
    defaults.update(overrides)
    return JobAnalysis(**defaults)


def make_prefs(**overrides) -> JobPreferences:
    defaults = dict(
        desired_roles=["software engineer"],
        min_salary=150000,
        target_salary=200000,
        seniority=["senior", "staff"],
        work_arrangement=["remote", "hybrid"],
        excluded_companies=[],
        auto_apply_threshold=0,
    )
    defaults.update(overrides)
    return JobPreferences(**defaults)


# ── Salary checks ──────────────────────────────────────────────────────────────

def test_salary_ok_when_above_floor():
    job = make_job(salary_max=200000)
    prefs = make_prefs(min_salary=150000)
    report = evaluate_fit(job, prefs)
    assert report.salary_ok is True


def test_salary_warn_when_below_floor():
    job = make_job(salary_min=100000, salary_max=130000)
    prefs = make_prefs(min_salary=150000)
    report = evaluate_fit(job, prefs)
    assert report.salary_ok is False
    assert "⚠️" in report.salary_note


def test_salary_hard_pass_when_drastically_below_floor_and_not_estimated():
    job = make_job(salary_min=80000, salary_max=100000, salary_is_estimated=False)
    prefs = make_prefs(min_salary=150000)
    report = evaluate_fit(job, prefs)
    assert report.hard_pass is True
    assert report.salary_ok is False


def test_no_hard_pass_when_salary_estimated_even_if_low():
    # Estimated salary shouldn't trigger hard pass — it might be wrong
    job = make_job(salary_min=80000, salary_max=100000, salary_is_estimated=True)
    prefs = make_prefs(min_salary=150000)
    report = evaluate_fit(job, prefs)
    assert report.hard_pass is False
    assert report.salary_ok is False  # still a soft warn


def test_no_salary_check_when_no_floor_set():
    job = make_job(salary_max=50000)
    prefs = make_prefs(min_salary=0)
    report = evaluate_fit(job, prefs)
    assert report.salary_ok is True
    assert report.hard_pass is False


def test_no_salary_check_when_job_has_no_salary():
    job = make_job(salary_min=0, salary_max=0)
    prefs = make_prefs(min_salary=150000)
    report = evaluate_fit(job, prefs)
    # Can't evaluate — not a hard pass
    assert report.hard_pass is False
    assert "not posted" in report.salary_note


# ── Role type checks ───────────────────────────────────────────────────────────

def test_role_ok_when_matches_desired():
    job = make_job(role_type="software engineer")
    prefs = make_prefs(desired_roles=["software engineer", "backend engineer"])
    report = evaluate_fit(job, prefs)
    assert report.role_ok is True


def test_role_warn_when_no_match():
    job = make_job(role_type="marketing manager")
    prefs = make_prefs(desired_roles=["software engineer"])
    report = evaluate_fit(job, prefs)
    assert report.role_ok is False
    assert "⚠️" in report.role_note


def test_role_ok_when_no_preference_set():
    job = make_job(role_type="anything")
    prefs = make_prefs(desired_roles=[])
    report = evaluate_fit(job, prefs)
    assert report.role_ok is True


def test_role_partial_match_accepted():
    # "senior software engineer" contains "software engineer"
    job = make_job(role_type="senior software engineer")
    prefs = make_prefs(desired_roles=["software engineer"])
    report = evaluate_fit(job, prefs)
    assert report.role_ok is True


# ── Seniority checks ───────────────────────────────────────────────────────────

def test_seniority_ok_when_matches():
    job = make_job(seniority_level="senior")
    prefs = make_prefs(seniority=["senior", "staff"])
    report = evaluate_fit(job, prefs)
    assert report.seniority_ok is True


def test_seniority_warn_when_no_match():
    job = make_job(seniority_level="junior")
    prefs = make_prefs(seniority=["senior", "staff"])
    report = evaluate_fit(job, prefs)
    assert report.seniority_ok is False
    assert "⚠️" in report.seniority_note


def test_seniority_ok_when_unknown_level():
    # Can't evaluate unknown — don't penalise
    job = make_job(seniority_level="unknown")
    prefs = make_prefs(seniority=["senior"])
    report = evaluate_fit(job, prefs)
    assert report.seniority_ok is True


def test_seniority_ok_when_no_preference():
    job = make_job(seniority_level="junior")
    prefs = make_prefs(seniority=[])
    report = evaluate_fit(job, prefs)
    assert report.seniority_ok is True


# ── Work arrangement checks ────────────────────────────────────────────────────

def test_arrangement_ok_when_matches():
    job = make_job(work_arrangement="remote")
    prefs = make_prefs(work_arrangement=["remote", "hybrid"])
    report = evaluate_fit(job, prefs)
    assert report.arrangement_ok is True


def test_arrangement_warn_when_no_match():
    job = make_job(work_arrangement="onsite")
    prefs = make_prefs(work_arrangement=["remote"])
    report = evaluate_fit(job, prefs)
    assert report.arrangement_ok is False
    assert "⚠️" in report.arrangement_note


def test_arrangement_ok_when_unknown():
    job = make_job(work_arrangement="unknown")
    prefs = make_prefs(work_arrangement=["remote"])
    report = evaluate_fit(job, prefs)
    assert report.arrangement_ok is True


# ── Excluded companies ─────────────────────────────────────────────────────────

def test_excluded_company_hard_pass():
    job = make_job(company="BadCorp")
    prefs = make_prefs(excluded_companies=["BadCorp"])
    report = evaluate_fit(job, prefs)
    assert report.hard_pass is True
    assert report.excluded_company is True


def test_excluded_company_case_insensitive():
    job = make_job(company="badcorp")
    prefs = make_prefs(excluded_companies=["BadCorp"])
    report = evaluate_fit(job, prefs)
    assert report.hard_pass is True


def test_not_excluded_when_different_company():
    job = make_job(company="GoodCorp")
    prefs = make_prefs(excluded_companies=["BadCorp"])
    report = evaluate_fit(job, prefs)
    assert report.excluded_company is False
    assert report.hard_pass is False


# ── Auto-apply ─────────────────────────────────────────────────────────────────

def test_auto_apply_triggered_when_all_pass_and_score_meets_threshold():
    job = make_job(match_score=90)
    prefs = make_prefs(auto_apply_threshold=85)
    report = evaluate_fit(job, prefs)
    assert report.auto_apply is True
    assert report.hard_pass is False


def test_auto_apply_not_triggered_below_threshold():
    job = make_job(match_score=80)
    prefs = make_prefs(auto_apply_threshold=85)
    report = evaluate_fit(job, prefs)
    assert report.auto_apply is False


def test_auto_apply_not_triggered_when_threshold_zero():
    job = make_job(match_score=100)
    prefs = make_prefs(auto_apply_threshold=0)
    report = evaluate_fit(job, prefs)
    assert report.auto_apply is False


def test_auto_apply_not_triggered_when_soft_warn_exists():
    # Even at high score, if work_arrangement fails, no auto-apply
    job = make_job(match_score=95, work_arrangement="onsite")
    prefs = make_prefs(auto_apply_threshold=80, work_arrangement=["remote"])
    report = evaluate_fit(job, prefs)
    assert report.auto_apply is False


# ── fit_summary_lines ──────────────────────────────────────────────────────────

def test_fit_summary_includes_salary_line():
    job = make_job(salary_min=160000, salary_max=200000)
    prefs = make_prefs(min_salary=150000)
    report = evaluate_fit(job, prefs)
    lines = fit_summary_lines(job, report)
    assert any("160k" in l or "200k" in l or "Salary" in l for l in lines)


def test_fit_summary_includes_arrangement_when_known():
    job = make_job(work_arrangement="remote")
    prefs = make_prefs(work_arrangement=[])
    report = evaluate_fit(job, prefs)
    lines = fit_summary_lines(job, report)
    assert any("remote" in l.lower() for l in lines)


def test_fit_summary_empty_when_no_notable_info():
    job = make_job(
        salary_min=0, salary_max=0,
        work_arrangement="unknown",
        seniority_level="unknown",
    )
    prefs = make_prefs(
        desired_roles=[], seniority=[], work_arrangement=[], min_salary=0
    )
    report = evaluate_fit(job, prefs)
    lines = fit_summary_lines(job, report)
    # Should be empty or nearly empty — nothing of note to surface
    assert all("unknown" not in l.lower() for l in lines)


# ── score_breakdown ────────────────────────────────────────────────────────────

def test_score_breakdown_contains_score():
    job = make_job(match_score=72, required_skills=["Python", "SQL"], preferred_skills=["Docker"])
    prefs = make_prefs()
    result = score_breakdown(job, prefs)
    assert "72/100" in result


def test_score_breakdown_contains_required_skills():
    job = make_job(required_skills=["Python", "SQL", "Kubernetes"])
    prefs = make_prefs()
    result = score_breakdown(job, prefs)
    assert "Python" in result
    assert "SQL" in result


def test_score_breakdown_contains_preferred_skills():
    job = make_job(preferred_skills=["Docker", "Terraform"])
    prefs = make_prefs()
    result = score_breakdown(job, prefs)
    assert "Docker" in result


def test_score_breakdown_verdict_excellent():
    job = make_job(match_score=95)
    assert "Excellent" in score_breakdown(job, make_prefs())


def test_score_breakdown_verdict_strong():
    job = make_job(match_score=80)
    assert "Strong" in score_breakdown(job, make_prefs())


def test_score_breakdown_verdict_decent():
    job = make_job(match_score=62)
    assert "Decent" in score_breakdown(job, make_prefs())


def test_score_breakdown_verdict_weak():
    job = make_job(match_score=50)
    assert "Weak" in score_breakdown(job, make_prefs())


def test_score_breakdown_verdict_poor():
    job = make_job(match_score=30)
    assert "Poor" in score_breakdown(job, make_prefs())


def test_score_breakdown_empty_skills_no_crash():
    """Should not crash when required/preferred skills are empty."""
    job = make_job(match_score=60, required_skills=[], preferred_skills=[])
    result = score_breakdown(job, make_prefs())
    assert "60/100" in result


def test_score_breakdown_caps_shown_skills():
    """Should not dump all 10+ skills into the breakdown — cap at 6 required, 4 preferred."""
    job = make_job(
        required_skills=["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"],
        preferred_skills=["Papa", "Quebec", "Romeo", "Sierra", "Tango"],
    )
    result = score_breakdown(job, make_prefs())
    # Capped at 6 required → Golf and Hotel should not appear
    assert "Golf" not in result
    assert "Hotel" not in result
    # Capped at 4 preferred → Tango should not appear
    assert "Tango" not in result
