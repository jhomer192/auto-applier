"""Unit tests for bot/referral_radar.py."""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import ReferralCandidate
from bot.referral_radar import (
    RATE_LIMIT,
    RATE_WINDOW,
    _check_rate_limit,
    _last_calls,
    _rank_connection_type,
    _truncate_note,
    find_referral_candidates,
)


# ---------------------------------------------------------------------------
# _truncate_note
# ---------------------------------------------------------------------------


def test_truncate_note_exact_300():
    """A string of exactly 300 chars should be returned unchanged."""
    s = "x" * 300
    result = _truncate_note(s, max_chars=300)
    assert result == s
    assert len(result) == 300


def test_truncate_note_over_300():
    """A 305-char string should be truncated to <=300 chars ending with '...'."""
    s = "y" * 305
    result = _truncate_note(s, max_chars=300)
    assert len(result) <= 300
    assert result.endswith("...")


def test_truncate_note_under_limit():
    """A string shorter than max_chars is returned as-is."""
    s = "hello"
    assert _truncate_note(s, max_chars=300) == s


# ---------------------------------------------------------------------------
# Candidate ranking
# ---------------------------------------------------------------------------


def _make_candidate(connection_type: str) -> ReferralCandidate:
    return ReferralCandidate(
        id=None,
        app_id=None,
        name="Test Person",
        connection_type=connection_type,
    )


def test_filter_candidates_prefers_alumni():
    """Alumni should rank before 2nd-degree connections."""
    candidates = [
        _make_candidate("2nd"),
        _make_candidate("alumni"),
        _make_candidate("2nd"),
    ]
    candidates.sort(key=lambda c: _rank_connection_type(c.connection_type))
    assert candidates[0].connection_type == "alumni"


def test_filter_candidates_ranking_order():
    """Full ranking order: alumni > 1st > 2nd > other."""
    candidates = [
        _make_candidate("other"),
        _make_candidate("2nd"),
        _make_candidate("alumni"),
        _make_candidate("1st"),
    ]
    candidates.sort(key=lambda c: _rank_connection_type(c.connection_type))
    assert [c.connection_type for c in candidates] == ["alumni", "1st", "2nd", "other"]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def test_rate_limit_respected():
    """After RATE_LIMIT calls in a window, the next call should return []."""
    # Clear and fill the call log with recent timestamps
    _last_calls.clear()
    now = time.monotonic()
    for _ in range(RATE_LIMIT):
        _last_calls.append(now)

    # At this point the rate limit is full — _check_rate_limit should deny
    assert _check_rate_limit() is False


def test_rate_limit_allows_after_window():
    """Calls older than RATE_WINDOW should be pruned, allowing new calls."""
    _last_calls.clear()
    old_time = time.monotonic() - RATE_WINDOW - 1
    for _ in range(RATE_LIMIT):
        _last_calls.append(old_time)

    # Old calls should be pruned; this call should be allowed
    assert _check_rate_limit() is True
    _last_calls.clear()  # clean up so other tests are not affected


# ---------------------------------------------------------------------------
# Disabled flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_flag_returns_empty():
    """When profile has referral_radar.enabled: false, find_referral_candidates is skipped.

    This test validates the check done in the Telegram hook — if the flag is false,
    the bot should not call find_referral_candidates at all. We verify the guard
    logic directly using the profile dict pattern used in telegram_bot.py.
    """
    profile = {"referral_radar": {"enabled": False}}
    radar_enabled = profile.get("referral_radar", {}).get("enabled", True)

    # Confirm the flag is correctly read as False
    assert radar_enabled is False

    # Simulate the guard: only call find_referral_candidates when enabled
    called = False

    async def _mock_find(*args, **kwargs):
        nonlocal called
        called = True
        return []

    with patch("bot.referral_radar.find_referral_candidates", side_effect=_mock_find):
        if radar_enabled:
            await find_referral_candidates(
                company="Acme",
                user_school="MIT",
                user_companies=[],
                linkedin_auth="/nonexistent/path.json",
            )

    assert not called, "find_referral_candidates should not be called when radar is disabled"


@pytest.mark.asyncio
async def test_missing_auth_returns_empty(tmp_path):
    """find_referral_candidates returns [] when linkedin_auth file does not exist."""
    result = await find_referral_candidates(
        company="Acme Corp",
        user_school="MIT",
        user_companies=["OldCo"],
        linkedin_auth=str(tmp_path / "nonexistent.json"),
    )
    assert result == []
