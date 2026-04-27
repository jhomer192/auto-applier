from typing import Protocol, runtime_checkable
from bot.models import JobInfo, FormField, ApplicationResult


class FillFailed(Exception):
    """Raised when a field fill silently no-ops — e.g., a stale selector matched
    a hidden input or no input at all, leaving ``page.input_value(selector)``
    empty after the type/select.

    Audit fix #4. Without this guard the submit button would still fire and the
    bot would happily report success on a blank application form. Catch this in
    the adapter's submit_application() outer handler and surface it as a normal
    submission failure (success=False, error=...).
    """


@runtime_checkable
class SiteAdapter(Protocol):
    name: str
    url_pattern: str

    async def fetch_job_info(self, url: str) -> JobInfo: ...
    async def extract_fields(self, url: str) -> list[FormField]: ...
    async def submit_application(
        self,
        url: str,
        fields: list[FormField],
        resume_path: str,
    ) -> ApplicationResult: ...


async def assert_field_filled(page, field: "FormField") -> None:
    """Read ``page.input_value(selector)`` after a fill and raise FillFailed
    if a required value is missing.

    Skips checkboxes (input_value isn't meaningful) and skips fields whose
    answer was empty (we never tried to fill them in the first place).
    """
    if not field.answer or field.field_type == "checkbox":
        return
    try:
        actual = await page.input_value(field.selector)
    except Exception:
        # Selector didn't match anything on the page — definitively a fill
        # failure for any field we attempted to set.
        raise FillFailed(
            f"selector {field.selector!r} for {field.label!r} matched no input"
        )
    actual = (actual or "").strip()
    if not actual:
        raise FillFailed(
            f"{field.label!r} ({field.selector!r}) is still empty after fill — "
            "selector likely stale; refusing to submit a blank form"
        )
