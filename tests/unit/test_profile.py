import pytest
import yaml
from pathlib import Path
from bot.profile import load_profile, ProfileError, REQUIRED_KEYS


def test_load_valid_profile(valid_profile):
    path, expected = valid_profile
    result = load_profile(path)
    assert result["name"] == expected["name"]


def test_load_valid_profile_email(valid_profile):
    path, expected = valid_profile
    result = load_profile(path)
    assert result["email"] == expected["email"]


def test_load_valid_profile_work_history(valid_profile):
    path, expected = valid_profile
    result = load_profile(path)
    assert len(result["work_history"]) == 1


def test_missing_required_key_raises(tmp_path):
    profile = {
        "name": "Jane", "email": "j@j.com", "phone": "555",
        "location": "NYC",
        # missing work_history, education, skills
    }
    p = tmp_path / "profile.yaml"
    with open(p, "w") as f:
        yaml.dump(profile, f)
    with pytest.raises(ProfileError, match="work_history"):
        load_profile(str(p))


def test_all_required_keys_checked(tmp_path):
    """Each required key, when omitted alone, triggers ProfileError."""
    base = {k: "x" for k in REQUIRED_KEYS}
    base["work_history"] = []
    base["education"] = []
    base["skills"] = []

    for key in REQUIRED_KEYS:
        partial = {k: v for k, v in base.items() if k != key}
        p = tmp_path / f"profile_{key}.yaml"
        with open(p, "w") as f:
            yaml.dump(partial, f)
        with pytest.raises(ProfileError):
            load_profile(str(p))


def test_file_not_found_raises():
    with pytest.raises(ProfileError, match="not found"):
        load_profile("/nonexistent/path/profile.yaml")


def test_invalid_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("name: [\nbroken yaml")
    with pytest.raises(ProfileError, match="Invalid YAML"):
        load_profile(str(p))


def test_non_dict_yaml_raises(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- item1\n- item2\n")
    with pytest.raises(ProfileError, match="mapping"):
        load_profile(str(p))
