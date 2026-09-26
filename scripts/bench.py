#!/usr/bin/env python3
"""bench.py — performance benchmark for the applier, one window or before/after.

Every number comes from logs the pipeline already writes; nothing here asks the
brain to remember anything:

  data/apply_events.jsonl   one row per attempt (apply_log.py)
  data/tailor_log.csv       one row per resume tailoring (tailor_resume.py)
  data/responses.jsonl      employer replies (record_response.py, via the nightly sweep)
  data/action_items.csv     human follow-ups the sweep found in the inbox
  ~claude/.claude/projects/-opt-auto-applier/*.jsonl   session transcripts (tokens, time)

Usage:
  python3 scripts/bench.py                       # last 7d vs the 7d before it
  python3 scripts/bench.py --days 3              # last 3d vs the 3d before it
  python3 scripts/bench.py --split 2026-09-26    # everything since the change vs the same span before it
  python3 scripts/bench.py --since 2026-09-18 --until 2026-09-25   # one window, no comparison
  python3 scripts/bench.py --json                # machine-readable, for saving a baseline

"Served" = every logged attempt (applied + skipped + blocked + error): each one is a
job a wave spent browser time on. A skip is a job that should never have reached the
browser, so skip share is the sourcing filter's miss rate.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TRANSCRIPTS = Path("/home/claude/.claude/projects/-opt-auto-applier")

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from url_norm import normalize as _norm
except ImportError:  # keep the benchmark runnable on a bare checkout
    def _norm(u: str) -> str:
        return (u or "").strip().lower().rstrip("/")

TARGET_LANES = {"security", "it"}
IDLE_GAP = 600  # seconds; a longer silence between turns is idle time, not work
COMPANY_CAP = 4  # source.py --per-company default (lifetime)
MATURE_DAYS = 14  # an application younger than this has not had time to draw a reply

# First match wins, so order matters: a "closed" posting is closed whatever else it says.
SKIP_CATEGORIES = [
    ("closed", r"closed|404|expired|no longer|not found|redirect|no jobs|filled|not listed|not on (the )?board"),
    ("ats_avoid", r"ashby|workday|tier.?3|hard.?avoid|turnstile|zoho|icims|smartrecruiters|workable|taleo|"
                  r"non-stand|custom ats|not (greenhouse|direct)|wellfound|linkedin"),
    ("location", r"not bay|not us\b|not remote|residents|territory|india|onsite in|location|"
                 r"\b(tx|nc|ma|md|sc|va|dc|ny)\b"),
    ("seniority", r"senior|\d\s*\+?\s*(years|yrs|yoe)|\bvp\b|lead|director|manager|principal|staff|not entry"),
    ("off_lane", r"lane|not cyber|coding|engineer|journalism|unpaid|apprentice|intern|contract|prohibits ai"),
    ("dup", r"already|duplicate|dup\b"),
]
BLOCK_CATEGORIES = [
    ("captcha", r"428|recaptcha|hcaptcha|turnstile|captcha|cloudflare|bot"),
    ("verify_code", r"code|pin\b|verif|otp|email never"),
    ("proxy", r"proxy|tunnel|network|timeout|timed out"),
    ("form_widget", r"react|select|pac-container|places|upload|field|widget|dropdown|phone|iti"),
    ("login_wall", r"login|sign.?in|account|password"),
]

# (key, label, higher_is_better) — None means no direction (context only).
METRICS = [
    ("served", "jobs served (all attempts)", None),
    ("applied", "applied", True),
    ("applied_per_day", "applied / day", True),
    ("apply_rate", "applied / served", True),
    ("skip_rate", "skipped / served  (filter miss rate)", False),
    ("block_rate", "blocked / (applied+blocked)", False),
    ("error_rate", "error / served", False),
    ("in_lane_share", "applied in security+it lanes", True),
    ("lane_unknown", "applied with no lane recorded", False),
    ("dup_applies", "duplicate applies", False),
    ("over_company_cap", f"companies past the {COMPANY_CAP}-apply cap", False),
    ("tailor_ok_rate", "résumés tailored (not base fallback)", True),
    ("applied_untailored", "applies that shipped an untailored résumé", False),
    ("waves", "wave sessions", None),
    ("brain_hours", "active brain hours (waves + humans)", None),
    ("applied_per_brain_hour", "applied / active brain hour", True),
    ("mtok_per_apply", "M tokens / applied (all sessions)", False),
    ("turns_per_apply", "model turns / applied", False),
    ("responses_matured", f"applies ≥{MATURE_DAYS}d old", None),
    ("response_rate", "reply/interview rate on matured applies", True),
    ("interviews", "interviews", True),
    ("confirmed_rate", "applies with a confirmation email", True),
    ("action_items", "inbox follow-ups opened", True),
]


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def categorize(reason: str | None, table: list[tuple[str, str]]) -> str:
    r = (reason or "").lower()
    for name, pat in table:
        if re.search(pat, r):
            return name
    return "other"


def gh_form(url: str) -> str:
    u = (url or "").lower()
    if "embed/job_app" in u:
        return "embed"
    if "greenhouse.io" in u:
        return "hosted"
    return "vanity"


def _key(company: str, title: str) -> tuple[str, str]:
    squash = lambda s: re.sub(r"\W+", " ", (s or "").lower()).strip()
    return squash(company), squash(title)


def _div(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def scan_transcripts(root: Path, since: datetime, until: datetime) -> dict:
    """Tokens, turns and session time inside [since, until), split by session kind."""
    out = {"available": root.is_dir(), "kinds": defaultdict(lambda: {"tokens": 0, "out": 0, "turns": 0}),
           "waves": 0, "brain_seconds": 0.0, "models": Counter()}
    if not root.is_dir():
        return out
    floor = since.timestamp()
    for f in root.glob("*.jsonl"):
        try:
            if f.stat().st_mtime < floor:
                continue  # last written before the window: nothing inside it
        except OSError:
            continue
        kind, seen_ids, stamps = None, set(), []
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if kind is None and '"type":"user"' in line:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    c = d.get("message", {}).get("content")
                    text = c if isinstance(c, str) else next(
                        (x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text"), "")
                    if text:
                        kind = ("sweep" if text.startswith("NIGHTLY INBOX SWEEP")
                                else "tailor" if re.match(r"(Check a tailored|You tailor|Rank these|From this job)", text)
                                else "wave" if text.startswith("Next wave")
                                else "other")  # a human turn in Discord, or a subagent
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = _ts(d.get("timestamp"))
                msg = d.get("message") or {}
                if not t or not (since <= t < until) or d.get("type") != "assistant":
                    continue
                mid = msg.get("id") or d.get("requestId")
                if mid in seen_ids:
                    continue  # stream-json repeats usage once per content block
                seen_ids.add(mid)
                u = msg.get("usage") or {}
                k = out["kinds"][kind or "other"]
                k["tokens"] += sum(u.get(x, 0) or 0 for x in ("input_tokens", "cache_read_input_tokens",
                                                              "cache_creation_input_tokens", "output_tokens"))
                k["out"] += u.get("output_tokens", 0) or 0
                k["turns"] += 1
                out["models"][msg.get("model", "?")] += 1
                stamps.append(t)
        if kind == "wave" and stamps:
            out["waves"] += 1
        if kind in ("wave", "other") and stamps:
            stamps.sort()
            out["brain_seconds"] += sum(g for a, b in zip(stamps, stamps[1:])
                                        if (g := (b - a).total_seconds()) <= IDLE_GAP)
    out["kinds"] = dict(out["kinds"])
    return out


def window(since: datetime, until: datetime, data: Path = DATA, transcripts: Path = TRANSCRIPTS) -> dict:
    events = [e for e in _jsonl(data / "apply_events.jsonl") if _ts(e.get("ts"))]
    inw = [e for e in events if since <= _ts(e["ts"]) < until]
    by = Counter(e.get("outcome") for e in inw)
    served = len(inw)
    applied = [e for e in inw if e.get("outcome") == "applied"]
    days = max((until - since).total_seconds() / 86400, 1e-9)
    m: dict = {"since": since.isoformat(), "until": until.isoformat(), "served": served,
               "applied": len(applied), "applied_per_day": round(len(applied) / days, 2)}
    m["apply_rate"] = _div(len(applied), served)
    m["skip_rate"] = _div(by["skipped"], served)
    m["block_rate"] = _div(by["blocked"], len(applied) + by["blocked"])
    m["error_rate"] = _div(by["error"], served)

    lanes = Counter(e.get("lane") or "unknown" for e in applied)
    known = len(applied) - lanes["unknown"]
    m["lanes"] = dict(lanes)
    m["in_lane_share"] = _div(sum(lanes[x] for x in TARGET_LANES), known)
    m["lane_unknown"] = lanes["unknown"]

    m["skip_reasons"] = dict(Counter(categorize(e.get("block_reason"), SKIP_CATEGORIES)
                                     for e in inw if e.get("outcome") == "skipped").most_common())
    m["block_reasons"] = dict(Counter(categorize(e.get("block_reason"), BLOCK_CATEGORIES)
                                      for e in inw if e.get("outcome") in ("blocked", "error")).most_common())

    ats: dict = defaultdict(Counter)
    for e in inw:
        name = e.get("ats") or "?"
        if name == "greenhouse":
            name = f"greenhouse/{gh_form(e.get('url', ''))}"
        ats[name][e.get("outcome")] += 1
    m["ats"] = {a: {"applied": c["applied"], "blocked": c["blocked"], "served": sum(c.values()),
                    "submit_rate": _div(c["applied"], c["applied"] + c["blocked"])}
                for a, c in sorted(ats.items(), key=lambda x: -sum(x[1].values()))}

    # Duplicates: an apply whose posting (by url, or by company+title) already landed earlier.
    prior_urls, prior_keys, dups = set(), set(), 0
    for e in sorted((e for e in events if e.get("outcome") == "applied"), key=lambda e: _ts(e["ts"])):
        u, k = _norm(e.get("url", "")), _key(e.get("company"), e.get("title"))
        if since <= _ts(e["ts"]) < until and (u in prior_urls or k in prior_keys):
            dups += 1
        prior_urls.add(u)
        prior_keys.add(k)
    m["dup_applies"] = dups

    lifetime = Counter(_key(e.get("company"), "")[0] for e in events
                       if e.get("outcome") == "applied" and _ts(e["ts"]) < until)
    window_cos = {_key(e.get("company"), "")[0] for e in applied}
    m["over_company_cap"] = sum(1 for c in window_cos if lifetime[c] > COMPANY_CAP)
    m["top_companies"] = dict(Counter(e.get("company") for e in applied).most_common(3))

    tl = [r for r in _csv(data / "tailor_log.csv") if (t := _ts(r.get("ts"))) and since <= t < until]
    tstat = Counter(r.get("status") for r in tl)
    m["tailor_runs"] = len(tl)
    m["tailor_ok_rate"] = _div(tstat["tailored"], len(tl))
    m["tailor_fallback_reasons"] = dict(Counter((r.get("reason") or "")[:60] for r in tl
                                                if r.get("status") != "tailored").most_common(3))
    m["applied_untailored"] = sum(1 for e in applied if e.get("resume_status") not in ("tailored", None))

    tx = scan_transcripts(transcripts, since, until)
    m["transcripts"] = tx["available"]
    if tx["available"]:
        tok = sum(k["tokens"] for k in tx["kinds"].values())
        turns = sum(k["turns"] for k in tx["kinds"].values())
        m["waves"] = tx["waves"]
        m["brain_hours"] = round(tx["brain_seconds"] / 3600, 2)
        m["applied_per_brain_hour"] = _div(len(applied), tx["brain_seconds"] / 3600)
        m["mtok_per_apply"] = _div(tok / 1e6, len(applied))
        m["turns_per_apply"] = _div(turns, len(applied))
        m["tokens_by_kind"] = {k: v["tokens"] for k, v in tx["kinds"].items()}
        m["models"] = dict(tx["models"])

    # Outcomes. Only applications old enough to have drawn a reply count toward the rate.
    responses = _jsonl(data / "responses.jsonl")
    by_key: dict = defaultdict(set)
    for r in responses:
        by_key[_key(r.get("company"), r.get("title"))].add(r.get("kind"))
        by_key[(_key(r.get("company"), "")[0], "*")].add(r.get("kind"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=MATURE_DAYS)
    matured = [e for e in applied if _ts(e["ts"]) <= cutoff]

    def kinds(e: dict) -> set:
        k = _key(e.get("company"), e.get("title"))
        return by_key.get(k, set()) | ({"confirmation"} & by_key.get((k[0], "*"), set()))

    m["responses_logged"] = len(responses)
    m["responses_matured"] = len(matured)
    m["response_rate"] = _div(sum(1 for e in matured if kinds(e) & {"reply", "interview"}), len(matured)) \
        if responses else None
    m["interviews"] = sum(1 for e in applied if "interview" in kinds(e)) if responses else None
    m["confirmed_rate"] = _div(sum(1 for e in applied if kinds(e)), len(applied)) if responses else None
    m["action_items"] = sum(1 for r in _csv(data / "action_items.csv")
                            if (t := _ts(r.get("date_added"))) and since <= t < until)
    return m


PERCENT = {"apply_rate", "skip_rate", "block_rate", "error_rate", "in_lane_share", "tailor_ok_rate",
           "response_rate", "confirmed_rate"}


def _fmt(v, pct: bool = False) -> str:
    if v is None:
        return "—"
    if pct:
        return f"{v:.1%}"
    return f"{v:.3g}" if isinstance(v, float) else str(v)


def _verdict(a, b, better) -> str:
    if better is None or a is None or b is None or a == b:
        return ""
    return "  better" if (b > a) == better else "  WORSE"


def render(ws: list[dict]) -> str:
    lines = []
    head = [f"{w['since'][:10]}→{w['until'][:10]}" for w in ws]
    lines.append(f"{'metric':44}" + "".join(f"{h:>24}" for h in head))
    for key, label, better in METRICS:
        vals = [w.get(key) for w in ws]
        row = f"{label:44}" + "".join(f"{_fmt(v, key in PERCENT):>24}" for v in vals)
        if len(ws) == 2:
            row += _verdict(vals[0], vals[1], better)
        lines.append(row)
    for w, h in zip(ws, head):
        lines.append(f"\n[{h}]")
        lines.append(f"  lanes: {w['lanes']}")
        lines.append(f"  skip reasons: {w['skip_reasons']}")
        lines.append(f"  block/error reasons: {w['block_reasons']}")
        ats = ", ".join(f"{a} {c['applied']}/{c['served']} (submit {_fmt(c['submit_rate'], True)})"
                        for a, c in w["ats"].items())
        lines.append(f"  ats applied/served: {ats}")
        lines.append(f"  top companies: {w['top_companies']}")
        if w.get("tailor_fallback_reasons"):
            lines.append(f"  tailoring fallbacks: {w['tailor_fallback_reasons']}")
        if w.get("tokens_by_kind"):
            lines.append(f"  tokens by session kind: {w['tokens_by_kind']}  models: {w.get('models')}")
        if not w.get("transcripts"):
            lines.append("  (no transcripts dir — token/time metrics skipped)")
        if not w["responses_logged"]:
            lines.append("  (data/responses.jsonl is empty — outcome metrics cannot be computed)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=7, help="window length for the default comparison")
    ap.add_argument("--split", help="change date (UTC, YYYY-MM-DD or ISO): compare before vs after")
    ap.add_argument("--since", help="single window start")
    ap.add_argument("--until", help="single window end (default now)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--transcripts", default=str(TRANSCRIPTS))
    a = ap.parse_args(argv[1:])
    now = datetime.now(timezone.utc)
    data, tx = Path(a.data), Path(a.transcripts)

    if a.since:
        spans = [(_ts(a.since), _ts(a.until) if a.until else now)]
    elif a.split:
        s = _ts(a.split)
        spans = [(s - (now - s), s), (s, now)]
    else:
        d = timedelta(days=a.days)
        spans = [(now - 2 * d, now - d), (now - d, now)]
    if any(x is None for span in spans for x in span):
        ap.error("could not parse a date")

    ws = [window(s, u, data, tx) for s, u in spans]
    print(json.dumps(ws, indent=2, default=str) if a.json else render(ws))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
