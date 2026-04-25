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


class LinkedInAdapter:
    name = "linkedin"
    url_pattern = r"linkedin\.com/jobs/view/\d+"

    def __init__(self, auth_state_path: str = "data/linkedin_auth.json") -> None:
        self._auth_state = auth_state_path

    async def fetch_job_info(self, url: str) -> JobInfo:
        async with async_playwright() as p:
            browser, ctx = await launch_stealth_context(p, self._auth_state)
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
                await page_load_pause()

                # Scroll a bit to look like we're reading the job description
                await human_scroll(page)
                await read_pause(400)

                try:
                    title = (
                        await page.text_content("h1.job-details-jobs-unified-top-card__job-title") or ""
                    ).strip()
                except Exception:
                    title = await page.title()

                try:
                    company = (
                        await page.text_content(".job-details-jobs-unified-top-card__company-name") or ""
                    ).strip()
                except Exception:
                    company = ""

                html = await page.content()
                return JobInfo(title=title, company=company, url=url, raw_html=html)
            finally:
                await browser.close()

    async def extract_fields(self, url: str) -> list[FormField]:
        """Open the Easy Apply modal and scrape fields from each step.

        LinkedIn Easy Apply is multi-step — we click through all steps, scraping
        fields from each, then close without submitting. Returns the complete
        ordered field list across all steps.
        """
        async with async_playwright() as p:
            browser, ctx = await launch_stealth_context(p, self._auth_state)
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
                await page_load_pause()
                await human_scroll(page)
                await jitter_pause(800)

                # Open Easy Apply modal
                apply_btn = page.locator("button.jobs-apply-button")
                if await apply_btn.count() == 0:
                    logger.warning("LinkedIn: Easy Apply button not found, using static fallback")
                    return _static_fallback()

                await human_click(page, "button.jobs-apply-button")
                await jitter_pause(1500)

                all_fields: list[FormField] = []
                seen_labels: set[str] = set()
                max_steps = 15

                for step in range(max_steps):
                    step_fields = await extract_fields_from_page(page)
                    for f in step_fields:
                        key = f"{f.label}|{f.field_type}"
                        if key not in seen_labels:
                            seen_labels.add(key)
                            all_fields.append(f)

                    # Check for Submit button — end of flow
                    submit_btn = page.locator(
                        "button[aria-label*='Submit'], button:has-text('Submit application')"
                    )
                    if await submit_btn.count() > 0:
                        logger.info("LinkedIn: reached Submit on step %d, total fields: %d", step + 1, len(all_fields))
                        break

                    # Advance to next step
                    next_btn = page.locator(
                        "button[aria-label*='Continue'], button:has-text('Next')"
                    )
                    if await next_btn.count() > 0:
                        await human_click(page, "button[aria-label*='Continue'], button:has-text('Next')")
                        await jitter_pause(1200)
                    else:
                        logger.warning("LinkedIn: no Next or Submit found at step %d", step + 1)
                        break

                # Dismiss modal
                try:
                    dismiss = page.locator("button[aria-label='Dismiss']")
                    if await dismiss.count() > 0:
                        await dismiss.first.click()
                except Exception:
                    pass

                logger.info("LinkedIn: scraped %d unique fields across all steps", len(all_fields))
                return all_fields if all_fields else _static_fallback()

            except Exception as e:
                logger.warning("LinkedIn: field extraction failed (%s), using static fallback", e)
                return _static_fallback()
            finally:
                await browser.close()

    async def submit_application(
        self,
        url: str,
        fields: list[FormField],
        resume_path: str,
    ) -> ApplicationResult:
        """Step through the Easy Apply modal, filling fields at each step."""
        _validate_resume(resume_path)

        submitted: dict[str, str] = {}
        screenshot_path: str | None = None
        submitted_flag = False
        job_id = url.rstrip("/").split("/")[-1]

        async with async_playwright() as p:
            browser, ctx = await launch_stealth_context(p, self._auth_state)
            page = await ctx.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded")
                await page_load_pause()
                await human_scroll(page)
                await jitter_pause(800)

                await human_click(page, "button.jobs-apply-button")
                await jitter_pause(1500)

                os.makedirs("data/screenshots", exist_ok=True)
                field_by_label = {f.label.lower(): f for f in fields}
                max_steps = 15

                for _step in range(max_steps):
                    step_fields = await extract_fields_from_page(page)
                    for visible_field in step_fields:
                        match = field_by_label.get(visible_field.label.lower())
                        answer = match.answer if match else ""
                        if not answer:
                            continue
                        try:
                            if visible_field.field_type == "file":
                                file_input = page.locator(visible_field.selector)
                                if await file_input.count() > 0 and visible_field.label not in submitted:
                                    await file_input.set_input_files(resume_path)
                                    submitted[visible_field.label] = resume_path
                                    await field_transition_pause()
                            elif visible_field.field_type in ("text", "textarea"):
                                await human_type(page, visible_field.selector, answer)
                                submitted[visible_field.label] = answer
                                await field_transition_pause()
                            elif visible_field.field_type == "select":
                                await page.select_option(visible_field.selector, label=answer)
                                submitted[visible_field.label] = answer
                                await field_transition_pause()
                            elif visible_field.field_type == "checkbox":
                                should_check = answer.lower() in ("yes", "true", "1")
                                is_checked = await page.is_checked(visible_field.selector)
                                if should_check and not is_checked:
                                    await page.check(visible_field.selector)
                                submitted[visible_field.label] = answer
                                await field_transition_pause()
                        except Exception as fill_err:
                            logger.warning("LinkedIn: could not fill %r: %s", visible_field.label, fill_err)

                    await jitter_pause(500)

                    # Check for Submit button
                    submit_btn = page.locator(
                        "button[aria-label*='Submit'], button:has-text('Submit application')"
                    )
                    if await submit_btn.count() > 0:
                        screenshot_path = f"data/screenshots/linkedin_{job_id}_pre.png"
                        await page.screenshot(path=screenshot_path)
                        await human_click(page, "button[aria-label*='Submit'], button:has-text('Submit application')")
                        await jitter_pause(3000)
                        screenshot_path = f"data/screenshots/linkedin_{job_id}_post.png"
                        await page.screenshot(path=screenshot_path)
                        submitted_flag = True
                        break

                    # Advance
                    next_btn = page.locator(
                        "button[aria-label*='Continue'], button:has-text('Next')"
                    )
                    if await next_btn.count() > 0:
                        await human_click(page, "button[aria-label*='Continue'], button:has-text('Next')")
                        await jitter_pause(1200)
                    else:
                        break

                if not submitted_flag:
                    return ApplicationResult(
                        success=False,
                        screenshot_path=screenshot_path,
                        submitted_fields=submitted,
                        error="Submit button was never reached after stepping through the Easy Apply modal.",
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
                    err_shot = f"data/screenshots/linkedin_error_{job_id}.png"
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
        FormField(label="Phone", field_type="text", required=False, selector="input[id*='phoneNumber']"),
        FormField(label="Resume", field_type="file", required=True, selector="input[name='file']"),
        FormField(label="Cover Letter", field_type="textarea", required=False, selector="textarea[id*='coverLetter']"),
    ]
