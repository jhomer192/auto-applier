"""GitHub Pages personal website generator.

Takes the user's profile.yaml and generates a complete, self-contained
index.html — no build tools, no dependencies, copy straight to a GitHub repo
named <username>.github.io and it's live.

Usage:
    html = generate_website(profile, theme="minimal")
    Path("index.html").write_text(html)

Themes:
    "minimal"   — clean white, very professional, suits any field
    "dark"      — dark background, good for engineers/hackers
    "academic"  — serif fonts, research-forward layout
"""
import html as html_lib
import textwrap
from typing import Literal

Theme = Literal["minimal", "dark", "academic"]


# ---------------------------------------------------------------------------
# CSS themes
# ---------------------------------------------------------------------------


_CSS_MINIMAL = """
  :root {
    --bg: #ffffff;
    --surface: #f8f9fa;
    --text: #1a1a2e;
    --muted: #6c757d;
    --accent: #0077b5;
    --accent-light: #e8f4fd;
    --border: #dee2e6;
    --font-body: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    --font-mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
  }
  body { background: var(--bg); color: var(--text); font-family: var(--font-body); }
  a { color: var(--accent); }
  .hero { background: var(--surface); border-bottom: 1px solid var(--border); }
  .tag { background: var(--accent-light); color: var(--accent); }
"""

_CSS_DARK = """
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --accent-light: #1f2d3d;
    --border: #30363d;
    --font-body: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    --font-mono: 'JetBrains Mono', 'Fira Code', monospace;
  }
  body { background: var(--bg); color: var(--text); font-family: var(--font-body); }
  a { color: var(--accent); }
  .hero { background: var(--surface); border-bottom: 1px solid var(--border); }
  .tag { background: var(--accent-light); color: var(--accent); }
"""

_CSS_ACADEMIC = """
  :root {
    --bg: #fefefe;
    --surface: #f5f0e8;
    --text: #2c2c2c;
    --muted: #666;
    --accent: #8b0000;
    --accent-light: #f9e8e8;
    --border: #d4c9b0;
    --font-body: 'Georgia', 'Times New Roman', serif;
    --font-mono: 'Courier New', monospace;
  }
  body { background: var(--bg); color: var(--text); font-family: var(--font-body); }
  a { color: var(--accent); }
  .hero { background: var(--surface); border-bottom: 1px solid var(--border); }
  .tag { background: var(--accent-light); color: var(--accent); }
"""

_CSS_THEMES: dict[str, str] = {
    "minimal": _CSS_MINIMAL,
    "dark": _CSS_DARK,
    "academic": _CSS_ACADEMIC,
}

_CSS_BASE = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { line-height: 1.6; }

  .container { max-width: 820px; margin: 0 auto; padding: 0 1.5rem; }

  /* Hero / header */
  .hero { padding: 3rem 0 2.5rem; }
  .hero h1 { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.3rem; }
  .hero .tagline { font-size: 1.1rem; color: var(--muted); margin-bottom: 1rem; }
  .hero .summary { max-width: 600px; line-height: 1.7; margin-bottom: 1.2rem; }
  .hero .links a {
    display: inline-flex; align-items: center; gap: 0.3rem;
    margin-right: 1rem; text-decoration: none; font-size: 0.95rem;
    color: var(--accent); border-bottom: 1px dashed var(--accent);
    padding-bottom: 1px;
  }
  .hero .links a:hover { opacity: 0.75; }

  /* Navigation */
  nav { background: var(--surface); border-bottom: 1px solid var(--border);
        position: sticky; top: 0; z-index: 100; }
  nav .container { display: flex; gap: 1.5rem; padding-top: 0.6rem; padding-bottom: 0.6rem; }
  nav a { text-decoration: none; color: var(--muted); font-size: 0.9rem; font-weight: 500; }
  nav a:hover { color: var(--accent); }

  /* Sections */
  section { padding: 2.5rem 0; border-bottom: 1px solid var(--border); }
  section:last-child { border-bottom: none; }
  section h2 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem;
               padding-bottom: 0.4rem; border-bottom: 2px solid var(--accent); display: inline-block; }

  /* Cards (projects, jobs) */
  .card { padding: 1.2rem 1.4rem; border: 1px solid var(--border); border-radius: 8px;
          margin-bottom: 1rem; background: var(--surface); }
  .card h3 { font-size: 1.05rem; font-weight: 600; margin-bottom: 0.2rem; }
  .card .meta { font-size: 0.85rem; color: var(--muted); margin-bottom: 0.6rem; }
  .card p { font-size: 0.95rem; line-height: 1.65; }
  .card .outcome { margin-top: 0.5rem; font-size: 0.9rem; color: var(--accent);
                   font-weight: 500; }
  .card a.card-link { font-size: 0.85rem; color: var(--accent); text-decoration: none;
                       display: inline-block; margin-top: 0.5rem; }
  .card a.card-link:hover { text-decoration: underline; }

  /* Tags */
  .tags { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.6rem; }
  .tag { font-size: 0.78rem; font-weight: 500; padding: 0.15rem 0.55rem;
         border-radius: 4px; font-family: var(--font-mono, monospace); }

  /* Skills grid */
  .skills-grid { display: flex; flex-wrap: wrap; gap: 0.5rem; }

  /* Certifications / awards list */
  .item-list { list-style: none; }
  .item-list li { padding: 0.5rem 0; border-bottom: 1px solid var(--border); font-size: 0.95rem; }
  .item-list li:last-child { border-bottom: none; }
  .item-list .item-meta { font-size: 0.82rem; color: var(--muted); }

  /* Academic section */
  .pub-list { list-style: disc; padding-left: 1.5rem; }
  .pub-list li { margin-bottom: 0.4rem; font-size: 0.9rem; line-height: 1.5; }

  /* Footer */
  footer { text-align: center; padding: 1.5rem 0; font-size: 0.8rem; color: var(--muted); }

  @media (max-width: 600px) {
    .hero h1 { font-size: 1.7rem; }
    nav .container { gap: 1rem; overflow-x: auto; }
  }
"""


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _e(text: str) -> str:
    """HTML-escape a string."""
    return html_lib.escape(str(text)) if text else ""


def _link(url: str, label: str, css_class: str = "") -> str:
    if not url:
        return ""
    cls = f' class="{_e(css_class)}"' if css_class else ""
    return f'<a href="{_e(url)}"{cls} target="_blank" rel="noopener">{_e(label)}</a>'


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_hero(profile: dict) -> str:
    name = _e(profile.get("name", ""))
    location = _e(profile.get("location", ""))
    email = _e(profile.get("email", ""))
    summary = _e(profile.get("summary", ""))
    links = profile.get("links", {}) or {}

    # Derive a tagline from skills + location, or first job title
    tagline_parts = []
    if profile.get("skills"):
        tagline_parts.append(" · ".join(profile["skills"][:4]))
    if location:
        tagline_parts.append(location)
    tagline = _e(" | ".join(tagline_parts))

    # Social links
    link_items = []
    if email:
        link_items.append(f'<a href="mailto:{email}">✉ {email}</a>')
    if links.get("linkedin"):
        link_items.append(_link(links["linkedin"], "🔗 LinkedIn"))
    if links.get("github"):
        link_items.append(_link(links["github"], "⚙ GitHub"))
    if links.get("portfolio"):
        link_items.append(_link(links["portfolio"], "🌐 Portfolio"))
    links_html = "\n    ".join(link_items)

    summary_html = f'<p class="summary">{_e(summary)}</p>' if summary else ""

    return f"""
  <div class="hero">
    <div class="container">
      <h1>{name}</h1>
      <p class="tagline">{tagline}</p>
      {summary_html}
      <div class="links">
        {links_html}
      </div>
    </div>
  </div>"""


def _build_nav(profile: dict) -> str:
    sections = ["experience", "projects", "skills"]
    if profile.get("education"):
        sections.append("education")
    if profile.get("certifications") or profile.get("competitions"):
        sections.append("credentials")
    if profile.get("academic"):
        sections.append("research")

    items = "\n    ".join(
        f'<a href="#{s}">{s.capitalize()}</a>' for s in sections
    )
    return f"""
  <nav>
    <div class="container">
      {items}
    </div>
  </nav>"""


def _build_experience(profile: dict) -> str:
    history = profile.get("work_history", [])
    if not history:
        return ""

    cards = []
    for job in history:
        title = _e(job.get("title", ""))
        company = _e(job.get("company", ""))
        start = _e(job.get("start", ""))
        end = _e(job.get("end", "present"))
        desc = _e(job.get("description", ""))
        desc_html = f"<p>{desc}</p>" if desc else ""
        cards.append(f"""    <div class="card">
      <h3>{title}</h3>
      <div class="meta">{company} &nbsp;·&nbsp; {start} – {end}</div>
      {desc_html}
    </div>""")

    body = "\n".join(cards)
    return f"""
  <section id="experience">
    <div class="container">
      <h2>Experience</h2>
{body}
    </div>
  </section>"""


def _build_projects(profile: dict) -> str:
    projects = profile.get("projects", [])
    if not projects:
        return ""

    cards = []
    for p in projects:
        name = _e(p.get("name", ""))
        desc = _e(p.get("description", ""))
        outcome = p.get("outcome", "")
        tech = p.get("tech", [])
        link = p.get("link", "")

        outcome_html = f'<div class="outcome">📈 {_e(outcome)}</div>' if outcome else ""
        tags_html = ""
        if tech:
            tags = " ".join(f'<span class="tag">{_e(t)}</span>' for t in tech)
            tags_html = f'<div class="tags">{tags}</div>'
        link_html = _link(link, "→ View project", "card-link") if link else ""

        cards.append(f"""    <div class="card">
      <h3>{name}</h3>
      <p>{desc}</p>
      {outcome_html}
      {tags_html}
      {link_html}
    </div>""")

    body = "\n".join(cards)
    return f"""
  <section id="projects">
    <div class="container">
      <h2>Projects</h2>
{body}
    </div>
  </section>"""


def _build_skills(profile: dict) -> str:
    skills = profile.get("skills", [])
    if not skills:
        return ""

    tags = " ".join(f'<span class="tag">{_e(s)}</span>' for s in skills)
    return f"""
  <section id="skills">
    <div class="container">
      <h2>Skills</h2>
      <div class="skills-grid">
        {tags}
      </div>
    </div>
  </section>"""


def _build_education(profile: dict) -> str:
    education = profile.get("education", [])
    if not education:
        return ""

    cards = []
    for edu in education:
        degree = _e(edu.get("degree", ""))
        school = _e(edu.get("school", ""))
        year = _e(edu.get("year", ""))
        year_html = f" &nbsp;·&nbsp; {year}" if year else ""
        cards.append(f"""    <div class="card">
      <h3>{degree}</h3>
      <div class="meta">{school}{year_html}</div>
    </div>""")

    body = "\n".join(cards)
    return f"""
  <section id="education">
    <div class="container">
      <h2>Education</h2>
{body}
    </div>
  </section>"""


def _build_credentials(profile: dict) -> str:
    certs = profile.get("certifications", [])
    competitions = profile.get("competitions", [])
    if not certs and not competitions:
        return ""

    blocks = []

    if certs:
        items = []
        for c in certs:
            name = _e(c.get("name", ""))
            issuer = _e(c.get("issuer", ""))
            year = _e(c.get("year", ""))
            score = c.get("score", "")
            meta_parts = [p for p in [issuer, year, score] if p]
            meta = " · ".join(_e(m) for m in meta_parts)
            items.append(f'<li><strong>{name}</strong><span class="item-meta"> — {meta}</span></li>' if meta else f'<li><strong>{name}</strong></li>')
        items_html = "\n    ".join(items)
        blocks.append(f"<h3 style='margin-bottom:0.8rem'>Certifications</h3>\n    <ul class='item-list'>\n    {items_html}\n    </ul>")

    if competitions:
        items = []
        for c in competitions:
            name = _e(c.get("name", ""))
            result = _e(c.get("result", ""))
            year = _e(c.get("year", ""))
            meta = f" ({year})" if year else ""
            items.append(f'<li><strong>{name}</strong><span class="item-meta"> — {result}{meta}</span></li>')
        items_html = "\n    ".join(items)
        blocks.append(f"<h3 style='margin-bottom:0.8rem;margin-top:1.5rem'>Awards &amp; Competitions</h3>\n    <ul class='item-list'>\n    {items_html}\n    </ul>")

    body = "\n".join(blocks)
    return f"""
  <section id="credentials">
    <div class="container">
      <h2>Credentials</h2>
      {body}
    </div>
  </section>"""


def _build_academic(profile: dict) -> str:
    acad = profile.get("academic")
    if not acad:
        return ""

    university = _e(acad.get("university", ""))
    department = _e(acad.get("department", ""))
    degree = _e(acad.get("degree", ""))
    grad_year = _e(acad.get("graduation_year", ""))
    gpa = acad.get("gpa", "")
    thesis = acad.get("thesis", "")
    research_areas = acad.get("research_areas", [])
    publications = acad.get("publications", [])
    ta_ra = acad.get("ta_ra_positions", [])

    meta_parts = [p for p in [department, degree, f"Class of {grad_year}" if grad_year else ""] if p]
    meta = " · ".join(meta_parts)
    gpa_html = f'<p style="margin-top:0.3rem;font-size:0.9rem">GPA: {_e(gpa)}</p>' if gpa else ""
    thesis_html = f'<p style="margin-top:0.8rem"><strong>Thesis:</strong> {_e(thesis)}</p>' if thesis else ""

    areas_html = ""
    if research_areas:
        tags = " ".join(f'<span class="tag">{_e(a)}</span>' for a in research_areas)
        areas_html = f'<div style="margin-top:0.8rem"><strong>Research areas:</strong><div class="tags" style="margin-top:0.4rem">{tags}</div></div>'

    pubs_html = ""
    if publications:
        items = "\n".join(f"<li>{_e(p)}</li>" for p in publications)
        pubs_html = f'<div style="margin-top:1rem"><strong>Selected publications:</strong><ul class="pub-list" style="margin-top:0.4rem">{items}</ul></div>'

    ta_html = ""
    if ta_ra:
        items = "\n".join(f"<li>{_e(pos)}</li>" for pos in ta_ra)
        ta_html = f'<div style="margin-top:1rem"><strong>TA / RA positions:</strong><ul class="pub-list" style="margin-top:0.4rem">{items}</ul></div>'

    return f"""
  <section id="research">
    <div class="container">
      <h2>Research &amp; Academic</h2>
      <div class="card">
        <h3>{university}</h3>
        <div class="meta">{meta}</div>
        {gpa_html}
        {thesis_html}
        {areas_html}
        {pubs_html}
        {ta_html}
      </div>
    </div>
  </section>"""


def _build_footer(profile: dict) -> str:
    name = _e(profile.get("name", ""))
    return f"""
  <footer>
    <div class="container">
      {name} · Built with <a href="https://github.com/jhomer192/auto-applier" target="_blank" rel="noopener">auto-applier</a>
    </div>
  </footer>"""


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


def generate_website(profile: dict, theme: Theme = "minimal") -> str:
    """Generate a complete self-contained index.html for GitHub Pages.

    Args:
        profile: The user's profile.yaml dict.
        theme: Visual theme — "minimal", "dark", or "academic".

    Returns:
        Complete HTML string ready to write as index.html.
    """
    if theme not in _CSS_THEMES:
        theme = "minimal"

    name = profile.get("name", "My Portfolio")
    theme_css = _CSS_THEMES[theme]

    hero = _build_hero(profile)
    nav = _build_nav(profile)
    experience = _build_experience(profile)
    projects = _build_projects(profile)
    skills = _build_skills(profile)
    education = _build_education(profile)
    credentials = _build_credentials(profile)
    academic = _build_academic(profile)
    footer = _build_footer(profile)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_e(name)}</title>
  <style>
{theme_css}
{_CSS_BASE}
  </style>
</head>
<body>
{hero}
{nav}
{experience}
{projects}
{skills}
{education}
{credentials}
{academic}
{footer}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Deployment guide
# ---------------------------------------------------------------------------


def deployment_guide(profile: dict) -> str:
    """Return a step-by-step guide for deploying to GitHub Pages.

    Args:
        profile: The user's profile.yaml dict.

    Returns:
        Plain text deployment instructions.
    """
    name = profile.get("name", "Your Name")
    github_url = (profile.get("links") or {}).get("github", "")
    # Try to extract username from GitHub URL
    username = ""
    if github_url:
        import re
        m = re.search(r"github\.com/([^/]+)", github_url)
        if m:
            username = m.group(1)
    repo_name = f"{username}.github.io" if username else "<username>.github.io"
    profile_url = f"https://{repo_name}" if username else f"https://<username>.github.io"

    return textwrap.dedent(f"""
    🚀 Deploy your site to GitHub Pages — free, custom domain ready.

    Step 1 — Create the repo
    ─────────────────────────
    Go to github.com/new and create a repository named:
      {repo_name}
    (Must be exactly <your GitHub username>.github.io)

    Step 2 — Upload index.html
    ──────────────────────────
    Option A (browser): On the new repo page → Add file → Upload files
    Option B (git):
      git init
      git add index.html
      git commit -m "Add portfolio site"
      git branch -M main
      git remote add origin https://github.com/{username or '<username>'}/{repo_name}.git
      git push -u origin main

    Step 3 — Enable GitHub Pages
    ─────────────────────────────
    In your repo: Settings → Pages → Source: main branch → / (root) → Save

    Step 4 — Wait ~60 seconds, then visit:
    ────────────────────────────────────────
      {profile_url}

    Optional: Custom domain
    ───────────────────────
    Settings → Pages → Custom domain → enter your domain
    Then add a CNAME DNS record pointing to {username or '<username>'}.github.io

    Tips:
    • Add your site URL to your LinkedIn "Contact info" and GitHub profile
    • Pin this repo on your GitHub profile page
    • Update profile.yaml and re-run /website generate whenever you add projects
    """).strip()
