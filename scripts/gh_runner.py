#!/usr/bin/env python3
"""gh_runner.py — Claude-free Greenhouse applier loop.

Sources wide (backlog + a 3000-board mining pass, seniority filter off), submits each
Greenhouse job with scripts/gh_submit.mjs, and records the result. Jobs whose required
questions the rule tables can't answer go to data/gh_need_human.jsonl for the Claude wave
(or a human) and are not retried here. Runs until killed.

    python3 scripts/gh_runner.py [--dry] [--once] [--limit N]
"""
import argparse, json, re, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import yaml  # noqa: E402

DATA = ROOT / "data"
NEED = DATA / "gh_need_human.jsonl"
PROFILE_JSON = DATA / "gh_profile.json"
MONTHS = "January February March April May June July August September October November December".split()
STATES = {"VA": "Virginia", "CA": "California", "NY": "New York", "TX": "Texas", "IL": "Illinois", "MD": "Maryland", "DC": "District of Columbia"}


def build_profile() -> None:
    p = yaml.safe_load((ROOT / "profile.yaml").read_text())
    first, *rest = p["name"].split()
    job = p["work_history"][0]
    sy, sm = job["start"].split("-")
    ey, em = job["end"].split("-")
    school = p["education"][0]["school"]
    clearance = p["clearance"].split(". ")[0].rstrip(".") + "."
    state_long = STATES.get(p["state"], p["state"])
    out = {
        "name": p["name"], "first": first, "last": rest[-1], "preferred": p.get("preferred_name") or first,
        "email": p["email"], "phone": p["phone"],
        "linkedin": p["links"]["linkedin"], "github": p["links"]["github"], "website": p["links"]["website"],
        "city": p["city"], "state": p["state"], "state_long": state_long, "zip": str(p["zip"]),
        "address_full": f'{p["street_address"]}, {p["city"]}, {p["state"]} {p["zip"]}',
        "school": school, "clearance": clearance,
        "current_company": f"{school} (M.S. student)", "current_title": "Graduate Student, " + p["education"][0]["degree"].split(" (")[0],
        "skills": p.get("skills", []),
        "job_company": job["company"], "job_title": job["title"],
        "job_start_month": MONTHS[int(sm) - 1], "job_end_month": MONTHS[int(em) - 1],
        "job_start_year": sy, "job_end_year": ey,
        # label-regex answers learned from tonight's hand submits (all true of the candidate)
        "extra": {
            "referr.*\\*$": "No",
            "primary residence|city and state|city, state|current location": f'{p["city"]}, {p["state"]}',
            "u\\.s\\. person|export.control|u\\.s\\. citizen": "Yes",
            "meet this requirement": "No",
            "^state\\s*\\*?$": state_long,
            "government (employee|official)|procurement|contract award": "No",
        },
    }
    PROFILE_JSON.write_text(json.dumps(out, indent=1))
    PROFILE_JSON.chmod(0o600)


def source(args: list[str]) -> list[dict]:
    r = subprocess.run([sys.executable, "scripts/source.py", "--platforms", "greenhouse", *args],
                       cwd=ROOT, capture_output=True, text=True)
    jobs = []
    for line in r.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 5 and ("gh_jid=" in f[0] or re.search(r"greenhouse\.io/[^/]+/jobs/\d+", f[0])):
            jobs.append(dict(zip(["url", "company", "title", "location", "lane"], f)))
    return jobs


def applied(url: str) -> bool:
    r = subprocess.run([sys.executable, "scripts/applied.py", "check", url], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() not in ("", "NONE")


def need_human_urls() -> set[str]:
    if not NEED.exists():
        return set()
    return {json.loads(l)["url"] for l in NEED.read_text().splitlines() if l.strip()}


def log(company, title, url, outcome, reason="", elapsed=0):
    cmd = [sys.executable, "scripts/apply_log.py", "--company", company, "--title", title, "--ats", "greenhouse",
           "--url", url, "--outcome", outcome, "--elapsed-s", str(int(elapsed))]
    if reason:
        cmd += ["--block-reason", reason[:200]]
    subprocess.run(cmd, cwd=ROOT, capture_output=True)


def run_one(job: dict, dry: bool) -> str:
    t0 = time.time()
    url = job["url"]
    if "gh_jid=" not in url and not re.search(r"greenhouse\.io/.+/jobs/\d+", url):
        print(f"{time.strftime('%H:%M:%S')} SKIP       {job['company']} — {job['title']}  {url}", flush=True)
        return "SKIP"
    cmd = ["node", "scripts/gh_submit.mjs", "--profile", str(PROFILE_JSON), "--url", url, "--company", job["company"]] + (["--dry"] if dry else [])
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        res = {"state": "ERRORS", "pending": [p.stderr[-300:]]}
    st, dt = res["state"], time.time() - t0
    company, title = res.get("company") or job["company"], job["title"]
    if dry:
        pass  # dry run: report only, record nothing
    elif st == "SUBMITTED":
        subprocess.run([sys.executable, "scripts/applied.py", "record", job["url"], company, title, "greenhouse"], cwd=ROOT)
        log(company, title, job["url"], "applied", elapsed=dt)
    elif st == "NEED_HUMAN":
        with NEED.open("a") as f:
            f.write(json.dumps({"ts": int(time.time()), **job, "pending": res.get("pending", [])}) + "\n")
    if st in ("CLOSED", "BLOCKED", "ERRORS") and not dry:  # don't re-serve from mining rounds
        subprocess.run([sys.executable, "scripts/seen.py", "mark", job["url"], company, title, job.get("location", "")], cwd=ROOT, capture_output=True)
    if dry:
        pass
    elif st == "CLOSED":
        log(company, title, job["url"], "skipped", "posting closed / no form", dt)
    elif st == "BLOCKED":
        log(company, title, job["url"], "blocked", res.get("reason", ""), dt)
    elif st == "ERRORS":
        log(company, title, job["url"], "error", json.dumps(res.get("pending", []))[:200], dt)
    print(f"{time.strftime('%H:%M:%S')} {st:10} {dt:5.0f}s  {company} — {title}"
          + (f"  {json.dumps(res.get('pending'))[:300]}" if st in ("NEED_HUMAN", "ERRORS") else "")
          + (f"  {res.get('reason', '')}" if st == "BLOCKED" else ""), flush=True)
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="fill but don't submit or record")
    ap.add_argument("--once", action="store_true", help="one sourcing round, then exit")
    ap.add_argument("--limit", type=int, default=0, help="stop after N jobs (for testing)")
    a = ap.parse_args()
    build_profile()
    done = 0
    while True:
        jobs = source(["--backlog", "--n", "200", "--max-years", "0"]) + source(["--n", "150", "--boards", "3000", "--max-years", "0"])
        skip = need_human_urls()
        seen_now = set()
        fresh = [j for j in jobs if j["url"] not in skip and not (j["url"] in seen_now or seen_now.add(j["url"]))]
        print(f"{time.strftime('%H:%M:%S')} round: {len(jobs)} sourced, {len(fresh)} to try", flush=True)
        for j in fresh:
            if applied(j["url"]):  # the Claude wave may have taken it meanwhile
                continue
            run_one(j, a.dry)
            done += 1
            if a.limit and done >= a.limit:
                return 0
        if a.once:
            return 0
        if not fresh:
            time.sleep(1800)  # idle: nothing new to source yet; wait for boards to post more


if __name__ == "__main__":
    raise SystemExit(main())
