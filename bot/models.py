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
