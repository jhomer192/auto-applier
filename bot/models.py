from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ApplicationRecord:
    url: str
    title: str
    company: str
    site: str
    status: str  # "applied" | "skipped" | "failed" | "needs_info"
    submitted_fields: str = "{}"  # JSON blob
    screenshot_path: str | None = None
    applied_at: str | None = None
    notes: str = ""
    cover_letter: str = ""
    tailored_resume: str = ""
    id: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class JobInfo:
    title: str
    company: str
    url: str
    raw_html: str


@dataclass
class FormField:
    label: str
    field_type: str   # "text" | "textarea" | "select" | "checkbox" | "file"
    required: bool
    selector: str
    options: list[str] = field(default_factory=list)
    answer: str = ""


@dataclass
class ApplicationResult:
    success: bool
    screenshot_path: str | None
    submitted_fields: dict[str, str]
    error: str | None
    # Verification metadata
    submission_confirmed: bool = False   # True only when page gave a positive confirmation signal
    missing_fields: list = field(default_factory=list)  # required fields we had answers for but couldn't fill
    closed: bool = False                 # True if job was detected as no longer accepting applications
    already_applied: bool = False        # True if job was detected as already applied to


@dataclass
class JobPreferences:
    """User's job search preferences. Loaded from profile.yaml `job_preferences:` block."""
    desired_roles: list[str] = field(default_factory=list)
    min_salary: int = 0            # annual USD (0 = no floor set)
    target_salary: int = 0         # annual USD (0 = not set)
    seniority: list[str] = field(default_factory=list)
    work_arrangement: list[str] = field(default_factory=list)  # "remote"|"hybrid"|"onsite"
    excluded_companies: list[str] = field(default_factory=list)
    auto_apply_threshold: int = 0  # match_score >= this + all fit checks pass → apply without Y/N (0 = disabled)
    min_apply_gap_minutes: int = 4  # minimum minutes between application submissions
    max_apply_gap_minutes: int = 8  # upper bound for randomised gap
    max_applies_per_day: int = 30   # daily application cap (0 = no cap)
    requires_sponsorship: bool = False  # True if candidate needs H-1B or similar visa sponsorship


@dataclass
class JobAnalysis:
    title: str
    company: str
    match_score: int  # 0-100
    tailored_summary: str
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    key_responsibilities: list[str] = field(default_factory=list)
    company_tone: str = "formal"  # "formal" | "casual" | "mission-driven" | "technical"
    ats_keywords: list[str] = field(default_factory=list)
    why_this_role: str = ""
    # Salary intelligence
    salary_min: int = 0            # annual USD from posting (0 if not stated)
    salary_max: int = 0            # annual USD from posting (0 if not stated)
    salary_currency: str = "USD"
    salary_is_estimated: bool = False  # True when LLM estimated (not from posting)
    # Role classification
    seniority_level: str = "unknown"  # "junior"|"mid"|"senior"|"staff"|"principal"|"director"|"unknown"
    work_arrangement: str = "unknown" # "remote"|"hybrid"|"onsite"|"unknown"
    role_type: str = ""               # normalised role category e.g. "software engineer"
    # Visa sponsorship intelligence from posting
    sponsors_visa: bool | None = None  # True=explicitly sponsors; False=explicitly no sponsorship; None=not mentioned


@dataclass
class FitReport:
    """Result of evaluating a job against the user's preferences."""
    salary_ok: bool = True
    salary_note: str = ""
    role_ok: bool = True
    role_note: str = ""
    seniority_ok: bool = True
    seniority_note: str = ""
    arrangement_ok: bool = True
    arrangement_note: str = ""
    excluded_company: bool = False
    # hard_pass: auto-skip without asking (excluded company, or salary drastically below floor)
    hard_pass: bool = False
    hard_pass_reason: str = ""
    # auto_apply: skip Y/N and go straight to submit
    auto_apply: bool = False
    # Visa sponsorship
    sponsorship_ok: bool = True
    sponsorship_note: str = ""


@dataclass
class PendingJob:
    url: str
    job_info: JobInfo
    fields: list[FormField]
    awaiting_fields: list[FormField] = field(default_factory=list)
    current_field_index: int = 0
    app_id: int | None = None
    cover_letter: str = ""
    tailored_resume: str = ""
    fit_report: "FitReport | None" = None


@dataclass
class SavedSearch:
    query: str
    location: str = ""            # e.g. "San Francisco, CA" or ""
    site: str = "linkedin"        # "linkedin" | "any"
    active: bool = True
    id: int | None = None
    last_checked: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class SearchResult:
    title: str
    company: str
    url: str
    search_id: int


@dataclass
class EmailThread:
    message_id: str       # RFC 2822 Message-ID header value
    thread_id: str        # In-Reply-To / References chain root (or message_id if root)
    from_address: str
    subject: str
    body_preview: str     # First 500 chars of plain-text body
    direction: str        # "inbound" | "outbound"
    app_id: int | None = None
    id: int | None = None
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
