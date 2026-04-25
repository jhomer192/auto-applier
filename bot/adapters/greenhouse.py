import logging
import os

from bot.human import (
    after_click_pause,
    field_transition_pause,
    human_click,
    human_scroll,
    human_type,
    jitter_pause,
    launch_stealth_context,
    page_load_pause,
    read_pause,
)
from bot.models import ApplicationResult, FormField, JobInfo
from bot.scraper import extract_fields_from_page
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


def _validate_resume(resume_path: str) -> None:
    if not resume_path or not os.path.isfile(resume_path):
        raise ValueError(f"Resume file not found: {resume_path!r}")


class GreenhouseAdapter:
    name = "greenhouse"
    url_pattern = r"boards\.greenhouse\.io/.+"

    async def fetch_job_info(self, url: str) -> JobInfo:
        async with async_playwright() as p:
            browser, ctx = await launch_stealth_context(p)
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle")
                await page_load_pause()
                await human_scroll(page)
                await read_pause(350)

                title = await page.title()
                try:
                    company = (await page.text_content(".company-name") or "").strip()
                except Exception:
                    company = title.split(" at ")[-1] if " at " in title else ""

                html = await page.content()
                return JobInfo(title=title.strip(), company=company.strip(), url=url, raw_html=html)
            finally:
                await browser.close()

    async def extract_fields(self, url: str) -> list[FormField]:
        """Dynamically scrape all fields from the Greenhouse application form."""
        async with async_playwright() as p:
            browser, ctx = await launch_stealth_context(p)
            page = await ctx.new_page()
            try:
                app_url = url if "/application" in url else url.rstrip("/") + "/application"
                await page.goto(app_url, wait_until="networkidle")
                await page_load_pause()
                await human_scroll(page)
                await jitter_pause(800)

                fields = await extract_fields_from_page(page)
                logger.info("Greenhouse: scraped %d fields from %s", len(fields), app_url)
                return fields
            except Exception as e:
                logger.warning("Greenhouse: dynamic extraction failed (%s), using static fallback", e)
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
            browser, ctx = await launch_stealth_context(p)
            page = await ctx.new_page()

            try:
                app_url = url if "/application" in url else url.rstrip("/") + "/application"
                await page.goto(app_url, wait_until="networkidle")
                await page_load_pause()
                await human_scroll(page)
                await jitter_pause(600)

                for field in fields:
                    if not field.answer:
                        continue
                    try:
                        if field.field_type == "file":
                            await page.set_input_files(field.selector, resume_path)
                            submitted[field.label] = resume_path
                            await field_transition_pause()
                        elif field.field_type in ("text", "textarea"):
                            await human_type(page, field.selector, field.answer)
                            submitted[field.label] = field.answer
                            await field_transition_pause()
                        elif field.field_type == "select":
                            await page.select_option(field.selector, label=field.answer)
                            submitted[field.label] = field.answer
                            await field_transition_pause()
                        elif field.field_type == "checkbox":
                            should_check = field.answer.lower() in ("yes", "true", "1", "checked")
                            is_checked = await page.is_checked(field.selector)
                            if should_check and not is_checked:
                                await page.check(field.selector)
                            elif not should_check and is_checked:
                                await page.uncheck(field.selector)
                            submitted[field.label] = field.answer
                            await field_transition_pause()
                    except Exception as fill_err:
                        logger.warning("Greenhouse: could not fill %r (%s): %s", field.label, field.selector, fill_err)

                # Scroll down to the submit button so it's in viewport
                await human_scroll(page, pixels=300)
                await jitter_pause(500)

                os.makedirs("data/screenshots", exist_ok=True)
                screenshot_path = f"data/screenshots/greenhouse_{job_slug}_pre.png"
                await page.screenshot(path=screenshot_path)

                await human_click(page, "button[type='submit'], input[type='submit']")
                await page.wait_for_load_state("networkidle", timeout=15000)
                await jitter_pause(800)

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


def _static_fallback() -> list[FormField]:
    return [
        FormField(label="First Name", field_type="text", required=True, selector="#first_name"),
        FormField(label="Last Name", field_type="text", required=True, selector="#last_name"),
        FormField(label="Email", field_type="text", required=True, selector="#email"),
        FormField(label="Phone", field_type="text", required=True, selector="#phone"),
        FormField(label="Resume", field_type="file", required=True, selector="input[name='resume']"),
        FormField(label="Cover Letter", field_type="textarea", required=False, selector="textarea[name='cover_letter']"),
        FormField(label="LinkedIn Profile", field_type="text", required=False, selector="input[id*='linkedin']"),
    ]
