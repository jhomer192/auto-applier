"""bench.py scorecard on a synthetic log."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import bench  # noqa: E402


def _ev(ts, outcome, url, company="Acme", title="SOC Analyst", reason=None, lane="security", ats="greenhouse"):
    return {"ts": ts, "company": company, "title": title, "ats": ats, "url": url, "outcome": outcome,
            "block_reason": reason, "lane": lane, "resume_status": "tailored"}


def _write(tmp_path, events, responses=()):
    (tmp_path / "apply_events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (tmp_path / "responses.jsonl").write_text("".join(json.dumps(r) + "\n" for r in responses))
    (tmp_path / "tailor_log.csv").write_text(
        "ts,lane,status,reason,md5,archive,jd_chars,keywords,summary\n"
        "2026-09-02T00:00:00+00:00,security,tailored,,x,y,1,k,s\n"
        "2026-09-02T01:00:00+00:00,security,fallback,QC rejected twice,x,y,1,k,s\n")


def _window(tmp_path):
    return bench.window(datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 3, tzinfo=timezone.utc),
                        data=tmp_path, transcripts=tmp_path / "none")


def test_funnel_and_categories(tmp_path):
    _write(tmp_path, [
        _ev("2026-09-01T01:00:00+00:00", "applied", "https://job-boards.greenhouse.io/acme/jobs/1"),
        _ev("2026-09-01T02:00:00+00:00", "skipped", "https://job-boards.greenhouse.io/acme/jobs/2", reason="job closed 404"),
        _ev("2026-09-01T03:00:00+00:00", "skipped", "u3", reason="5+ years required"),
        _ev("2026-09-01T04:00:00+00:00", "blocked", "https://job-boards.greenhouse.io/acme/jobs/4", reason="428 reCAPTCHA"),
        _ev("2026-08-01T00:00:00+00:00", "applied", "old"),  # outside the window
    ])
    m = _window(tmp_path)
    assert (m["served"], m["applied"]) == (4, 1)
    assert m["skip_rate"] == 0.5 and m["block_rate"] == 0.5
    assert m["skip_reasons"] == {"closed": 1, "seniority": 1}
    assert m["block_reasons"] == {"captcha": 1}
    assert m["ats"]["greenhouse/hosted"]["applied"] == 1
    assert m["tailor_ok_rate"] == 0.5
    assert m["in_lane_share"] == 1.0
    assert m["response_rate"] is None  # no responses logged: say so, don't report 0%


def test_duplicate_across_url_shapes(tmp_path):
    _write(tmp_path, [
        _ev("2026-08-20T00:00:00+00:00", "applied", "https://acme.com/careers?gh_jid=77"),
        _ev("2026-09-01T00:00:00+00:00", "applied", "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=77"),
        _ev("2026-09-01T01:00:00+00:00", "applied", "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=78",
            title="IT Support"),
    ])
    assert _window(tmp_path)["dup_applies"] == 1


def test_categorize_order():
    assert bench.categorize("Washington DC not bay area; 3+ years", bench.SKIP_CATEGORIES) == "location"
    assert bench.categorize("Ashby HARD-AVOID", bench.SKIP_CATEGORIES) == "ats_avoid"
    assert bench.categorize("position closed", bench.SKIP_CATEGORIES) == "closed"
    assert bench.categorize("", bench.SKIP_CATEGORIES) == "other"
