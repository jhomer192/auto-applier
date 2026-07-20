"""Filter regressions for scripts/source.py.

Every case here is a real posting from the 2026-07-19 wave — the first wave the
rotating sourcer ran. 18 applications went out and 54 sourced jobs were rejected by
the brain after it had already opened them, which is wasted wave time. The rejects
below are the ones that were rejectable from the title alone, plus two location bugs
that let genuinely unreachable jobs through.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from source import classify, is_bay_area, location_ok  # noqa: E402

# Titles the brain opened and rejected on 2026-07-19. Should never reach it again.
REJECTED = [
    "Technical Sourcer (Contract)",
    "Talent Sourcer - Temporary",
    "Executive Sourcer",
    "Technical Talent Sourcer (6-Month Contract)",
    "Customer Success Executive",
    "Customer Success Account Executive",
    "Account Executive PR Communications",
    "Executive IT Support Specialist",
    "Operations Associate Part Time",
    "Expression of Interest: School Sales Representative (Remote, US)",
]

# Titles that DID convert to an application on 2026-07-19. Must keep passing.
APPLIED = [
    ("Enterprise SDR - NorCal", "sdr"),
    ("Sales Development Representative - Bay Area", "sdr"),
    ("Business Development Representative (BDR)", "sdr"),
    ("Junior Sales Representative (SaaS)", "sdr"),
    ("Inside Sales Representative", "sdr"),
    ("Channel BDR", "sdr"),
    ("Compliance Analyst", "security"),
    ("Security Analyst (Security Operations)", "security"),
    ("PM Administrative Assistant", "ops"),
    ("Finance Operations Coordinator", "ops"),
    ("Talent and Operations Coordinator", "ops"),
    ("Technical Support Specialist", "it"),
]


@pytest.mark.parametrize("title", REJECTED)
def test_rejected_titles_are_filtered_at_source(title):
    assert classify(title, None) is None


@pytest.mark.parametrize("title,lane", APPLIED)
def test_applied_titles_still_pass(title, lane):
    assert classify(title, None) == lane


@pytest.mark.parametrize(
    "location,expected",
    [
        # Bare city names that are NOT unique to the Bay Area must not match.
        ("Brisbane", False),          # Brisbane AU — leaked a real BDR application
        ("Albany, NY", False),
        ("Lafayette, LA", False),
        ("Union City, NJ", False),
        ("Richmond, VA", False),
        ("Concord, NH", False),
        ("Belmont, MA", False),
        ("Danville, VA", False),
        ("Saratoga Springs, NY", False),
        # The California originals still match.
        ("Brisbane, CA", True),
        ("Albany, CA", True),
        ("Lafayette, CA", True),
        ("Union City, CA", True),
        ("Richmond, CA", True),
        ("San Francisco, CA", True),
        ("Oakland, CA", True),
        ("Mountain View, CA", True),
    ],
)
def test_ambiguous_city_names(location, expected):
    assert is_bay_area(location) is expected


def test_foreign_board_bare_remote_is_dropped():
    """A UK entity's "Remote" means remote-in-the-UK. Three such applies went out on
    2026-07-19; Zach has US work authorization only, so they were unwinnable."""
    assert not location_ok(
        "Remote", "Security Analyst",
        "https://job-boards.eu.greenhouse.io/nscaleoperationsukltd/jobs/4921843101",
    )


def test_foreign_board_with_explicit_us_location_is_kept():
    """Plenty of EU-HQ companies post genuine US roles on their EU board."""
    assert location_ok(
        "San Francisco", "Business Development Representative",
        "https://job-boards.eu.greenhouse.io/parloa/jobs/4925162101",
    )
    assert location_ok(
        "United States", "Business Development Representative",
        "https://job-boards.eu.greenhouse.io/someco/jobs/1",
    )


@pytest.mark.parametrize(
    "location,title,expected",
    [
        ("Remote - USA", "Sales Development Representative", True),
        ("United States - Remote", "GRC Analyst", True),
        ("New York, NY", "Sales Development Representative", False),
        ("London, UK", "Sales Development Representative", False),
        ("Remote - EMEA", "Sales Development Representative", False),
        ("", "Sales Development Representative", False),
    ],
)
def test_location_gate(location, title, expected):
    assert location_ok(location, title, "https://boards.greenhouse.io/x/jobs/1") is expected
