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


# ---------------------------------------------------------------------------
# Robustness against profile entries with missing keys (audit finding #6).
# Previously these crashed with KeyError, silently killing resume generation.
# ---------------------------------------------------------------------------


def test_certification_missing_name_is_skipped_not_raised():
    profile = {
        "work_history": [],
        "projects": [],
        "certifications": [{"issuer": "AWS"}, {"name": "CCNA"}],  # first has no name
        "competitions": [],
    }
    # Must not raise KeyError
    result = _build_experience_context(profile)
    assert "CCNA" in result
    assert "AWS" not in result  # the entry without a name is dropped, not its issuer field


def test_competition_missing_result_renders_name_only():
    profile = {
        "work_history": [],
        "projects": [],
        "certifications": [],
        "competitions": [{"name": "ICPC"}, {"name": "HackMIT", "result": "1st place"}],
    }
    result = _build_experience_context(profile)
    assert "ICPC" in result
    assert "HackMIT (1st place)" in result
    # ICPC should appear without parentheses since no result field
    assert "ICPC (" not in result


def test_project_missing_outcome_and_description_renders_name_only():
    profile = {
        "work_history": [],
        "projects": [{"name": "compiler"}],
        "certifications": [],
        "competitions": [],
    }
    result = _build_experience_context(profile)
    assert "compiler" in result


def test_project_with_no_name_is_skipped():
    profile = {
        "work_history": [],
        "projects": [{"description": "did stuff"}, {"name": "real one"}],
        "certifications": [],
        "competitions": [],
    }
    result = _build_experience_context(profile)
    assert "real one" in result
    assert "did stuff" not in result


def test_all_extras_with_missing_keys_does_not_crash():
    """The audit case: a profile with sparse but partially-filled extras.

    Before the fix this raised KeyError and silently killed resume generation.
    """
    profile = {
        "work_history": [],
        "projects": [{"name": "X"}],
        "certifications": [{"name": "AWS"}, {}],
        "competitions": [{"name": "ICPC"}, {"result": "winner"}],  # second has no name
    }
    result = _build_experience_context(profile)
    assert "X" in result
    assert "AWS" in result
    assert "ICPC" in result
    # The entry with no name in competitions is dropped — "winner" must not appear by itself.
    assert "winner" not in result.replace("ICPC", "")
