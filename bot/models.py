from dataclasses import dataclass, field
from datetime import datetime


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
    id: int | None = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


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


@dataclass
class PendingJob:
    url: str
    job_info: JobInfo
    fields: list[FormField]
    awaiting_fields: list[FormField] = field(default_factory=list)
    current_field_index: int = 0
    app_id: int | None = None
