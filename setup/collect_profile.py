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

    output_path = Path("profile.yaml")
    with open(output_path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, allow_unicode=True)

    print("")
    print("Profile saved to: " + str(output_path.absolute()))
    print("You can edit profile.yaml directly at any time.")


if __name__ == "__main__":
    main()
