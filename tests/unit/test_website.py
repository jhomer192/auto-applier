"""Tests for bot/website.py — HTML generation and deployment guide."""
import pytest
from bot.website import generate_website, deployment_guide, _e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_profile() -> dict:
    return {
        "name": "Jane Smith",
        "email": "jane@example.com",
        "location": "San Francisco, CA",
        "summary": "I build things.",
        "skills": ["Python", "SQL", "PyTorch"],
        "work_history": [
            {
                "title": "Software Engineer",
                "company": "Acme Corp",
                "start": "2022-06",
                "end": "present",
                "description": "Built distributed systems.",
            }
        ],
        "education": [
            {"degree": "B.S. Computer Science", "school": "MIT", "year": "2022"}
        ],
        "links": {
            "linkedin": "https://linkedin.com/in/janesmith",
            "github": "https://github.com/janesmith",
            "portfolio": "",
        },
    }


def _full_profile() -> dict:
    p = _minimal_profile()
    p["projects"] = [
        {
            "name": "ToxiCat",
            "description": "Toxic comment classifier with 97% accuracy.",
            "tech": ["Python", "scikit-learn", "FastAPI"],
            "outcome": "97% accuracy on test set",
            "link": "https://github.com/janesmith/toxicat",
        }
    ]
    p["certifications"] = [
        {"name": "AWS Solutions Architect", "issuer": "Amazon", "year": "2023", "score": ""},
    ]
    p["competitions"] = [
        {"name": "Kaggle Fraud Detection", "result": "Top 3%", "year": "2023"},
    ]
    p["academic"] = {
        "university": "MIT",
        "department": "EECS",
        "degree": "MS",
        "graduation_year": "2024",
        "research_areas": ["NLP", "ML Safety"],
        "thesis": "Towards Safer Language Models",
        "publications": ["Smith et al. ACL 2024"],
        "gpa": "3.9",
        "ta_ra_positions": ["TA: 6.864 Natural Language Processing"],
    }
    return p


# ---------------------------------------------------------------------------
# HTML structure
# ---------------------------------------------------------------------------


def test_generates_valid_html():
    html = generate_website(_minimal_profile())
    assert html.strip().startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_contains_name():
    html = generate_website(_minimal_profile())
    assert "Jane Smith" in html


def test_contains_email():
    html = generate_website(_minimal_profile())
    assert "jane@example.com" in html


def test_contains_summary():
    html = generate_website(_minimal_profile())
    assert "I build things." in html


def test_contains_skills():
    html = generate_website(_minimal_profile())
    assert "Python" in html
    assert "PyTorch" in html


def test_contains_work_history():
    html = generate_website(_minimal_profile())
    assert "Acme Corp" in html
    assert "Software Engineer" in html


def test_contains_education():
    html = generate_website(_minimal_profile())
    assert "MIT" in html
    assert "B.S. Computer Science" in html


def test_contains_linkedin_link():
    html = generate_website(_minimal_profile())
    assert "linkedin.com/in/janesmith" in html


def test_contains_github_link():
    html = generate_website(_minimal_profile())
    assert "github.com/janesmith" in html


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------


def test_minimal_theme_no_dark_bg():
    html = generate_website(_minimal_profile(), theme="minimal")
    assert "--bg: #ffffff" in html


def test_dark_theme_dark_bg():
    html = generate_website(_minimal_profile(), theme="dark")
    assert "--bg: #0d1117" in html


def test_academic_theme_serif_font():
    html = generate_website(_minimal_profile(), theme="academic")
    assert "Georgia" in html


def test_unknown_theme_falls_back_to_minimal():
    html = generate_website(_minimal_profile(), theme="neon-pink")  # type: ignore
    assert "--bg: #ffffff" in html


# ---------------------------------------------------------------------------
# Projects section
# ---------------------------------------------------------------------------


def test_projects_section_present_when_projects_exist():
    html = generate_website(_full_profile())
    assert "ToxiCat" in html


def test_projects_section_absent_when_no_projects():
    p = _minimal_profile()
    p.pop("projects", None)
    html = generate_website(p)
    assert "id=\"projects\"" not in html


def test_projects_outcome_rendered():
    html = generate_website(_full_profile())
    assert "97% accuracy" in html


def test_projects_tech_tags_rendered():
    html = generate_website(_full_profile())
    assert "scikit-learn" in html


def test_projects_link_rendered():
    html = generate_website(_full_profile())
    assert "github.com/janesmith/toxicat" in html


# ---------------------------------------------------------------------------
# Credentials section
# ---------------------------------------------------------------------------


def test_certifications_rendered():
    html = generate_website(_full_profile())
    assert "AWS Solutions Architect" in html
    assert "Amazon" in html


def test_competitions_rendered():
    html = generate_website(_full_profile())
    assert "Kaggle Fraud Detection" in html
    assert "Top 3%" in html


def test_credentials_absent_when_none():
    p = _minimal_profile()
    html = generate_website(p)
    assert "id=\"credentials\"" not in html


# ---------------------------------------------------------------------------
# Academic section
# ---------------------------------------------------------------------------


def test_academic_section_rendered():
    html = generate_website(_full_profile())
    assert "Towards Safer Language Models" in html  # thesis
    assert "NLP" in html
    assert "Smith et al. ACL 2024" in html


def test_academic_gpa_rendered():
    html = generate_website(_full_profile())
    assert "3.9" in html


def test_academic_absent_when_none():
    p = _minimal_profile()
    html = generate_website(p)
    assert "id=\"research\"" not in html


# ---------------------------------------------------------------------------
# XSS safety (_e escaping)
# ---------------------------------------------------------------------------


def test_html_escape_ampersand():
    assert _e("A & B") == "A &amp; B"


def test_html_escape_lt_gt():
    assert _e("<script>") == "&lt;script&gt;"


def test_html_escape_quotes():
    assert _e('"quoted"') == "&quot;quoted&quot;"


def test_xss_in_name_escaped():
    p = _minimal_profile()
    p["name"] = '<script>alert("xss")</script>'
    html = generate_website(p)
    assert '<script>alert' not in html
    assert "&lt;script&gt;" in html


def test_xss_in_summary_escaped():
    p = _minimal_profile()
    p["summary"] = 'Hello <img onerror="evil()">'
    html = generate_website(p)
    assert '<img onerror' not in html


# ---------------------------------------------------------------------------
# deployment_guide
# ---------------------------------------------------------------------------


def test_deployment_guide_contains_github_pages():
    guide = deployment_guide(_minimal_profile())
    assert "GitHub Pages" in guide


def test_deployment_guide_infers_username_from_url():
    guide = deployment_guide(_full_profile())
    assert "janesmith.github.io" in guide


def test_deployment_guide_contains_steps():
    guide = deployment_guide(_minimal_profile())
    assert "Step 1" in guide
    assert "Step 2" in guide
    assert "Step 3" in guide


def test_deployment_guide_contains_custom_domain_tip():
    guide = deployment_guide(_minimal_profile())
    assert "domain" in guide.lower()


def test_deployment_guide_handles_missing_github_url():
    p = _minimal_profile()
    p["links"]["github"] = ""
    guide = deployment_guide(p)
    assert "github.io" in guide  # generic placeholder still present
