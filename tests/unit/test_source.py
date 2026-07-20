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

import source  # noqa: E402
from source import classify, is_bay_area, location_ok  # noqa: E402

# Titles the brain opened and rejected on 2026-07-19. Should never reach it again.
REJECTED = [
    "Technical Sourcer (Contract)",
    "Talent Sourcer - Temporary",
    "Executive Sourcer",
    "Technical Talent Sourcer (6-Month Contract)",
    "Customer Success Account Executive",
    "Account Executive PR Communications",
    "Operations Associate Part Time",
    "Expression of Interest: School Sales Representative (Remote, US)",
]

# Deliberately un-filtered on review (2026-07-19). Bare "executive" was killing
# "Executive IT Support Specialist" (SpaceX, Palo Alto) and "Account Executive - New
# Grad"; "Customer Success Executive" survives via the narrower "success executive".
# The sourcer is a first pass on TITLES — the brain still does the fit read, and
# missing a good role costs an opportunity while surfacing a marginal one costs one
# page load.
UNFILTERED_ON_PURPOSE = [
    ("Executive IT Support Specialist", "it"),
    ("Recruiting Coordinator (Contract)", "people"),
    ("IT Support Specialist II", "it"),
    ("Technical Support Engineer - University Graduate 2026", "it"),
    ("IT Help Desk Engineer", "it"),
    ("Security Operations Engineer", "security"),
    ("Internal Communications Specialist", "content"),
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


# --- fetch status classification ---------------------------------------------
# Greenhouse returns 406 when throttling. On 2026-07-19 a burst of requests made 63
# healthy boards 406 at once; every one answered 200 minutes later. If those count as
# failures, four such episodes silently prune the board from the pool forever — the
# opposite of the "widen the pool" work these boards exist for.

def _http_error(code):
    import urllib.error
    def raiser(platform, token):
        raise urllib.error.HTTPError("http://x", code, "boom", {}, None)
    return raiser


def test_404_is_missing_and_counts_toward_pruning(monkeypatch):
    monkeypatch.setattr(source, "_fetch_once", _http_error(404))
    _board, postings, status = source.fetch(("greenhouse", "gone"))
    assert (postings, status) == (None, "missing")


@pytest.mark.parametrize("code", [406, 429, 500, 503])
def test_throttling_is_not_a_board_failure(monkeypatch, code):
    monkeypatch.setattr(source, "_fetch_once", _http_error(code))
    monkeypatch.setattr(source.time, "sleep", lambda *_: None)  # don't back off in tests
    _board, postings, status = source.fetch(("greenhouse", "healthy"))
    assert (postings, status) == (None, "throttled")


def test_transient_error_retries_once_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(platform, token):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("first attempt times out")
        return [{"title": "SDR", "location": "San Francisco, CA", "url": "u"}]

    monkeypatch.setattr(source, "_fetch_once", flaky)
    monkeypatch.setattr(source.time, "sleep", lambda *_: None)
    _board, postings, status = source.fetch(("lever", "flaky"))
    assert status == "ok" and len(postings) == 1 and calls["n"] == 2


@pytest.mark.parametrize("title", [
    "Medical Capital Equipment Sales Representative (Central Midwest Candidates Only)",
    "Business Development Representative (Texas Residents Only)",
    "Sales Development Representative - Must Reside In Colorado",
])
def test_geographic_restriction_in_title(title):
    """Restriction stated in the title, filed under a country-wide location, so neither
    the location string nor the state-code check catches it."""
    assert classify(title, None) is None


@pytest.mark.parametrize("title,lane", UNFILTERED_ON_PURPOSE)
def test_deliberately_unfiltered(title, lane):
    assert classify(title, None) == lane


@pytest.mark.parametrize("title", [
    "Customer Success Executive", "Software Engineer", "Senior Software Engineer",
    "Backend Engineer", "Staff Machine Learning Engineer",
    "Customer Service Delivery Driver - San Francisco",
])
def test_still_filtered(title):
    """The narrowing must not become a hole: quota roles and pure SWE stay dead."""
    assert classify(title, None) is None


def test_foreign_host_gate_is_load_bearing():
    """A discriminating input. The previous two EU tests passed with FOREIGN_HOSTS
    deleted entirely — their inputs were already rejected by the ordinary rule, so
    they asserted nothing. Here the US marker is in the TITLE and not the location,
    so the result differs with and without the gate."""
    args = ("Remote", "Sales Development Representative, United States")
    assert not location_ok(*args, "https://job-boards.eu.greenhouse.io/x/jobs/1")
    assert location_ok(*args, "https://boards.greenhouse.io/x/jobs/1")


def test_dublin_ca_is_not_dublin_ireland():
    """NON_US ran before the Bay check, so bare "dublin" deleted Dublin, CA — including
    multi-site postings that also named San Francisco and Oakland."""
    assert location_ok("Dublin, CA", "Compliance Analyst", "u")
    assert location_ok("San Francisco, CA; Dublin, CA; Oakland, CA", "Compliance Analyst", "u")
    assert not location_ok("Dublin, Ireland", "Compliance Analyst", "u")
    assert not location_ok("Dublin, OH", "Compliance Analyst", "u")


@pytest.mark.parametrize("location,expected", [
    ("Sunnyvale, TX, United States", False),   # Sunnyvale TX is a Dallas suburb
    ("Sunnyvale, CA", True),
    ("Marina Del Rey, CA", False),             # matched on "marin"
    ("Marin County, CA", True),
    ("Haywards Heath, West Sussex", False),    # matched on "hayward"
    ("Hayward, CA", True),
    ("Berkeley Heights, NJ", False),
    ("Los Altos, CA", True),
    ("Colma, CA", True),
])
def test_substring_collisions(location, expected):
    assert location_ok(location, "Sales Development Representative", "u") is expected


@pytest.mark.parametrize("title", [
    "Inside Sales Representative, MM (PAM)",
    "Sales Development Representative, US (PerfectScale & SELECT)",
    "Sales Representative, SMB (Remote, US)",
])
def test_two_letter_tokens_are_not_states(title):
    """`,\\s*([A-Z]{2})` captured AI/US/MM/CX/RN and killed real remote-US roles —
    60% of that branch's rejections were wrong on live data."""
    assert location_ok("Remote, US", title, "https://boards.greenhouse.io/x/jobs/1")


# --- main() level -------------------------------------------------------------
# The consumer half of the throttle fix had no coverage: fetch()'s 406-vs-404
# classification was tested, but not main()'s duty to leave state untouched. A
# mutation putting `fails += 1` back on the throttled path survived the whole suite.

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point source.py's paths at a throwaway pool + rotation file."""
    pool = tmp_path / "companies.txt"
    pool.write_text("greenhouse:alpha\ngreenhouse:beta\nlever:gamma\n")
    monkeypatch.setattr(source, "POOL_PATH", pool)
    monkeypatch.setattr(source, "ROTATION_PATH", tmp_path / "board_rotation.csv")
    for attr in ("APPLIED_PATH", "SEEN_PATH", "RETRY_PATH", "BLOCKLIST_PATH"):
        monkeypatch.setattr(source, attr, tmp_path / f"{attr.lower()}.missing")
    monkeypatch.setattr(source.time, "sleep", lambda *_: None)
    return tmp_path


def _rotation_rows(tmp_path):
    import csv as _csv
    f = tmp_path / "board_rotation.csv"
    if not f.exists():
        return {}
    with f.open(newline="") as fh:
        return {(r["platform"], r["token"]): r for r in _csv.DictReader(fh)}


def test_throttled_board_never_accrues_fails(sandbox, monkeypatch, capsys):
    """The bug a420811 fixed: 4 throttled waves silently delete a healthy board."""
    import urllib.error

    def throttle(platform, token):
        raise urllib.error.HTTPError("http://x", 406, "throttled", {}, None)

    monkeypatch.setattr(source, "_fetch_once", throttle)
    rc = source.main(["source.py", "--boards", "3"])
    assert rc == 2, "a wave where nothing answered must fail loudly, not look dry"
    for row in _rotation_rows(sandbox).values():
        assert int(row["fails"]) == 0
        assert int(row["mines"]) == 0
        assert row["last_mined"] == ""
    assert "API problem" in capsys.readouterr().err


def test_missing_board_does_accrue_fails(sandbox, monkeypatch):
    import urllib.error
    calls = {"n": 0}

    def one_gone(platform, token):
        calls["n"] += 1
        if token == "alpha":
            raise urllib.error.HTTPError("http://x", 404, "gone", {}, None)
        return [{"title": "SDR", "location": "San Francisco, CA",
                 "url": f"https://boards.greenhouse.io/{token}/jobs/{calls['n']}"}]

    monkeypatch.setattr(source, "_fetch_once", one_gone)
    assert source.main(["source.py", "--boards", "3"]) == 0
    rows = _rotation_rows(sandbox)
    assert int(rows[("greenhouse", "alpha")]["fails"]) == 1
    assert int(rows[("greenhouse", "beta")]["fails"]) == 0


def test_per_company_cap_and_n_limit(sandbox, monkeypatch, capsys):
    def flood(platform, token):
        return [{"title": "Sales Development Representative",
                 "location": "San Francisco, CA",
                 "url": f"https://boards.greenhouse.io/{token}/jobs/{i}"}
                for i in range(50)]

    monkeypatch.setattr(source, "_fetch_once", flood)
    source.main(["source.py", "--boards", "3", "--per-company", "2", "--n", "4"])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 4, "the --n cap must hold"
    from collections import Counter
    assert max(Counter(l.split("\t")[1] for l in lines).values()) <= 2


def test_hits_credited_only_for_shown_jobs(sandbox, monkeypatch):
    """A board that floods 50 matches but is capped to 2 must not bank 50 hits."""
    def flood(platform, token):
        return [{"title": "Sales Development Representative",
                 "location": "San Francisco, CA",
                 "url": f"https://boards.greenhouse.io/{token}/jobs/{i}"}
                for i in range(50)]

    monkeypatch.setattr(source, "_fetch_once", flood)
    source.main(["source.py", "--boards", "3", "--per-company", "2", "--n", "4"])
    assert sum(int(r["hits"]) for r in _rotation_rows(sandbox).values()) == 4


def test_applied_urls_are_deduped(sandbox, monkeypatch, capsys):
    """Dedup against applied.csv is the script's entire reason to exist."""
    applied = sandbox / "applied.csv"
    applied.write_text("timestamp,url,company,title\n"
                       "t,https://boards.greenhouse.io/alpha/jobs/1,alpha,SDR\n")
    monkeypatch.setattr(source, "APPLIED_PATH", applied)

    def one(platform, token):
        return [{"title": "Sales Development Representative",
                 "location": "San Francisco, CA",
                 "url": f"https://boards.greenhouse.io/{token}/jobs/1"}]

    monkeypatch.setattr(source, "_fetch_once", one)
    source.main(["source.py", "--boards", "3"])
    out = capsys.readouterr().out
    assert "/alpha/jobs/1" not in out
    assert "/beta/jobs/1" in out


def test_ashby_is_off_by_default(sandbox, monkeypatch, capsys):
    (sandbox / "companies.txt").write_text("ashby:acme\ngreenhouse:alpha\n")

    seen = []

    def record(platform, token):
        seen.append(platform)
        return [{"title": "SDR", "location": "San Francisco, CA",
                 "url": f"https://x/{token}/1"}]

    monkeypatch.setattr(source, "_fetch_once", record)
    source.main(["source.py", "--boards", "5"])
    assert "ashby" not in seen


@pytest.mark.parametrize("title", [
    "(Work At Home) Data Entry - Remote - Administrative Assistant",
    "Work From Home Data Entry Clerk",
])
def test_work_from_home_data_entry_spam(title):
    """One fake posting cloned across dozens of small towns, and a personal-data
    harvesting funnel. Found while yield-testing Breezy HR, 2026-07-19."""
    assert classify(title, None) is None


@pytest.mark.parametrize("location,title,expected", [
    # "remote" in the TITLE plus any US city is not a remote-US role.
    ("Oshkosh, United States", "Administrative Assistant - Remote", False),
    ("Battle Mountain, United States", "Operations Coordinator (Remote)", False),
    # The location itself says remote, or there is no location to contradict the title.
    ("Remote - US", "Sales Development Representative", True),
    ("Remote, United States", "Sales Development Representative", True),
    ("United States", "Sales Development Representative", True),
    ("", "Business Development Representative - US Remote", True),
])
def test_remote_must_come_from_the_location(location, title, expected):
    assert location_ok(location, title, "https://boards.greenhouse.io/x/1") is expected


# --- pruning is not a one-way door -------------------------------------------
def test_prune_amnesty():
    """A pruned board is never fetched again, so `fails` can never reset — pruning was
    permanent. A four-wave outage (or a 406 throttling storm) would delete a healthy
    board from the pool forever. One retry a month makes it recoverable."""
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    stale = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
    assert source._is_live({"fails": 0})
    assert source._is_live({"fails": source.DEAD_AFTER - 1, "last_mined": recent})
    assert not source._is_live({"fails": source.DEAD_AFTER, "last_mined": recent})
    assert source._is_live({"fails": source.DEAD_AFTER, "last_mined": stale})


def test_blocked_companies_are_not_even_fetched(sandbox, monkeypatch):
    """A blocklisted board was still mined every rotation, burning a slot it can
    never repay."""
    blocklist = sandbox / "blocklist.txt"
    blocklist.write_text("boards.greenhouse.io\talpha\tcaptcha wall\n")
    monkeypatch.setattr(source, "BLOCKLIST_PATH", blocklist)
    fetched = []

    def record(platform, token):
        fetched.append(token)
        return [{"title": "Sales Development Representative",
                 "location": "San Francisco, CA",
                 "url": f"https://boards.greenhouse.io/{token}/jobs/1"}]

    monkeypatch.setattr(source, "_fetch_once", record)
    source.main(["source.py", "--boards", "5"])
    assert "alpha" not in fetched
    assert "beta" in fetched


@pytest.mark.parametrize("title,expected", [
    # California, but not the Bay Area — the ", CA" satisfied the other-state check.
    ("Outside Sales Representative - Central Valley, CA", False),
    ("Sales Representative - Los Angeles, CA", False),
    ("Account Development Rep - San Diego", False),
    ("BDR - Sacramento", False),
    # Bay Area named in the title still passes.
    ("Sales Development Representative - San Francisco, CA", True),
    ("Sales Development Representative", True),
])
def test_california_but_not_bay_area_in_title(title, expected):
    assert location_ok("United States", title,
                       "https://boards.greenhouse.io/x/1") is expected
