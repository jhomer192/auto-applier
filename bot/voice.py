"""Voice fingerprinting — collects writing samples, extracts style, persists profile."""
import logging
import os
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

VOICE_PROFILE_PATH_DEFAULT = "data/voice_profile.yaml"


def get_voice_profile_path() -> str:
    """Return the path to the voice profile YAML, honouring the env override."""
    return os.getenv("VOICE_PROFILE_PATH", VOICE_PROFILE_PATH_DEFAULT)


def load_voice_profile() -> dict | None:
    """Load voice profile from YAML.

    Returns:
        Parsed profile dict, or None if the file does not exist.
    """
    path = Path(get_voice_profile_path())
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f) or None


def save_voice_profile(profile: dict) -> None:
    """Persist voice profile to YAML.

    Args:
        profile: Voice profile dict to write.
    """
    path = Path(get_voice_profile_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, allow_unicode=True)


def voice_profile_summary(vp: dict) -> str:
    """Return a brief human-readable summary of a voice profile.

    Args:
        vp: Voice profile dict.

    Returns:
        Multi-line summary string, or "Profile captured" when vp is empty.
    """
    lines = []
    if vp.get("tone"):
        lines.append(f"Tone: {vp['tone']}")
    if vp.get("vocabulary_level"):
        lines.append(f"Vocabulary: {vp['vocabulary_level']}")
    if vp.get("quirks"):
        quirks = vp["quirks"][:3]
        lines.append(f"Style markers: {', '.join(quirks)}")
    return "\n".join(lines) if lines else "Profile captured"
