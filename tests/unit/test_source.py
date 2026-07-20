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


def test_foreign_board_gate_only_tightens():
    """The foreign-host check must ADD a requirement, never replace the ordinary one.
    An early `return` on a US marker let a StubHub LA+NYC role through on the strength
    of "United States" alone — a location the normal Bay/remote-US rule rejects."""
    assert not location_ok(
        "Los Angeles, California, United States; New York, New York, United States",
        "People Operations Partner",
        "https://job-boards.eu.greenhouse.io/stubhubinc/jobs/4920545101",
    )
    # ...and the same location is rejected on a domestic board too. Same answer either way.
    assert not location_ok(
        "Los Angeles, California, United States", "People Operations Partner",
        "https://boards.greenhouse.io/x/jobs/1",
    )


@pytest.mark.parametrize("title", [
    "Talent Community - GGRC-NB",
    "Sales Development Representative - Future Opportunity",
    "Join Our Talent Network - Customer Success",
])
def test_pipeline_postings_dropped(title):
    assert classify(title, None) is None


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


def test_san_jose_costa_rica_is_not_the_bay_area():
    """Surfaced while yield-testing new Lever boards: two Latin America BDR roles in
    San Jose, Costa Rica passed the gate on a bare "san jose". Fixed via NON_US rather
    than by qualifying the city, so postings that just say "San Jose" still count."""
    assert not location_ok(
        "San Jose, Costa Rica", "Business Development Representative - Latin America",
        "https://jobs.lever.co/bluelightconsulting/1",
    )
    assert location_ok("San Jose", "Sales Development Representative",
                       "https://jobs.lever.co/x/1")
    assert location_ok("San Jose, CA", "Sales Development Representative",
                       "https://jobs.lever.co/x/1")


@pytest.mark.parametrize("title", [
    "Administrative Support Specialist (Secret Cleared)- Remote",
    "Security Analyst - TS/SCI with Polygraph",
    "Help Desk Technician (Active Clearance Required)",
])
def test_clearance_gated_roles_dropped(title):
    """profile.yaml records no clearance; one takes months and a sponsoring employer."""
    assert classify(title, None) is None


def test_support_tiers():
    assert classify("Tier 3 Technical Support Agent", None) is None
    assert classify("Tier 1 Technical Support Specialist", None) == "it"


@pytest.mark.parametrize("location,expected", [
    # Country and nothing else -> distributed role, no office to commute to.
    ("United States", True), ("USA", True), ("U.S.", True), ("Nationwide", True),
    # A city is attached -> must satisfy the ordinary Bay-Area/remote-US rule.
    ("Los Angeles, California, United States", False),
    ("New York, NY, United States", False),
    ("United States, San Mateo, CA", True),
])
def test_bare_country_location(location, expected):
    assert location_ok(location, "Sales Development Representative",
                       "https://boards.greenhouse.io/x/jobs/1") is expected


@pytest.mark.parametrize("title,expected", [
    # A city+state in the TITLE pins the role even when the location is country-wide.
    ("Outside Sales Representative - Portland, OR", False),
    ("Outside Sales Representative - Pittsburgh, PA", False),
    ("Enterprise Sales Development Representative - US-Based", True),
    ("Sales Development Representative | Enterprise (North America)", True),
    ("Sales Development Representative - San Jose, CA", True),
])
def test_state_named_in_title(title, expected):
    assert location_ok("United States", title,
                       "https://boards.greenhouse.io/x/jobs/1") is expected


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
