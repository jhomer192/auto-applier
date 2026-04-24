#!/usr/bin/env python3
"""
Run by Claude Code during first-time setup.
Asks the user questions and writes profile.yaml.
"""
import sys
import yaml
from pathlib import Path


def ask(prompt: str, required: bool = False, multiline: bool = False) -> str:
    while True:
        if multiline:
            print(f"{prompt} (type END on a blank line to finish):")
            lines = []
            while True:
                line = input()
                if line.strip() == "END":
                    break
                lines.append(line)
            value = "
".join(lines).strip()
        else:
            value = input(f"{prompt}: ").strip()

        if value or not required:
            return value
        print("  (This field is required. Please enter a value.)")


def collect_work_history() -> list[dict]:
    history = []
    try:
        count = int(input("How many jobs do you want to include (0 to skip)? ").strip() or "0")
    except ValueError:
        count = 0

    for i in range(count):
        print(f"
Job {i+1}:")
        job = {
            "title": ask("  Job title", required=True),
            "company": ask("  Company name", required=True),
            "start": ask("  Start date (YYYY-MM)", required=True),
            "end": ask("  End date (YYYY-MM) or 'present'"),
            "description": ask("  Key responsibilities/achievements (bullet points)", multiline=True),
        }
        if not job["end"]:
            job["end"] = "present"
        history.append(job)
    return history


def collect_education() -> list[dict]:
    education = []
    try:
        count = int(input("How many degrees to include (0 to skip)? ").strip() or "0")
    except ValueError:
        count = 0

    for i in range(count):
        print(f"
Degree {i+1}:")
        edu = {
            "degree": ask("  Degree (e.g. B.S. Computer Science)", required=True),
            "school": ask("  School name", required=True),
            "year": ask("  Graduation year"),
        }
        education.append(edu)
    return education


def main() -> None:
    print("=" * 60)
    print("Auto Job Applier — Profile Setup")
    print("=" * 60)
    print("I'll ask you some questions to build your profile.")
    print("This information will only ever be used exactly as you provide it.
")

    profile: dict = {}

    print("── Basic Information ──")
    profile["name"] = ask("Full name", required=True)
    profile["email"] = ask("Email address", required=True)
    profile["phone"] = ask("Phone number", required=True)
    profile["location"] = ask("City and state (e.g. San Francisco, CA)", required=True)

    resume_path = ask("Absolute path to your resume PDF", required=True)
    while not Path(resume_path).exists():
        print(f"  File not found: {resume_path}")
        resume_path = ask("  Please enter a valid path", required=True)
    profile["resume_path"] = resume_path

    print("
── Professional Summary (optional) ──")
    summary = ask("2-3 sentence professional summary (press Enter to skip)", multiline=False)
    if summary:
        profile["summary"] = summary

    print("
── Work History ──")
    profile["work_history"] = collect_work_history()

    print("
── Education ──")
    profile["education"] = collect_education()

    print("
── Skills ──")
    skills_raw = ask("List your top skills, comma-separated (e.g. Python, SQL, React)")
    profile["skills"] = [s.strip() for s in skills_raw.split(",") if s.strip()]

    print("
── Online Profiles (optional) ──")
    profile["links"] = {
        "linkedin": ask("LinkedIn URL (press Enter to skip)"),
        "github": ask("GitHub URL (press Enter to skip)"),
        "portfolio": ask("Portfolio/personal site URL (press Enter to skip)"),
    }

    print("
── EEO Demographics (optional — only used for voluntary diversity forms) ──")
    print("Press Enter to skip any of these.
")
    profile["demographics"] = {
        "gender": ask("Gender"),
        "ethnicity": ask("Ethnicity"),
        "veteran_status": ask("Veteran status (e.g. 'Not a veteran')"),
        "disability_status": ask("Disability status (e.g. 'No disability')"),
        "authorized_to_work": ask("Authorized to work in (e.g. 'US')"),
        "requires_sponsorship": ask("Requires visa sponsorship? (yes/no)").lower() in ("yes", "y"),
    }

    output_path = Path("profile.yaml")
    with open(output_path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, allow_unicode=True)

    print(f"
Profile saved to: {output_path.absolute()}")
    print("You can edit profile.yaml directly at any time to update your information.")


if __name__ == "__main__":
    main()
