"""Filter regressions for scripts/source.py.

Every case here is a real posting from the 2026-07-19 wave — the first wave the
rotating sourcer ran. 18 applications went out and 54 sourced jobs were rejected by
the brain after it had already opened them, which is wasted wave time. The rejects
below are the ones that were rejectable from the title alone, plus two location bugs
that let genuinely unreachable jobs through.
"""
from __future__ import annotations

import random
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
    ("IT Support Specialist II", "it"),
    ("Technical Support Engineer - University Graduate 2026", "it"),
    ("IT Help Desk Engineer", "it"),
    ("Security Operations Engineer", "security"),
]

# Titles that DID convert to an application on 2026-07-19. Must keep passing.
APPLIED = [
    ("Compliance Analyst", "security"),
    ("Security Analyst (Security Operations)", "security"),
    ("Technical Support Specialist", "it"),
]

# Lanes deleted 2026-09-18 (cyber + IT support only). These titles converted to real
# applications under the old nine-lane set — 370 sdr and 225 ops of 707 tailored resumes,
# against 112 security and zero cyber interviews. They must now classify to None: the cut
# is the whole point of the pivot, so a regression here is a regression to a BDR job hunt.
RETIRED_LANES = [
    "Enterprise SDR - NorCal",
    "Sales Development Representative - Bay Area",
    "Business Development Representative (BDR)",
    "Junior Sales Representative (SaaS)",
    "Inside Sales Representative",
    "Channel BDR",
    "PM Administrative Assistant",
    "Finance Operations Coordinator",
    "Talent and Operations Coordinator",
    "Recruiting Coordinator (Contract)",
    "Internal Communications Specialist",
    "Customer Success Associate",
    "Data Analyst",
]

# The cyber vocabulary the kill-list used to eat (2026-09-18). "clearance"/"cleared" and
# bare "audit" were blanket EXCLUDE entries, and the lane had no RMF/SOX/ISSO words at all,
# so the exact titles he wants were dropped before they were ever scored.
CYBER_TARGETS = [
    "IT Audit Analyst (SOX)",
    "GRC Analyst I",
    "Information Systems Security Officer",
    "Security Controls Assessor",
    "Cleared SOC Analyst (Remote)",
    "Cloud Security Analyst",
    "Third Party Risk Analyst",
    "IT Compliance Analyst - ITGC",
    "Cybersecurity Analyst (RMF / eMASS)",
    "Insider Threat Analyst",
    "Privacy Analyst",
]

# Security-titled roles that still hinge on a coding interview, plus the clearances a
# Secret in adjudication does not cover. source.CODING_HEAVY / EXCLUDE must kill these
# even though SUPPORT_SAFE now carves out the engineer family for security titles.
CYBER_NON_TARGETS = [
    "Application Security Engineer",
    "AppSec Engineer",
    "Product Security Engineer",
    "Detection Engineer",
    "Malware Reverse Engineer",
    "Offensive Security Engineer",
    "Penetration Tester",
    "Red Team Operator",
    "Forward Deployed Engineer",
    "Solutions Engineer",
    "Sales Engineer",
    "TS/SCI Poly Cyber Analyst",
    "Senior GRC Manager",
    "Tax Accountant",
    "Financial Statement Audit Associate",
]


@pytest.mark.parametrize("title", REJECTED)
def test_rejected_titles_are_filtered_at_source(title):
    assert classify(title, None) is None


@pytest.mark.parametrize("title,lane", APPLIED)
def test_applied_titles_still_pass(title, lane):
    assert classify(title, None) == lane


@pytest.mark.parametrize("title", RETIRED_LANES)
def test_retired_lanes_are_dropped(title):
    assert classify(title, None) is None


@pytest.mark.parametrize("title", CYBER_TARGETS)
def test_cyber_vocabulary_passes(title):
    assert classify(title, None) == "security"


@pytest.mark.parametrize("title", CYBER_NON_TARGETS)
def test_coding_heavy_and_high_clearance_dropped(title):
    assert classify(title, None) is None


def test_tier_tolerates_a_retired_lane():
    """seen.csv/retry.csv hold 1,227 rows of sdr/ops lanes; LANES[lane] would KeyError."""
    assert source._tier("sdr") == 99
    assert source._tier("security") == 1


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
    2026-07-19; the candidate has US work authorization only, so they were unwinnable."""
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
    assert location_ok(location, "Security Analyst", "u") is expected


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
    for attr in ("APPLIED_PATH", "SEEN_PATH", "RETRY_PATH", "BLOCKLIST_PATH", "EVENTS_PATH"):
        monkeypatch.setattr(source, attr, tmp_path / f"{attr.lower()}.missing")
    monkeypatch.setattr(source, "VERIFY_CACHE_PATH", tmp_path / "verify_cache.json")
    monkeypatch.setattr(source.time, "sleep", lambda *_: None)
    # Every row served now goes through verify_rows(), which asks the employer's ATS
    # whether the posting is still open. These tests invent their postings, so the real
    # check answers 404 for all of them and every assertion below sees an empty wave.
    # Stub the single-posting lookup, not verify_rows itself: the per-company cap, the
    # dedup and the hit-crediting under test all run around it, and a test that skipped
    # verification wholesale would stop covering them. Tests that care about the
    # verification rules patch this again themselves.
    monkeypatch.setattr(source, "_verify_one",
                        lambda url: {"alive": True, "location": None, "years": None})
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


@pytest.mark.parametrize("location,expected", [
    # Outer ring of the nine-county Bay Area — included on Jack's call, 2026-07-20.
    ("Santa Rosa, CA", True), ("Napa, CA", True), ("Vallejo, California", True),
    ("Fairfield, CA", True), ("Antioch, CA", True), ("Brentwood, CA", True),
    ("Pittsburg, CA", True), ("Benicia, CA", True), ("Pacifica, CA", True),
    ("El Cerrito, CA", True), ("Atherton, CA", True),
    # ...and their better-known namesakes must still be rejected. "pittsburg" is a
    # prefix of "Pittsburgh"; Fairfield/Antioch/Brentwood/Santa Rosa all exist elsewhere.
    ("Pittsburgh, PA", False), ("Fairfield, CT", False), ("Antioch, TN", False),
    ("Brentwood, TN", False), ("Santa Rosa, NM", False),
])
def test_outer_ring_bay_area(location, expected):
    assert location_ok(location, "Sales Development Representative", "u") is expected


@pytest.mark.parametrize("title", [
    "Fraud Investigator", "Investigations Analyst", "Loss Prevention Specialist",
    "Background Screening Specialist", "Financial Crime Analyst", "Sanctions Analyst",
    "AML Investigator", "Physical Security Specialist",
])
def test_investigations_lane(title):
    """His degree is criminology; these titles previously matched no lane at all."""
    assert classify(title, None) == "security"


# --- serving-path filters added 2026-08-09 ------------------------------------
# 887 attempted applications produced 162 submissions. 74% of everything a wave opened
# in a browser was thrown away, and 89% of those rejects — wrong location, closed,
# too senior — were knowable from the ATS API before the page was ever loaded.

@pytest.mark.parametrize("url,platform", [
    ("https://job-boards.greenhouse.io/grin/jobs/4555260005", "greenhouse"),
    ("https://grin.co/careers/jobs/?gh_jid=4555260005", "greenhouse"),
    ("https://jobs.lever.co/shieldai/d3f3a93f", "lever"),
    ("https://jobs.ashbyhq.com/traba/17f1440c", "ashby"),
    ("https://revgen.breezy.hr/p/6e425918c9ba-sdr", "breezy"),
    ("https://apply.workable.com/foo/j/ABC/", "workable"),
    ("https://jobs.smartrecruiters.com/VulcanCyberInc/743999694586151", "smartrecruiters"),
    ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x", "workday"),
    ("https://careers-acme.icims.com/jobs/1234/x/job", "icims"),
    ("https://www.linkedin.com/jobs/view/123", "linkedin"),
    ("https://careers.salesforce.com/en/jobs/jr325892/sdr/", "other"),
])
def test_platform_of(url, platform):
    assert source.platform_of(url) == platform


@pytest.mark.parametrize("platform", ["workable", "smartrecruiters", "workday",
                                      "icims", "linkedin", "ashby", "lever"])
def test_submit_blocked_platforms(platform):
    """0 of 147 Workable jobs and 0 of 7 SmartRecruiters ones ever became applications."""
    assert platform in source.SUBMIT_BLOCKED


@pytest.mark.parametrize("platform", ["greenhouse", "breezy"])
def test_platforms_that_convert_are_still_served(platform):
    assert platform not in source.SUBMIT_BLOCKED


@pytest.mark.parametrize("title", [
    "SDR - Atlanta GA not Bay Area",
    "Data Analyst - requires 2+ years experience, SQL proficiency",
    "People Operations - too senior (5+ yrs)",
    "Entry Level SDR - NC residency required",
    "Cybersecurity Analyst - 3-5 yrs + clearance required",
    "Business Operations Associate - LinkedIn required",
    "SDR SmartRecruiters HARD-AVOID",
])
def test_reject_note_is_a_verdict_not_a_title(title):
    """A wave already opened these and wrote down why they were a no."""
    assert source.REJECT_NOTE_RE.search(title)


@pytest.mark.parametrize("title", [
    "Sales Development Representative", "Security Analyst",
    "Associate GRC Cybersecurity", "Trust & Safety Agent 2026 New Grad",
    "Inside Sales Representative", "SOC Analyst Tier 1",
])
def test_reject_note_leaves_real_titles_alone(title):
    """Zero of the 896 rows in applied.csv match this pattern."""
    assert not source.REJECT_NOTE_RE.search(title)


@pytest.mark.parametrize("title,pinned", [
    ("BDR Texas Colorado", True),          # no comma, so the ", XX" rule never saw it
    ("SDR - Austin, TX", True),
    ("BDR - Pennsylvania only", True),
    ("Outside Sales Representative - Central Valley, CA", True),
    ("Sales Development Representative", False),
    ("Security Analyst - San Francisco, CA", False),
    ("Compliance Analyst, California", False),
    ("Inside Sales Representative, MM (PAM)", False),   # MM is not a state
    ("Analyst, AI", False),
])
def test_names_other_state_reads_spelled_out_states(title, pinned):
    assert source._names_other_state(title) is pinned


def _row(url="https://job-boards.greenhouse.io/x/jobs/1", title="SDR"):
    return {"url": url, "title": title, "company": "X", "location": "?"}


def test_verify_drops_a_closed_posting(monkeypatch, tmp_path):
    monkeypatch.setattr(source, "VERIFY_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(source.time, "sleep", lambda *_: None)
    monkeypatch.setattr(source, "_verify_one",
                        lambda url: {"alive": False, "reason": "posting 404"})
    assert source.verify_rows([_row()], 1, 4) == []


def test_verify_drops_an_out_of_band_posting(monkeypatch, tmp_path):
    monkeypatch.setattr(source, "VERIFY_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(source.time, "sleep", lambda *_: None)
    monkeypatch.setattr(source, "_verify_one",
                        lambda url: {"alive": True, "location": "Remote, US", "years": 5})
    assert source.verify_rows([_row()], 1, 4) == []
    # ...and the same posting survives once the ceiling is lifted.
    assert len(source.verify_rows([_row()], 1, 0)) == 1


def test_verify_fails_open_when_the_ats_is_unreachable(monkeypatch, tmp_path):
    """A timeout must never cost a live job. Only an explicit 404/410 is fatal."""
    monkeypatch.setattr(source, "VERIFY_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(source.time, "sleep", lambda *_: None)
    monkeypatch.setattr(source, "_verify_one",
                        lambda url: {"alive": True, "location": None, "years": None})
    kept = source.verify_rows([_row()], 1, 4)
    assert len(kept) == 1 and kept[0]["location"] == "?"


def test_verify_stops_as_soon_as_it_has_enough(monkeypatch, tmp_path):
    """A healthy queue costs only as many ATS requests as it actually serves."""
    monkeypatch.setattr(source, "VERIFY_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(source.time, "sleep", lambda *_: None)
    seen_urls = []

    def spy(url):
        seen_urls.append(url)
        return {"alive": True, "location": "Remote, US", "years": None}

    monkeypatch.setattr(source, "_verify_one", spy)
    rows = [_row(f"https://job-boards.greenhouse.io/x/jobs/{i}") for i in range(50)]
    assert len(source.verify_rows(rows, 3, 4)) == 3
    assert len(seen_urls) < 20, "checked far more postings than it served"


def test_verify_disabled_passes_rows_through(monkeypatch):
    """--no-verify, for when the ATS is down and raw rows beat no rows."""
    def explode(url):
        raise AssertionError("--no-verify must not touch the network")

    monkeypatch.setattr(source, "_verify_one", explode)
    rows = [_row(f"https://job-boards.greenhouse.io/x/jobs/{i}") for i in range(5)]
    assert source.verify_rows(rows, 2, 4, enabled=False) == rows[:2]


@pytest.mark.parametrize("text,years", [
    ("We need 5+ years of experience in security", 5),
    ("2+ years required, 5+ preferred", 2),          # the MINIMUM, never the maximum
    ("3-5 years experience with SQL", 3),
    ("No experience necessary", None),
    ("Founded in 2019, 100 years of combined leadership", None),
])
def test_min_years_required(text, years):
    assert source._min_years_required(text) == years


# --- barren boards + rotation seeding ----------------------------------------
def test_barren_board_is_dropped_from_the_pool():
    """Past BARREN_POSTS with no lifetime hit is an employer in another market."""
    assert source._is_live({"posts": source.BARREN_POSTS, "hits": 0}) is False


def test_a_barren_board_that_ever_produced_is_kept_forever():
    """hits never decays, so one matching posting is a permanent exemption. Without
    this a big employer whose Bay roles are all already in seen.csv gets deleted."""
    assert source._is_live({"posts": 99999, "hits": 1}) is True


def test_a_small_board_with_nothing_is_not_barren():
    """The median board returns 8-10 postings. Empty weeks are normal; staleness
    floats them back up. Only p99 volume with zero lifetime hits is disqualifying."""
    assert source._is_live({"posts": source.BARREN_POSTS - 1, "hits": 0}) is True


def test_rotation_round_trips_posts(sandbox):
    source.save_rotation({("greenhouse", "alpha"):
                          {"last_mined": "", "mines": 2, "hits": 1, "fails": 0, "posts": 37}})
    assert source.load_rotation()[("greenhouse", "alpha")]["posts"] == 37


def test_rotation_without_a_posts_column_still_loads(sandbox):
    """The live file predates the column; a missing value must read as 0, not crash."""
    source.ROTATION_PATH.write_text(
        "platform,token,last_mined,mines,hits,fails\ngreenhouse,alpha,,3,1,0\n")
    assert source.load_rotation()[("greenhouse", "alpha")]["posts"] == 0


def test_mining_credits_posts(sandbox, monkeypatch):
    monkeypatch.setattr(source, "_fetch_once", lambda p, t: [
        {"url": f"https://x/{t}/{i}", "title": "Groomer", "location": "Ohio"}
        for i in range(5)])
    source.main(["source.py", "--n", "5", "--boards", "3", "--no-verify"])
    rows = _rotation_rows(sandbox)
    assert all(int(r["posts"]) == 5 for r in rows.values()), rows


def test_board_of_reads_every_ats_url_shape():
    from seed_rotation import board_of
    cases = {
        "https://boards.greenhouse.io/verkada/jobs/1234": ("greenhouse", "verkada"),
        "https://job-boards.greenhouse.io/figma/jobs/99": ("greenhouse", "figma"),
        "https://boards.greenhouse.io/embed/job_app?for=brex&token=7": ("greenhouse", "brex"),
        "https://jobs.lever.co/hive/abc-def": ("lever", "hive"),
        "https://jobs.ashbyhq.com/ramp/xyz": ("ashby", "ramp"),
        "https://acme.breezy.hr/p/12345": ("breezy", "acme"),
        "https://www.brex.com/careers/role": None,
    }
    for url, want in cases.items():
        assert board_of(url) == want, url


def test_seeding_never_overwrites_real_history(sandbox):
    import seed_rotation
    source.save_rotation({("greenhouse", "alpha"):
                          {"last_mined": "2026-08-01", "mines": 9, "hits": 0,
                           "fails": 0, "posts": 12}})
    source.APPLIED_PATH.write_text(
        "url\nhttps://boards.greenhouse.io/alpha/jobs/1\n"
        "https://boards.greenhouse.io/beta/jobs/2\n")
    seed_rotation.main(["seed_rotation.py", "--write"])
    rows = _rotation_rows(sandbox)
    assert int(rows[("greenhouse", "alpha")]["mines"]) == 9   # untouched
    assert int(rows[("greenhouse", "alpha")]["hits"]) == 0
    assert int(rows[("greenhouse", "beta")]["hits"]) == 1     # seeded
    assert int(rows[("greenhouse", "beta")]["mines"]) == seed_rotation.SEED_MINES


def test_seeding_is_idempotent(sandbox):
    import seed_rotation
    source.APPLIED_PATH.write_text(
        "url\nhttps://jobs.lever.co/gamma/1\nhttps://jobs.lever.co/gamma/2\n")
    seed_rotation.main(["seed_rotation.py", "--write"])
    first = _rotation_rows(sandbox)[("lever", "gamma")]
    seed_rotation.main(["seed_rotation.py", "--write"])
    assert _rotation_rows(sandbox)[("lever", "gamma")] == first


def test_seeding_ignores_boards_that_are_not_in_the_pool(sandbox):
    import seed_rotation
    source.APPLIED_PATH.write_text("url\nhttps://boards.greenhouse.io/notinpool/jobs/1\n")
    seed_rotation.main(["seed_rotation.py", "--write"])
    assert ("greenhouse", "notinpool") not in _rotation_rows(sandbox)


def test_seeded_boards_outrank_never_mined_ones(sandbox):
    """The whole point: a board that has produced beats a coin flip from the pool."""
    seeded = source.pick_boards.__globals__  # noqa: F841 - readability only
    state = {("greenhouse", "alpha"): {"last_mined": "", "mines": 4, "hits": 4,
                                       "fails": 0, "posts": 0}}
    pool = [("greenhouse", "alpha"), ("greenhouse", "beta")]
    random.seed(0)
    wins = sum(source.pick_boards(pool, state, 1)[0] == ("greenhouse", "alpha")
               for _ in range(400))
    assert wins > 280, wins   # x2.25 productivity over an unmined board


def test_attempted_posting_is_never_served_again(sandbox, monkeypatch):
    """A skip is marked seen, and the backlog IS seen.csv, so before attempted_urls() a
    skipped posting came straight back: 148 of 209 attempts from 09-26 to 10-04 were
    repeats. The careers-page twin of the same Greenhouse id must match too."""
    events = sandbox / "apply_events.jsonl"
    events.write_text(
        '{"url": "https://job-boards.greenhouse.io/roblox/jobs/8211694", "outcome": "skipped"}\n'
        'not json\n'
        '{"url": "https://jobs.lever.co/acme/abc-123", "outcome": "blocked"}\n'
        '{"url": "https://job-boards.greenhouse.io/coinbase/jobs/42", "outcome": "error"}\n')
    monkeypatch.setattr(source, "EVENTS_PATH", events)
    twin = source._norm("https://careers.roblox.com/jobs/8211694?gh_jid=8211694")
    assert twin in source.attempted_urls()
    assert twin in source.load_known_urls()
    assert twin in source._dead_urls()
    assert source._norm("https://jobs.lever.co/acme/abc-123/apply") in source._dead_urls()
    assert source._norm("https://job-boards.greenhouse.io/roblox/jobs/1") not in source._dead_urls()
    assert source._norm("https://job-boards.greenhouse.io/coinbase/jobs/42") not in source._dead_urls()


@pytest.mark.parametrize("title", [
    "Business Development Representative SaaS Cybersecurity",
    "Junior AE Cybersecurity",
    "SDR - Cyber Security",
])
def test_sales_titles_with_a_security_word_dropped(title):
    """The sdr lane is deleted; a security keyword in a sales title must not revive it."""
    assert classify(title, None) is None
