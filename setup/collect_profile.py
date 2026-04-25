#!/usr/bin/env python3
"""Run by Claude Code during first-time setup. Writes profile.yaml."""
import yaml
from pathlib import Path


def ask(prompt: str, required: bool = False, multiline: bool = False) -> str:
    nl = "\n"
    while True:
        if multiline:
            print(prompt + " (type END on a blank line to finish):")
            lines = []
            while True:
                line = input()
                if line.strip() == "END":
                    break
                lines.append(line)
            value = nl.join(lines).strip()
        else:
            value = input(prompt + ": ").strip()
        if value or not required:
            return value
        print("  (This field is required. Please enter a value.)")


def collect_work_history() -> list[dict]:
    history = []
    try:
        count = int(input("How many jobs to include (0 to skip)? ").strip() or "0")
    except ValueError:
        count = 0
    for i in range(count):
        print("")
        print("Job " + str(i + 1) + ":")
        job = {
            "title": ask("  Job title", required=True),
            "company": ask("  Company name", required=True),
            "start": ask("  Start date (YYYY-MM)", required=True),
            "end": ask("  End date (YYYY-MM) or present") or "present",
            "description": ask("  Key responsibilities/achievements", multiline=True),
        }
        history.append(job)
    return history


def collect_education() -> list[dict]:
    education = []
    try:
        count = int(input("How many degrees to include (0 to skip)? ").strip() or "0")
    except ValueError:
        count = 0
    for i in range(count):
        print("")
        print("Degree " + str(i + 1) + ":")
        edu = {
            "degree": ask("  Degree (e.g. B.S. Computer Science)", required=True),
            "school": ask("  School name", required=True),
            "year": ask("  Graduation year"),
        }
        education.append(edu)
    return education


def main() -> None:
    sep = "=" * 60
    print(sep)
    print("Auto Job Applier -- Profile Setup")
    print(sep)
    print("I will ask questions to build your job application profile.")
    print("Your answers are used exactly as provided -- nothing is invented.")
    print("")

    profile: dict = {}

    print("-- Basic Information --")
    profile["name"] = ask("Full name", required=True)
    profile["email"] = ask("Email address", required=True)
    profile["phone"] = ask("Phone number", required=True)
    profile["location"] = ask("City and state (e.g. San Francisco, CA)", required=True)

    resume_path = ask("Absolute path to your resume PDF", required=True)
    while not Path(resume_path).exists():
        print("  File not found: " + resume_path)
        resume_path = ask("  Please enter a valid path", required=True)
    profile["resume_path"] = resume_path

    print("")
    print("-- Professional Summary (optional) --")
    summary = ask("2-3 sentence professional summary (Enter to skip)")
    if summary:
        profile["summary"] = summary

    print("")
    print("-- Work History --")
    profile["work_history"] = collect_work_history()

    print("")
    print("-- Education --")
    profile["education"] = collect_education()

    print("")
    print("-- Skills --")
    skills_raw = ask("Top skills, comma-separated (e.g. Python, SQL, React)")
    profile["skills"] = [s.strip() for s in skills_raw.split(",") if s.strip()]

    print("")
    print("-- Online Profiles (optional) --")
    profile["links"] = {
        "linkedin": ask("LinkedIn URL (Enter to skip)"),
        "github": ask("GitHub URL (Enter to skip)"),
        "portfolio": ask("Portfolio/personal site URL (Enter to skip)"),
    }

    print("")
    print("-- EEO Demographics (optional) --")
    print("Press Enter to skip any of these.")
    print("")
    profile["demographics"] = {
        "gender": ask("Gender"),
        "ethnicity": ask("Ethnicity"),
        "veteran_status": ask("Veteran status (e.g. Not a veteran)"),
        "disability_status": ask("Disability status (e.g. No disability)"),
        "authorized_to_work": ask("Authorized to work in (e.g. US)"),
        "requires_sponsorship": ask("Requires visa sponsorship? (yes/no)").lower() in ("yes", "y"),
    }

    print("")
    print("-- Job Preferences (optional but recommended) --")
    print("These tell the bot which jobs to target, auto-skip, or auto-apply to.")
    print("Press Enter to skip any field.")
    print("")
    profile["job_preferences"] = _collect_preferences()

    output_path = Path("profile.yaml")
    with open(output_path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, allow_unicode=True)

    print("")
    print("Profile saved to: " + str(output_path.absolute()))
    print("You can edit profile.yaml directly at any time.")
    print("You can also update preferences at any time with /prefs in Telegram.")

    _setup_gmail_env()


def _collect_preferences() -> dict:
    """Collect job preferences interactively. All fields optional."""
    prefs: dict = {}

    # Desired roles
    roles_raw = ask("Desired role types, comma-separated (e.g. Software Engineer,Backend Engineer)")
    if roles_raw:
        prefs["desired_roles"] = [r.strip().lower() for r in roles_raw.split(",") if r.strip()]

    # Salary
    min_sal_raw = ask("Minimum acceptable annual salary in USD (e.g. 180000)")
    if min_sal_raw:
        try:
            prefs["min_salary"] = int(min_sal_raw.replace(",", "").replace("$", ""))
        except ValueError:
            print("  Skipping — could not parse salary.")
    target_sal_raw = ask("Target annual salary in USD (e.g. 220000)")
    if target_sal_raw:
        try:
            prefs["target_salary"] = int(target_sal_raw.replace(",", "").replace("$", ""))
        except ValueError:
            print("  Skipping — could not parse salary.")

    # Seniority
    sen_raw = ask("Acceptable seniority levels, comma-separated (junior/mid/senior/staff/principal/director)")
    if sen_raw:
        prefs["seniority"] = [s.strip().lower() for s in sen_raw.split(",") if s.strip()]

    # Work arrangement
    arr_raw = ask("Work arrangement preference, comma-separated (remote/hybrid/onsite)")
    if arr_raw:
        valid = {"remote", "hybrid", "onsite"}
        chosen = [a.strip().lower() for a in arr_raw.split(",") if a.strip().lower() in valid]
        if chosen:
            prefs["work_arrangement"] = chosen

    # Excluded companies
    exc_raw = ask("Companies to exclude, comma-separated (e.g. Meta,Amazon)")
    if exc_raw:
        prefs["excluded_companies"] = [c.strip() for c in exc_raw.split(",") if c.strip()]

    # Auto-apply threshold
    print("")
    print("Auto-apply: when a job's match score meets the threshold AND all filters pass,")
    print("the bot submits the application without asking you. Set 0 to always ask.")
    auto_raw = ask("Auto-apply threshold (0-100, 0 = disabled)")
    if auto_raw:
        try:
            val = int(auto_raw)
            if 0 <= val <= 100:
                prefs["auto_apply_threshold"] = val
        except ValueError:
            print("  Skipping — invalid number.")

    return prefs


def _setup_gmail_env() -> None:
    """Optionally write GMAIL_ADDRESS / GMAIL_APP_PASSWORD into .env."""
    sep = "=" * 60
    print("")
    print(sep)
    print("Gmail Inbox (optional)")
    print(sep)
    print("The bot can watch your Gmail inbox for recruiter replies and")
    print("let you respond directly from Telegram.")
    print("")
    use_gmail = input("Set up Gmail inbox? (y/N): ").strip().lower()
    if use_gmail not in ("y", "yes"):
        print("Skipping Gmail setup. You can add GMAIL_ADDRESS and")
        print("GMAIL_APP_PASSWORD to .env later to enable it.")
        return

    print("")
    print("You need a Gmail App Password (not your normal password).")
    print("Create one at: https://myaccount.google.com/apppasswords")
    print("(Requires 2-Step Verification to be enabled.)")
    print("")
    gmail_address = input("Gmail address: ").strip()
    gmail_password = input("App Password (16-char, spaces ok): ").strip().replace(" ", "")

    if not gmail_address or not gmail_password:
        print("Skipping Gmail setup — incomplete input.")
        return

    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text("")

    content = env_path.read_text()
    lines = content.splitlines()

    def _set_var(lines: list, key: str, value: str) -> list:
        for i, line in enumerate(lines):
            if line.startswith(key + "="):
                lines[i] = key + "=" + value
                return lines
        lines.append(key + "=" + value)
        return lines

    lines = _set_var(lines, "GMAIL_ADDRESS", gmail_address)
    lines = _set_var(lines, "GMAIL_APP_PASSWORD", gmail_password)
    env_path.write_text("\n".join(lines) + "\n")

    print("")
    print("Gmail credentials saved to .env.")
    print("The bot will poll your inbox every 5 minutes and notify you of new recruiter emails.")


if __name__ == "__main__":
    main()
