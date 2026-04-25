"""Tests for profile.load_preferences() and save_preferences()."""
import pytest
import yaml
import tempfile
import os
from bot.profile import load_preferences, save_preferences
from bot.models import JobPreferences


def make_profile(**prefs_overrides) -> dict:
    base = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "555-1234",
        "location": "San Francisco, CA",
        "work_history": [],
        "education": [],
        "skills": ["Python"],
    }
    if prefs_overrides:
        base["job_preferences"] = prefs_overrides
    return base


def test_load_preferences_defaults_when_absent():
    profile = make_profile()
    prefs = load_preferences(profile)
    assert prefs.desired_roles == []
    assert prefs.min_salary == 0
    assert prefs.auto_apply_threshold == 0


def test_load_preferences_desired_roles():
    profile = make_profile(desired_roles=["software engineer", "backend engineer"])
    prefs = load_preferences(profile)
    assert "software engineer" in prefs.desired_roles
    assert "backend engineer" in prefs.desired_roles


def test_load_preferences_salary():
    profile = make_profile(min_salary=180000, target_salary=220000)
    prefs = load_preferences(profile)
    assert prefs.min_salary == 180000
    assert prefs.target_salary == 220000


def test_load_preferences_seniority():
    profile = make_profile(seniority=["senior", "staff"])
    prefs = load_preferences(profile)
    assert "senior" in prefs.seniority
    assert "staff" in prefs.seniority


def test_load_preferences_work_arrangement():
    profile = make_profile(work_arrangement=["remote", "hybrid"])
    prefs = load_preferences(profile)
    assert "remote" in prefs.work_arrangement
    assert "hybrid" in prefs.work_arrangement


def test_load_preferences_excluded_companies():
    profile = make_profile(excluded_companies=["Meta", "Amazon"])
    prefs = load_preferences(profile)
    assert "Meta" in prefs.excluded_companies
    assert "Amazon" in prefs.excluded_companies


def test_load_preferences_auto_apply_threshold():
    profile = make_profile(auto_apply_threshold=85)
    prefs = load_preferences(profile)
    assert prefs.auto_apply_threshold == 85


def test_load_preferences_handles_none_block():
    profile = make_profile()
    profile["job_preferences"] = None
    prefs = load_preferences(profile)
    assert prefs.min_salary == 0


def test_save_preferences_roundtrip(tmp_path):
    profile = make_profile()
    profile_path = str(tmp_path / "profile.yaml")
    with open(profile_path, "w") as f:
        yaml.dump(profile, f)

    prefs = JobPreferences(
        desired_roles=["data scientist"],
        min_salary=150000,
        target_salary=190000,
        seniority=["senior"],
        work_arrangement=["remote"],
        excluded_companies=["BadCorp"],
        auto_apply_threshold=80,
    )
    save_preferences(profile, prefs, profile_path)

    with open(profile_path) as f:
        saved = yaml.safe_load(f)
    jp = saved["job_preferences"]
    assert jp["min_salary"] == 150000
    assert jp["target_salary"] == 190000
    assert "data scientist" in jp["desired_roles"]
    assert "BadCorp" in jp["excluded_companies"]
    assert jp["auto_apply_threshold"] == 80


def test_save_preferences_updates_profile_dict_in_place(tmp_path):
    profile = make_profile()
    profile_path = str(tmp_path / "profile.yaml")
    with open(profile_path, "w") as f:
        yaml.dump(profile, f)

    prefs = JobPreferences(min_salary=200000)
    save_preferences(profile, prefs, profile_path)
    # profile dict should be mutated in-place
    assert profile["job_preferences"]["min_salary"] == 200000


def test_load_preferences_normalises_roles_to_lowercase():
    profile = make_profile(desired_roles=["Software Engineer", "ML ENGINEER"])
    prefs = load_preferences(profile)
    assert "software engineer" in prefs.desired_roles
    assert "ml engineer" in prefs.desired_roles


def test_load_preferences_rate_limit_fields():
    profile = make_profile(min_apply_gap_minutes=6, max_apply_gap_minutes=12, max_applies_per_day=20)
    prefs = load_preferences(profile)
    assert prefs.min_apply_gap_minutes == 6
    assert prefs.max_apply_gap_minutes == 12
    assert prefs.max_applies_per_day == 20


def test_load_preferences_rate_limit_defaults():
    profile = make_profile()
    prefs = load_preferences(profile)
    assert prefs.min_apply_gap_minutes == 4
    assert prefs.max_apply_gap_minutes == 8
    assert prefs.max_applies_per_day == 30


def test_save_preferences_rate_limit_roundtrip(tmp_path):
    import yaml
    profile = make_profile()
    profile_path = str(tmp_path / "profile.yaml")
    with open(profile_path, "w") as f:
        yaml.dump(profile, f)

    prefs = JobPreferences(
        min_apply_gap_minutes=5,
        max_apply_gap_minutes=10,
        max_applies_per_day=15,
    )
    save_preferences(profile, prefs, profile_path)

    with open(profile_path) as f:
        saved = yaml.safe_load(f)
    jp = saved["job_preferences"]
    assert jp["min_apply_gap_minutes"] == 5
    assert jp["max_apply_gap_minutes"] == 10
    assert jp["max_applies_per_day"] == 15
