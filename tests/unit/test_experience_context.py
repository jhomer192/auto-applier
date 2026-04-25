"""Tests for _build_experience_context and _months_of_experience in bot.llm."""
from bot.llm import _build_experience_context, _months_of_experience


# ---------------------------------------------------------------------------
# _months_of_experience
# ---------------------------------------------------------------------------

def test_months_of_experience_past_end_date():
    job = {"start": "2022-01", "end": "2023-01"}
    assert _months_of_experience(job) == 12


def test_months_of_experience_present():
    """A job with end='present' should return a positive number of months."""
    job = {"start": "2020-01", "end": "present"}
    result = _months_of_experience(job)
    assert result > 0


def test_months_of_experience_bad_dates_returns_zero():
    job = {"start": "not-a-date", "end": "also-bad"}
    assert _months_of_experience(job) == 0


def test_months_of_experience_short_internship():
    job = {"start": "2023-06", "end": "2023-08"}
    assert _months_of_experience(job) == 2


# ---------------------------------------------------------------------------
# _build_experience_context
# ---------------------------------------------------------------------------

def test_empty_work_history_with_projects_returns_non_empty():
    profile = {
        "work_history": [],
        "projects": [{"name": "FraudNet", "description": "ML fraud detection", "outcome": "98% accuracy"}],
    }
    result = _build_experience_context(profile)
    assert result != ""


def test_three_full_time_roles_no_extras_returns_empty():
    """Experienced candidate with no projects/certs/competitions → empty string."""
    job = {"start": "2019-01", "end": "2022-01"}
    profile = {
        "work_history": [job, job, job],
        "projects": [],
        "certifications": [],
        "competitions": [],
    }
    result = _build_experience_context(profile)
    assert result == ""


def test_context_includes_project_names_and_outcomes():
    profile = {
        "work_history": [],
        "projects": [
            {"name": "FraudNet", "description": "ML model", "outcome": "98% accuracy"},
            {"name": "DataDash", "description": "Dashboard app"},
        ],
    }
    result = _build_experience_context(profile)
    assert "FraudNet" in result
    assert "98% accuracy" in result
    assert "DataDash" in result


def test_context_includes_cert_names():
    profile = {
        "work_history": [],
        "certifications": [{"name": "Security+"}, {"name": "AWS Solutions Architect"}],
        "projects": [{"name": "Dummy", "description": "x"}],
    }
    result = _build_experience_context(profile)
    assert "Security+" in result
    assert "AWS Solutions Architect" in result


def test_context_includes_competition_results():
    profile = {
        "work_history": [],
        "competitions": [{"name": "picoCTF 2024", "result": "Top 5%"}],
        "projects": [{"name": "Dummy", "description": "x"}],
    }
    result = _build_experience_context(profile)
    assert "picoCTF 2024" in result
    assert "Top 5%" in result


def test_context_contains_new_grad_framing_instruction():
    profile = {
        "work_history": [],
        "projects": [{"name": "MyProject", "description": "Something cool"}],
    }
    result = _build_experience_context(profile)
    assert "limited work history" in result
    assert "Do NOT fabricate" in result


def test_no_projects_certs_competitions_returns_empty_even_with_sparse_history():
    """If there is nothing to highlight, return empty regardless of history size."""
    profile = {
        "work_history": [{"start": "2023-01", "end": "2023-06"}],
        "projects": [],
        "certifications": [],
        "competitions": [],
    }
    result = _build_experience_context(profile)
    assert result == ""
