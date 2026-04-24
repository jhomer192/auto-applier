import yaml

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
