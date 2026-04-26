"""Tests for bot.voice and voice-profile injection in bot.llm."""
import asyncio
import os
import yaml
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from bot.voice import load_voice_profile, save_voice_profile, voice_profile_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def mock_subprocess(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


_SAMPLE_VOICE_PROFILE = {
    "tone": "direct and confident",
    "vocabulary_level": "professional",
    "avg_sentence_length": "medium",
    "uses_contractions": True,
    "uses_first_person": True,
    "quirks": [
        "opens with a strong action verb",
        "uses specific numbers and metrics",
        "ends paragraphs with a forward-looking statement",
    ],
    "avoid_phrases": [
        "I am passionate about",
        "leverage",
    ],
    "samples_collected": 3,
    "created_at": "2026-04-26T00:00:00Z",
}

_SAMPLE_JOB_ANALYSIS_DICT = {
    "title": "Software Engineer",
    "company": "Acme",
    "match_score": 85,
    "tailored_summary": "Great fit.",
    "required_skills": ["Python"],
    "preferred_skills": [],
    "key_responsibilities": ["Build APIs"],
    "company_tone": "casual",
    "ats_keywords": ["Python", "REST API"],
    "why_this_role": "Interesting product.",
    "salary_min": 120000,
    "salary_max": 160000,
    "salary_currency": "USD",
    "salary_is_estimated": False,
    "seniority_level": "mid",
    "work_arrangement": "remote",
    "role_type": "software engineer",
    "sponsors_visa": None,
}


def _make_job_analysis():
    from bot.models import JobAnalysis
    return JobAnalysis(**_SAMPLE_JOB_ANALYSIS_DICT)


# ---------------------------------------------------------------------------
# bot.voice unit tests
# ---------------------------------------------------------------------------

def test_load_voice_profile_missing(tmp_path, monkeypatch):
    """Returns None when the voice profile file does not exist."""
    monkeypatch.setenv("VOICE_PROFILE_PATH", str(tmp_path / "nonexistent.yaml"))
    result = load_voice_profile()
    assert result is None


def test_save_and_load_round_trip(tmp_path, monkeypatch):
    """Save a dict then load it back — all keys must match."""
    vp_path = str(tmp_path / "voice_profile.yaml")
    monkeypatch.setenv("VOICE_PROFILE_PATH", vp_path)

    save_voice_profile(_SAMPLE_VOICE_PROFILE)
    loaded = load_voice_profile()

    assert loaded is not None
    assert loaded["tone"] == _SAMPLE_VOICE_PROFILE["tone"]
    assert loaded["vocabulary_level"] == _SAMPLE_VOICE_PROFILE["vocabulary_level"]
    assert loaded["quirks"] == _SAMPLE_VOICE_PROFILE["quirks"]
    assert loaded["avoid_phrases"] == _SAMPLE_VOICE_PROFILE["avoid_phrases"]
    assert loaded["uses_contractions"] == _SAMPLE_VOICE_PROFILE["uses_contractions"]
    assert loaded["uses_first_person"] == _SAMPLE_VOICE_PROFILE["uses_first_person"]
    assert loaded["samples_collected"] == _SAMPLE_VOICE_PROFILE["samples_collected"]


def test_voice_profile_summary_full():
    """Dict with all fields produces expected summary lines."""
    summary = voice_profile_summary(_SAMPLE_VOICE_PROFILE)
    assert "Tone: direct and confident" in summary
    assert "Vocabulary: professional" in summary
    assert "Style markers:" in summary
    assert "opens with a strong action verb" in summary


def test_voice_profile_summary_empty():
    """Empty dict returns the fallback string."""
    summary = voice_profile_summary({})
    assert summary == "Profile captured"


# ---------------------------------------------------------------------------
# bot.llm — voice profile injection
# ---------------------------------------------------------------------------

def test_generate_cover_letter_with_voice_profile(valid_profile):
    """Voice profile section must appear in the prompt sent to claude_call."""
    _, profile = valid_profile
    job = _make_job_analysis()
    captured = []

    def fake_run(args, **kwargs):
        captured.append(kwargs.get("input", ""))
        return mock_subprocess(stdout="Cover letter body.")

    with patch("subprocess.run", side_effect=fake_run):
        from bot.llm import generate_cover_letter
        run(generate_cover_letter(job, profile, voice_profile=_SAMPLE_VOICE_PROFILE))

    assert captured, "subprocess.run was not called"
    prompt = captured[0]
    assert "VOICE PROFILE" in prompt
    assert "direct and confident" in prompt
    assert "opens with a strong action verb" in prompt
    assert "I am passionate about" in prompt  # avoid_phrases must appear


def test_generate_field_answer_with_voice_profile(valid_profile):
    """Voice profile section must appear in the prompt for field answers."""
    _, profile = valid_profile
    job = _make_job_analysis()
    captured = []

    def fake_run(args, **kwargs):
        captured.append(kwargs.get("input", ""))
        return mock_subprocess(stdout="4 years")

    with patch("subprocess.run", side_effect=fake_run):
        from bot.llm import generate_field_answer
        run(generate_field_answer(
            "Years of experience", "Backend role", profile, job,
            voice_profile=_SAMPLE_VOICE_PROFILE,
        ))

    assert captured, "subprocess.run was not called"
    prompt = captured[0]
    assert "VOICE PROFILE" in prompt
    assert "direct and confident" in prompt


def test_generate_cover_letter_without_voice_profile(valid_profile):
    """Backward-compat: voice_profile=None must not inject a voice block."""
    _, profile = valid_profile
    job = _make_job_analysis()
    captured = []

    def fake_run(args, **kwargs):
        captured.append(kwargs.get("input", ""))
        return mock_subprocess(stdout="Cover letter body.")

    with patch("subprocess.run", side_effect=fake_run):
        from bot.llm import generate_cover_letter
        run(generate_cover_letter(job, profile, voice_profile=None))

    assert captured, "subprocess.run was not called"
    prompt = captured[0]
    assert "VOICE PROFILE" not in prompt
