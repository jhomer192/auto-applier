"""url_norm: one key per posting, whatever URL shape it was reached through."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pytest  # noqa: E402

from url_norm import normalize  # noqa: E402

GH = "https://boards.greenhouse.io/jobs/7979987003"


@pytest.mark.parametrize("url", [
    "https://job-boards.greenhouse.io/abnormalsecurity/jobs/7979987003",
    "https://boards.greenhouse.io/AbnormalSecurity/jobs/7979987003/",
    "https://job-boards.greenhouse.io/embed/job_app?for=abnormalsecurity&token=7979987003",
    "https://abnormal.ai/careers/jobs/7979987003?gh_jid=7979987003",
    "https://job-boards.greenhouse.io/embed/job_app?token=7979987003&for=abnormalsecurity&gh_jid=7979987003",
])
def test_greenhouse_shapes_share_one_key(url):
    assert normalize(url) == GH


def test_embed_urls_do_not_collapse_together():
    # The pre-2026-09-26 bug: every embed URL normalized to .../embed/job_app.
    a = normalize("https://job-boards.greenhouse.io/embed/job_app?for=doma&token=8219586")
    b = normalize("https://job-boards.greenhouse.io/embed/job_app?for=doma&token=8219588")
    assert a != b


def test_vanity_pages_without_id_keep_their_path():
    assert normalize("https://www.ixl.com/company/careers?gh_jid=8773437002") == \
        normalize("https://www.ixl.com/company/careers/apply?gh_jid=8773437002")
    assert normalize("https://example.com/careers/role-a") != normalize("https://example.com/careers/role-b")


def test_other_hosts_unchanged():
    assert normalize("https://job-boards.greenhouse.io/stripe") == "https://boards.greenhouse.io/stripe"
    assert normalize("https://jobs.lever.co/x/abc/apply") == "https://jobs.lever.co/x/abc"
    assert normalize("https://www.linkedin.com/jobs/collections/?currentJobId=5") == \
        "https://www.linkedin.com/jobs/view/5"
