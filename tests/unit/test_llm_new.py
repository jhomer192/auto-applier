"""Tests for tailor_resume() and extract_achievements() in llm.py."""
import asyncio
import pytest
from unittest.mock import patch, MagicMock

from bot.llm import tailor_resume, extract_achievements, GROUNDING_CONSTRAINT, LLMError
from bot.models import JobAnalysis


def run(coro):
    return asyncio.run(coro)


def mock_subprocess(returncode=0, stdout="output", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def make_job_analysis(**overrides) -> JobAnalysis:
    defaults = dict(
        title="Software Engineer",
        company="Acme Corp",
        match_score=80,
        tailored_summary="Strong match based on Python and distributed systems experience.",
        required_skills=["Python", "Kubernetes"],
        preferred_skills=["Go", "Terraform"],
        key_responsibilities=["Build APIs", "Own deployments"],
        company_tone="technical",
        ats_keywords=["Python", "distributed systems", "CI/CD"],
        why_this_role="First team to own the entire data ingestion pipeline.",
    )
    defaults.update(overrides)
    return JobAnalysis(**defaults)


# ── tailor_resume ──────────────────────────────────────────────────────────────

def test_tailor_resume_returns_output(valid_profile):
    _, profile = valid_profile
    job = make_job_analysis()
    with patch("subprocess.run", return_value=mock_subprocess(stdout="# Jane Doe\n\nSummary...")):
        result = run(tailor_resume(job, profile))
    assert "Jane Doe" in result


def test_tailor_resume_prompt_contains_grounding_constraint(valid_profile):
    _, profile = valid_profile
    job = make_job_analysis()
    captured = []

    def fake_run(args, **kwargs):
        captured.append(args[-1])
        return mock_subprocess(stdout="resume text")

    with patch("subprocess.run", side_effect=fake_run):
        run(tailor_resume(job, profile))

    assert captured, "subprocess.run was not called"
    assert "NEEDS_USER_INPUT" in captured[0]


def test_tailor_resume_prompt_contains_ats_keywords(valid_profile):
    _, profile = valid_profile
    job = make_job_analysis(ats_keywords=["UNIQUE_KEYWORD_XYZ"])
    captured = []

    def fake_run(args, **kwargs):
        captured.append(args[-1])
        return mock_subprocess(stdout="resume")

    with patch("subprocess.run", side_effect=fake_run):
        run(tailor_resume(job, profile))

    assert "UNIQUE_KEYWORD_XYZ" in captured[0]


def test_tailor_resume_raises_llm_error_on_failure(valid_profile):
    _, profile = valid_profile
    job = make_job_analysis()
    with patch("subprocess.run", return_value=mock_subprocess(returncode=1, stderr="fail")):
        with pytest.raises(LLMError):
            run(tailor_resume(job, profile))


# ── extract_achievements ───────────────────────────────────────────────────────

SAMPLE_ACHIEVEMENTS_YAML = """achievements:
  - summary: Built a data pipeline processing 1M events/day.
    impact: Reduced latency by 40%.
    skills: [Python, Kafka]
    context: Acme Corp
"""


def test_extract_achievements_returns_yaml(valid_profile):
    _, profile = valid_profile
    answers = [("What's your biggest win?", "I built a pipeline that processed 1M events/day.")]
    with patch("subprocess.run", return_value=mock_subprocess(stdout=SAMPLE_ACHIEVEMENTS_YAML)):
        result = run(extract_achievements(answers, profile))
    assert "achievements" in result


def test_extract_achievements_prompt_contains_grounding_constraint(valid_profile):
    _, profile = valid_profile
    answers = [("Q1", "A1")]
    captured = []

    def fake_run(args, **kwargs):
        captured.append(args[-1])
        return mock_subprocess(stdout="achievements: []")

    with patch("subprocess.run", side_effect=fake_run):
        run(extract_achievements(answers, profile))

    assert "NEEDS_USER_INPUT" in captured[0]


def test_extract_achievements_prompt_includes_interview_answers(valid_profile):
    _, profile = valid_profile
    answers = [("Best win?", "SPECIAL_ANSWER_TOKEN_42")]
    captured = []

    def fake_run(args, **kwargs):
        captured.append(args[-1])
        return mock_subprocess(stdout="achievements: []")

    with patch("subprocess.run", side_effect=fake_run):
        run(extract_achievements(answers, profile))

    assert "SPECIAL_ANSWER_TOKEN_42" in captured[0]


def test_extract_achievements_empty_answers_list(valid_profile):
    _, profile = valid_profile
    with patch("subprocess.run", return_value=mock_subprocess(stdout="achievements: []")):
        result = run(extract_achievements([], profile))
    assert "achievements" in result
