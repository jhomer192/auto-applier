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

    # --- Default: drop query + fragment, keep scheme/path -----------------
    return urlunparse((p.scheme or "https", netloc, path, "", query, ""))
