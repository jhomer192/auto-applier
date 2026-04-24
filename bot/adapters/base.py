from typing import Protocol, runtime_checkable
from bot.models import JobInfo, FormField, ApplicationResult


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
