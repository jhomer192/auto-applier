import logging
import os

from bot.models import ApplicationResult, FormField, JobInfo
from playwright.async_api import async_playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)


def _validate_resume(resume_path: str) -> None:
    if not resume_path or not os.path.isfile(resume_path):
        raise ValueError(f"Resume file not found: {resume_path!r}")


class LinkedInAdapter:
    name = "linkedin"
    url_pattern = r"linkedin\.com/jobs/view/\d+"

    def __init__(self, auth_state_path: str = "data/linkedin_auth.json") -> None:
        self._auth_state = auth_state_path

    async def _get_context(self, playwright) -> tuple[Browser, BrowserContext]:
        browser = await playwright.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=self._auth_state)
        return browser, ctx

    async def fetch_job_info(self, url: str) -> JobInfo:
        from playwright_stealth import stealth_async

        async with async_playwright() as p:
            browser, ctx = await self._get_context(p)
            page = await ctx.new_page()
            try:
                await stealth_async(page)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                try:
                    title = (
                        await page.text_content("h1.job-details-jobs-unified-top-card__job-title") or ""
                    )
                    title = title.strip()
                except Exception:
                    title = await page.title()

                try:
                    company = (
                        await page.text_content(".job-details-jobs-unified-top-card__company-name") or ""
                    )
                    company = company.strip()
                except Exception:
                    company = ""

                html = await page.content()
                return JobInfo(title=title, company=company, url=url, raw_html=html)
            finally:
                await browser.close()

    async def extract_fields(self, url: str) -> list[FormField]:
        """Return standard LinkedIn Easy Apply fields (static — modal is dynamic at submit time)."""
        return [
            FormField(label="Phone", field_type="text", required=False, selector="input[id*='phoneNumber']"),
            FormField(label="Resume", field_type="file", required=True, selector="input[name='file']"),
            FormField(label="Cover Letter", field_type="textarea", required=False, selector="textarea[id*='coverLetter']"),
        ]

    async def submit_application(
        self,
        url: str,
        fields: list[FormField],
        resume_path: str,
    ) -> ApplicationResult:
        """Step through the LinkedIn Easy Apply modal and submit."""
        _validate_resume(resume_path)
        from playwright_stealth import stealth_async

        submitted: dict[str, str] = {}
        screenshot_path: str | None = None
        submitted_flag = False  # track whether Submit button was actually clicked

        async with async_playwright() as p:
            browser, ctx = await self._get_context(p)
            page = await ctx.new_page()

            try:
                await stealth_async(page)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                await page.click("button.jobs-apply-button", timeout=10000)
                await page.wait_for_timeout(1500)

                os.makedirs("data/screenshots", exist_ok=True)
                job_id = url.rstrip("/").split("/")[-1]

                max_steps = 10
                for _step in range(max_steps):
                    # Fill phone if visible
                    try:
                        phone_input = page.locator("input[id*='phoneNumber']")
                        if await phone_input.count() > 0:
                            phone_field = next((f for f in fields if f.label == "Phone"), None)
                            if phone_field and phone_field.answer and "Phone" not in submitted:
                                await phone_input.fill(phone_field.answer)
                                submitted["Phone"] = phone_field.answer
                    except Exception as e:
                        logger.warning("LinkedIn: could not fill Phone: %s", e)

                    # Upload resume if file input is visible
                    try:
                        file_input = page.locator("input[type='file']")
                        if await file_input.count() > 0 and "Resume" not in submitted:
                            await file_input.set_input_files(resume_path)
                            submitted["Resume"] = resume_path
                    except Exception as e:
                        logger.warning("LinkedIn: could not upload resume: %s", e)

                    await page.wait_for_timeout(500)

                    # Check for Submit button
                    submit_btn = page.locator(
                        "button[aria-label*='Submit'], button:has-text('Submit application')"
                    )
                    if await submit_btn.count() > 0:
                        screenshot_path = f"data/screenshots/linkedin_{job_id}_pre.png"
                        await page.screenshot(path=screenshot_path)
                        await submit_btn.click()
                        await page.wait_for_timeout(3000)
                        screenshot_path = f"data/screenshots/linkedin_{job_id}_post.png"
                        await page.screenshot(path=screenshot_path)
                        submitted_flag = True
                        break

                    # Advance to next step
                    next_btn = page.locator(
                        "button[aria-label*='Continue'], button:has-text('Next')"
                    )
                    if await next_btn.count() > 0:
                        await next_btn.click()
                        await page.wait_for_timeout(1000)
                    else:
                        # No Next and no Submit — modal is in an unexpected state
                        break

                if not submitted_flag:
                    return ApplicationResult(
                        success=False,
                        screenshot_path=screenshot_path,
                        submitted_fields=submitted,
                        error="Submit button was never found after stepping through the Easy Apply modal.",
                    )

                return ApplicationResult(
                    success=True,
                    screenshot_path=screenshot_path,
                    submitted_fields=submitted,
                    error=None,
                )

            except Exception as e:
                logger.error("LinkedIn submission failed: %s", e)
                try:
                    err_shot = f"data/screenshots/linkedin_error_{job_id if 'job_id' in dir() else 'unknown'}.png"
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
