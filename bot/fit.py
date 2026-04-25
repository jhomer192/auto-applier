"""Job fit evaluation: compares a JobAnalysis against the user's JobPreferences.

Returns a FitReport that drives three possible paths in the bot:
  hard_pass  → auto-skip, no Y/N prompt
  auto_apply → skip Y/N, apply immediately
  else       → show enriched Y/N prompt with fit details
"""
from bot.models import FitReport, JobAnalysis, JobPreferences


# Salary is a "hard pass" when it is definitively stated (not estimated)
# AND is below this fraction of the user's minimum.
_HARD_PASS_SALARY_FRACTION = 0.80


def _fmt_salary(amount: int) -> str:
    if amount >= 1000:
        return f"${amount // 1000}k"
    return f"${amount}"


def _role_matches(role_type: str, desired_roles: list[str]) -> bool:
    """Return True if role_type fuzzy-matches any entry in desired_roles."""
    if not desired_roles or not role_type:
        return True  # no preference set → always ok
    role_lower = role_type.lower()
    return any(desired.lower() in role_lower or role_lower in desired.lower()
               for desired in desired_roles)


def evaluate_fit(job: JobAnalysis, prefs: JobPreferences) -> FitReport:
    """Evaluate a job against the user's preferences.

    Args:
        job: Fully-analyzed job posting.
        prefs: User's job preferences from profile.yaml.

    Returns:
        FitReport describing what passed, what failed, and which fast-path to take.
    """
    report = FitReport()

    # --- Excluded companies ---
    if prefs.excluded_companies:
        company_lower = job.company.lower()
        for exc in prefs.excluded_companies:
            if exc.lower() in company_lower or company_lower in exc.lower():
                report.excluded_company = True
                report.hard_pass = True
                report.hard_pass_reason = f"{job.company} is on your excluded-companies list."
                return report  # nothing else matters

    # --- Salary ---
    if prefs.min_salary > 0 and (job.salary_min > 0 or job.salary_max > 0):
        posted = job.salary_max if job.salary_max > 0 else job.salary_min
        estimated_tag = " (est.)" if job.salary_is_estimated else ""
        salary_str = _fmt_salary(job.salary_min) if job.salary_min else ""
        if job.salary_max > 0 and job.salary_max != job.salary_min:
            salary_str = (f"{_fmt_salary(job.salary_min)}–{_fmt_salary(job.salary_max)}"
                          if job.salary_min else _fmt_salary(job.salary_max))
        salary_str += estimated_tag

        if posted < prefs.min_salary:
            report.salary_ok = False
            gap = prefs.min_salary - posted
            report.salary_note = (
                f"Posted {salary_str} — {_fmt_salary(gap)} below your floor of "
                f"{_fmt_salary(prefs.min_salary)} ⚠️"
            )
            # Hard pass only if definitively posted (not estimated) and badly below floor
            if not job.salary_is_estimated and posted < prefs.min_salary * _HARD_PASS_SALARY_FRACTION:
                report.hard_pass = True
                report.hard_pass_reason = (
                    f"Salary {salary_str} is more than 20% below your floor of "
                    f"{_fmt_salary(prefs.min_salary)}."
                )
        else:
            report.salary_note = f"Salary: {salary_str} ✅"
    elif prefs.min_salary > 0 and not job.salary_min and not job.salary_max:
        report.salary_note = "Salary not posted — couldn't verify against your floor."
    else:
        report.salary_note = ""

    if report.hard_pass:
        return report

    # --- Role type ---
    if prefs.desired_roles:
        if job.role_type and not _role_matches(job.role_type, prefs.desired_roles):
            report.role_ok = False
            report.role_note = (
                f"Role type \"{job.role_type}\" doesn't match your desired roles: "
                f"{', '.join(prefs.desired_roles)} ⚠️"
            )
        else:
            report.role_note = f"Role: {job.role_type or job.title} ✅"

    # --- Seniority ---
    if prefs.seniority and job.seniority_level != "unknown":
        if job.seniority_level not in [s.lower() for s in prefs.seniority]:
            report.seniority_ok = False
            report.seniority_note = (
                f"Level is \"{job.seniority_level}\" — your preferred levels: "
                f"{', '.join(prefs.seniority)} ⚠️"
            )
        else:
            report.seniority_note = f"Level: {job.seniority_level} ✅"

    # --- Work arrangement ---
    if prefs.work_arrangement and job.work_arrangement != "unknown":
        if job.work_arrangement not in [w.lower() for w in prefs.work_arrangement]:
            report.arrangement_ok = False
            report.arrangement_note = (
                f"Arrangement is {job.work_arrangement} — you prefer "
                f"{'/'.join(prefs.work_arrangement)} ⚠️"
            )
        else:
            report.arrangement_note = f"Arrangement: {job.work_arrangement} ✅"

    # --- Auto-apply ---
    all_checks_pass = (
        report.salary_ok
        and report.role_ok
        and report.seniority_ok
        and report.arrangement_ok
    )
    if (
        prefs.auto_apply_threshold > 0
        and job.match_score >= prefs.auto_apply_threshold
        and all_checks_pass
    ):
        report.auto_apply = True

    return report


def fit_summary_lines(job: JobAnalysis, report: FitReport) -> list[str]:
    """Build a list of display lines for the Telegram Y/N prompt."""
    lines: list[str] = []

    # Salary
    if report.salary_note:
        lines.append(report.salary_note)

    # Role
    if report.role_note:
        lines.append(report.role_note)

    # Seniority
    if report.seniority_note:
        lines.append(report.seniority_note)

    # Arrangement
    if report.arrangement_note:
        lines.append(report.arrangement_note)

    # Fallback for known fields when no preference set
    if not report.salary_note and (job.salary_min or job.salary_max):
        lo = _fmt_salary(job.salary_min) if job.salary_min else ""
        hi = _fmt_salary(job.salary_max) if job.salary_max else ""
        rng = f"{lo}–{hi}" if lo and hi else (lo or hi)
        tag = " (est.)" if job.salary_is_estimated else ""
        lines.append(f"Salary: {rng}{tag}")
    if not report.arrangement_note and job.work_arrangement not in ("", "unknown"):
        lines.append(f"Arrangement: {job.work_arrangement}")
    if not report.seniority_note and job.seniority_level not in ("", "unknown"):
        lines.append(f"Level: {job.seniority_level}")

    return lines
