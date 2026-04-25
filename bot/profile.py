import yaml

from bot.models import JobPreferences

REQUIRED_KEYS = ["name", "email", "phone", "location", "work_history", "education", "skills"]


class ProfileError(Exception):
    pass


def load_profile(path: str = "profile.yaml") -> dict:
    """Load and validate a candidate profile from YAML.

    Args:
        path: Filesystem path to the profile YAML file.

    Returns:
        Parsed profile as a dict.

    Raises:
        ProfileError: If the file is missing, invalid YAML, or missing required keys.
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ProfileError(f"Profile file not found: {path}")
    except yaml.YAMLError as e:
        raise ProfileError(f"Invalid YAML: {e}")

    if not isinstance(data, dict):
        raise ProfileError("profile.yaml must be a YAML mapping")

    missing = [k for k in REQUIRED_KEYS if k not in data or data[k] is None]
    if missing:
        raise ProfileError(f"Missing required profile fields: {chr(44).join(missing)}")

    return data


def load_preferences(profile: dict) -> JobPreferences:
    """Extract job preferences from a loaded profile dict.

    The `job_preferences` block in profile.yaml is optional.
    Returns a default (permissive) JobPreferences when not set.

    Args:
        profile: Profile dict returned by load_profile().

    Returns:
        JobPreferences instance. All fields default to "no filter" when absent.
    """
    raw = profile.get("job_preferences") or {}
    if not isinstance(raw, dict):
        return JobPreferences()

    def _int(key: str) -> int:
        try:
            return int(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _strlist(key: str) -> list[str]:
        val = raw.get(key) or []
        if isinstance(val, list):
            return [str(v).strip().lower() for v in val if v]
        if isinstance(val, str):
            return [v.strip().lower() for v in val.split(",") if v.strip()]
        return []

    requires_sponsorship_raw = raw.get("requires_sponsorship", False)
    requires_sponsorship = bool(requires_sponsorship_raw) if requires_sponsorship_raw is not None else False

    auto_search_raw = raw.get("auto_search", True)
    auto_search = bool(auto_search_raw) if auto_search_raw is not None else True

    return JobPreferences(
        desired_roles=_strlist("desired_roles"),
        min_salary=_int("min_salary"),
        target_salary=_int("target_salary"),
        seniority=_strlist("seniority"),
        work_arrangement=_strlist("work_arrangement"),
        excluded_companies=[str(c).strip() for c in (raw.get("excluded_companies") or []) if c],
        auto_apply_threshold=_int("auto_apply_threshold"),
        min_apply_gap_minutes=_int("min_apply_gap_minutes") or 4,
        max_apply_gap_minutes=_int("max_apply_gap_minutes") or 8,
        max_applies_per_day=_int("max_applies_per_day") or 30,
        requires_sponsorship=requires_sponsorship,
        auto_search=auto_search,
    )


def save_preferences(profile: dict, prefs: JobPreferences, path: str) -> None:
    """Write updated job_preferences back to profile.yaml.

    Args:
        profile: Currently loaded profile dict (mutated in-place).
        prefs: Updated preferences to persist.
        path: Path to profile.yaml.
    """
    profile["job_preferences"] = {
        "desired_roles": prefs.desired_roles,
        "min_salary": prefs.min_salary,
        "target_salary": prefs.target_salary,
        "seniority": prefs.seniority,
        "work_arrangement": prefs.work_arrangement,
        "excluded_companies": prefs.excluded_companies,
        "auto_apply_threshold": prefs.auto_apply_threshold,
        "min_apply_gap_minutes": prefs.min_apply_gap_minutes,
        "max_apply_gap_minutes": prefs.max_apply_gap_minutes,
        "max_applies_per_day": prefs.max_applies_per_day,
        "requires_sponsorship": prefs.requires_sponsorship,
    }
    with open(path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, allow_unicode=True)
