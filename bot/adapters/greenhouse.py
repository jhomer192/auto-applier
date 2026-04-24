import logging
import os

from bot.models import ApplicationResult, FormField, JobInfo
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


def _validate_resume(resume_path: str) -> None:
    """Raise ValueError if resume_path does not point to an existing file."""
    if not resume_path or not os.path.isfile(resume_path):
        raise ValueError(f"Resume file not found: {resume_path!r}")


class GreenhouseAdapter:
    name = "greenhouse"
    url_pattern = r"boards\.greenhouse\.io/.+"

    async def fetch_job_info(self, url: str) -> JobInfo:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle")

            title = await page.title()
            try:
                company = await page.text_content(".company-name") or ""
                company = company.strip()
            except Exception:
                company = title.split(" at ")[-1] if " at " in title else ""

            html = await page.content()
            await browser.close()
            return JobInfo(title=title.strip(), company=company.strip(), url=url, raw_html=html)

    async def extract_fields(self, url: str) -> list[FormField]:
        """Return standard Greenhouse application fields (static — no browser needed)."""
        return [
            FormField(label="First Name", field_type="text", required=True, selector="#first_name"),
            FormField(label="Last Name", field_type="text", required=True, selector="#last_name"),
            FormField(label="Email", field_type="text", required=True, selector="#email"),
            FormField(label="Phone", field_type="text", required=True, selector="#phone"),
            FormField(label="Resume", field_type="file", required=True, selector="input[name='resume']"),
            FormField(label="Cover Letter", field_type="textarea", required=False, selector="textarea[name='cover_letter']"),
            FormField(label="LinkedIn Profile", field_type="text", required=False, selector="input[id*='linkedin']"),
            FormField(label="Website", field_type="text", required=False, selector="input[id*='website']"),
        ]

    async def submit_application(
        self,
        url: str,
        fields: list[FormField],
        resume_path: str,
    ) -> ApplicationResult:
        _validate_resume(resume_path)
        submitted: dict[str, str] = {}
        screenshot_path: str | None = None
        job_slug = url.rstrip("/").split("/")[-1]

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            try:
                app_url = url if "/application" in url else url.rstrip("/") + "/application"
                await page.goto(app_url, wait_until="networkidle")

                for field in fields:
                    if not field.answer:
                        continue
                    try:
                        if field.field_type == "file":
                            await page.set_input_files(field.selector, resume_path)
                            submitted[field.label] = resume_path
                        elif field.field_type in ("text", "textarea"):
                            await page.fill(field.selector, field.answer)
                            submitted[field.label] = field.answer
                        elif field.field_type == "select":
                            await page.select_option(field.selector, label=field.answer)
                            submitted[field.label] = field.answer
                    except Exception as fill_err:
                        logger.warning("Greenhouse: could not fill %r (%s): %s", field.label, field.selector, fill_err)

                os.makedirs("data/screenshots", exist_ok=True)
                screenshot_path = f"data/screenshots/greenhouse_{job_slug}_pre.png"
                await page.screenshot(path=screenshot_path)

                await page.click("button[type='submit'], input[type='submit']")
                await page.wait_for_load_state("networkidle", timeout=15000)

                screenshot_path = f"data/screenshots/greenhouse_{job_slug}_post.png"
                await page.screenshot(path=screenshot_path)

                return ApplicationResult(
                    success=True,
                    screenshot_path=screenshot_path,
                    submitted_fields=submitted,
                    error=None,
                )

            except Exception as e:
                logger.error("Greenhouse submission failed: %s", e)
                try:
                    err_shot = f"data/screenshots/greenhouse_error_{job_slug}.png"
                    await page.screenshot(path=err_shot)
                    screenshot_path = err_shot
                except Exception:
                    pass
                return ApplicationResult(
                    success=False,
                    screenshot_path=screenshot_path,
                    submitted_fields=submitted,
                    error=str(e),
                )
            finally:
                await browser.close()
