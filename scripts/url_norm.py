"""Shared URL normalizer for applied/seen dedup.

Handles real-world drift across job-board hosts so the same posting hashes
to one key:

- LinkedIn: /jobs/collections/...?currentJobId=N → /jobs/view/N
- LinkedIn: drops trackingId / refId / currentJobId / origin / etc
- Greenhouse: boards.greenhouse.io ↔ job-boards.greenhouse.io (the migration
  Greenhouse rolled out in 2024); also case-folds the company slug
- Lever: strips trailing /apply on jobs.lever.co URLs
- Trailing slash, scheme, and host case folded everywhere
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse


def _greenhouse_id(p) -> str | None:
    q = parse_qs(p.query)
    jid = (q.get("gh_jid") or [""])[0]
    if jid.isdigit():
        return jid
    if "greenhouse.io" not in p.netloc:
        return None
    parts = p.path.strip("/").split("/")
    if parts[-2:-1] == ["jobs"] and parts[-1].isdigit():
        return parts[-1]
    tok = (q.get("token") or [""])[0]
    if "job_app" in parts and tok.isdigit():
        return tok
    return None


def normalize(url: str) -> str:
    if not url or not isinstance(url, str):
        return ""
    raw = url.strip()
    try:
        p = urlparse(raw.lower())
    except Exception:
        return raw.lower()
    if not p.netloc:
        return raw.lower()

    netloc = p.netloc
    path = p.path.rstrip("/")
    query = ""

    # --- Any Greenhouse posting, keyed by its job id ------------------------
    # Greenhouse job ids are global, so the id alone names the posting whatever page
    # it was reached through: the hosted page (.../<slug>/jobs/<id>), the embed form
    # (.../embed/job_app?for=<slug>&token=<id>) or a company careers page (?gh_jid=<id>).
    # Before this, every embed URL dropped its query and collapsed to ONE key, so
    # `applied.py check` said MATCH for any embed job at all, and the same posting under
    # two URL shapes never matched — Abnormal's FedRAMP role went out 7 times and
    # IXL's help desk role 6 times between 2026-09-02 and 09-23.
    gh_id = _greenhouse_id(p)
    if gh_id:
        return f"https://boards.greenhouse.io/jobs/{gh_id}"

    # --- LinkedIn ----------------------------------------------------------
    if netloc.endswith("linkedin.com"):
        netloc = "www.linkedin.com"
        # collection page → canonical /jobs/view/<id>
        q = parse_qs(p.query)
        cur = q.get("currentjobid") or q.get("currentJobId".lower())
        if cur and cur[0].isdigit():
            path = f"/jobs/view/{cur[0]}"
        # /jobs/view/<id>/ keeps id only; trailing already stripped
        # drop tracking suffixes — query intentionally cleared

    # --- Greenhouse --------------------------------------------------------
    elif "greenhouse.io" in netloc:
        # Migration: boards.greenhouse.io ↔ job-boards.greenhouse.io
        netloc = "boards.greenhouse.io"
        # case-fold the slug (path is already lowercased above)

    # --- Lever -------------------------------------------------------------
    elif netloc == "jobs.lever.co":
        # strip /apply suffix — same posting
        if path.endswith("/apply"):
            path = path[: -len("/apply")]

    # --- Greenhouse-embedded company career pages --------------------------
    # 203 boards in the pool serve postings from the COMPANY's own domain, with the
    # job id only in the query: stripe.com/jobs/listing?gh_jid=123, coreweave.com/
    # careers?gh_jid=456. Dropping the query collapsed every posting on such a board
    # to a single key — so after one match the rest were discarded within a run, and
    # once a job was applied to, every future posting from that company was invisible
    # forever (databricks 787 postings -> 1 key, stripe 525 -> 1, waymo 402 -> 1).
    #
    # Only for non-canonical hosts: on boards.greenhouse.io the id is already in the
    # path, so keeping gh_jid there would break the boards/job-boards host folding.
    else:
        jid = parse_qs(p.query).get("gh_jid")
        if jid and jid[0]:
            query = f"gh_jid={jid[0]}"

    # --- Default: drop query + fragment, keep scheme/path -----------------
    return urlunparse((p.scheme or "https", netloc, path, "", query, ""))
