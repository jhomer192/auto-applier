import logging
import os

from bot.models import ApplicationResult, FormField, JobInfo
from bot.scraper import extract_fields_from_page
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


def _validate_resume(resume_path: str) -> None:
    if not resume_path or not os.path.isfile(resume_path):
        raise ValueError(f"Resume file not found: {resume_path!r}")


class LeverAdapter:
    name = "lever"
    url_pattern = r"jobs\.lever\.co/.+"

    async def fetch_job_info(self, url: str) -> JobInfo:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle")

            try:
                title = await page.text_content("h2") or await page.title()
                title = title.strip()
            except Exception:
                title = await page.title()

            try:
                company_slug = url.split("jobs.lever.co/")[-1].split("/")[0]
                company = company_slug.replace("-", " ").title()
            except Exception:
                company = ""

            html = await page.content()
            await browser.close()
            return JobInfo(title=title, company=company, url=url, raw_html=html)

    async def extract_fields(self, url: str) -> list[FormField]:
        """Dynamically scrape all fields from the Lever application form."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="networkidle")
                await page.wait_for_timeout(1000)

                # Click Apply button to reveal the form
                for apply_selector in [
                    "a[data-qa='btn-apply-bottom']",
                    "a[data-qa='btn-apply']",
                    "a.template-btn-submit",
                    "a:has-text('Apply')",
                ]:
                    try:
                        btn = page.locator(apply_selector)
                        if await btn.count() > 0:
                            await btn.first.click()
                            await page.wait_for_load_state("networkidle", timeout=10000)
                            await page.wait_for_timeout(1000)
                            break
                    except Exception:
                        pass

                fields = await extract_fields_from_page(page)
                logger.info("Lever: scraped %d fields from %s", len(fields), url)
                return fields
            except Exception as e:
                logger.warning("Lever: dynamic extraction failed (%s), using static fallback", e)
                return _static_fallback()
            finally:
                await browser.close()

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
                await page.goto(url, wait_until="networkidle")
                await page.wait_for_timeout(1000)

                # Click Apply to open form
                for apply_selector in [
                    "a[data-qa='btn-apply-bottom']",
                    "a[data-qa='btn-apply']",
                    "a.template-btn-submit",
                ]:
                    try:
                        btn = page.locator(apply_selector)
                        if await btn.count() > 0:
                            await btn.first.click()
                            await page.wait_for_load_state("networkidle", timeout=10000)
                            await page.wait_for_timeout(500)
                            break
                    except Exception:
                        pass

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
                        elif field.field_type == "checkbox":
                            should_check = field.answer.lower() in ("yes", "true", "1", "checked")
                            is_checked = await page.is_checked(field.selector)
                            if should_check and not is_checked:
                                await page.check(field.selector)
                            submitted[field.label] = field.answer
                    except Exception as fill_err:
                        logger.warning("Lever: could not fill %r (%s): %s", field.label, field.selector, fill_err)

                os.makedirs("data/screenshots", exist_ok=True)
                screenshot_path = f"data/screenshots/lever_{job_slug}_pre.png"
                await page.screenshot(path=screenshot_path)

                await page.click("button[data-qa='btn-submit']", timeout=10000)
                await page.wait_for_load_state("networkidle", timeout=15000)

                screenshot_path = f"data/screenshots/lever_{job_slug}_post.png"
                await page.screenshot(path=screenshot_path)

                return ApplicationResult(
                    success=True,
                    screenshot_path=screenshot_path,
                    submitted_fields=submitted,
                    error=None,
                )

            except Exception as e:
                logger.error("Lever submission failed: %s", e)
                try:
                    err_shot = f"data/screenshots/lever_error_{job_slug}.png"
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


def _static_fallback() -> list[FormField]:
    return [
        FormField(label="Full Name", field_type="text", required=True, selector="input[name='name']"),
        FormField(label="Email", field_type="text", required=True, selector="input[name='email']"),
        FormField(label="Phone", field_type="text", required=False, selector="input[name='phone']"),
        FormField(label="Current Company", field_type="text", required=False, selector="input[name='org']"),
        FormField(label="LinkedIn", field_type="text", required=False, selector="input[name='urls[LinkedIn]']"),
        FormField(label="Resume", field_type="file", required=True, selector="input[data-qa='resume-upload-input']"),
        FormField(label="Cover Letter", field_type="textarea", required=False, selector="textarea[name='comments']"),
    ]
