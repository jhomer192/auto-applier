"""
Dynamic form field scraper using Playwright.

Navigates to a page, finds every visible form field, and returns a list
of FormField objects with correct selectors, types, labels, and options.
"""
import logging
import re
from playwright.async_api import Page
from bot.models import FormField

logger = logging.getLogger(__name__)

# Fields to always skip — browser autofill noise, honeypots, hidden tracking
_SKIP_LABELS = {
    "", "search", "q", "query", "_token", "csrf", "utf8",
}
_SKIP_TYPES = {"hidden", "submit", "button", "reset", "image", "search"}


async def extract_fields_from_page(page: Page) -> list[FormField]:
    """Scrape all visible form fields from the current page state.

    Returns FormField objects ordered by document position.
    Each field has:
    - label: human-readable text from <label>, aria-label, or placeholder
    - field_type: "text" | "textarea" | "select" | "checkbox" | "file"
    - required: whether the field is marked required
    - selector: a CSS selector that uniquely identifies the element
    - options: list of option text values (select fields only)
    """
    fields: list[FormField] = []

    raw = await page.evaluate("""() => {
        const results = [];

        function getLabel(el) {
            // 1. aria-label
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
            // 2. aria-labelledby
            const labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
                const lbEl = document.getElementById(labelledBy);
                if (lbEl) return lbEl.textContent.trim();
            }
            // 3. <label for="id">
            if (el.id) {
                const labelEl = document.querySelector(`label[for="${el.id}"]`);
                if (labelEl) return labelEl.textContent.trim();
            }
            // 4. closest wrapping <label>
            const parentLabel = el.closest('label');
            if (parentLabel) {
                const clone = parentLabel.cloneNode(true);
                clone.querySelectorAll('input, select, textarea').forEach(n => n.remove());
                const text = clone.textContent.trim();
                if (text) return text;
            }
            // 5. previous sibling text
            let prev = el.previousElementSibling;
            while (prev) {
                const text = prev.textContent.trim();
                if (text && text.length < 120) return text;
                prev = prev.previousElementSibling;
            }
            // 6. placeholder
            if (el.placeholder) return el.placeholder.trim();
            // 7. name attribute as fallback
            return el.name || el.id || '';
        }

        function uniqueSelector(el) {
            if (el.id) return `#${CSS.escape(el.id)}`;
            if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
            // Build path from root
            const path = [];
            let cur = el;
            while (cur && cur !== document.body) {
                let seg = cur.tagName.toLowerCase();
                const siblings = Array.from(cur.parentElement?.children || []).filter(s => s.tagName === cur.tagName);
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(cur) + 1;
                    seg += `:nth-of-type(${idx})`;
                }
                path.unshift(seg);
                cur = cur.parentElement;
            }
            return path.join(' > ');
        }

        function isVisible(el) {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        }

        // Inputs
        document.querySelectorAll('input').forEach(el => {
            const type = (el.type || 'text').toLowerCase();
            if (['hidden','submit','button','reset','image','search'].includes(type)) return;
            if (!isVisible(el)) return;
            const label = getLabel(el);
            results.push({
                label,
                field_type: type === 'file' ? 'file' : (type === 'checkbox' ? 'checkbox' : (type === 'radio' ? 'radio' : 'text')),
                required: el.required || el.getAttribute('aria-required') === 'true',
                selector: uniqueSelector(el),
                options: [],
                name: el.name || '',
            });
        });

        // Textareas
        document.querySelectorAll('textarea').forEach(el => {
            if (!isVisible(el)) return;
            results.push({
                label: getLabel(el),
                field_type: 'textarea',
                required: el.required || el.getAttribute('aria-required') === 'true',
                selector: uniqueSelector(el),
                options: [],
                name: el.name || '',
            });
        });

        // Selects
        document.querySelectorAll('select').forEach(el => {
            if (!isVisible(el)) return;
            const options = Array.from(el.options)
                .filter(o => o.value && o.value !== '')
                .map(o => o.text.trim());
            results.push({
                label: getLabel(el),
                field_type: 'select',
                required: el.required || el.getAttribute('aria-required') === 'true',
                selector: uniqueSelector(el),
                options,
                name: el.name || '',
            });
        });

        return results;
    }""")

    seen_selectors: set[str] = set()
    for item in raw:
        label: str = item.get("label", "").strip()
        field_type: str = item.get("field_type", "text")
        selector: str = item.get("selector", "")

        # Deduplicate by selector
        if selector in seen_selectors:
            continue
        seen_selectors.add(selector)

        # Skip noise
        label_lower = label.lower().replace(" ", "").replace("*", "").replace("(required)", "")
        if label_lower in _SKIP_LABELS:
            continue

        options: list[str] = item.get("options", [])

        fields.append(FormField(
            label=label or item.get("name", "unknown"),
            field_type=field_type,
            required=bool(item.get("required", False)),
            selector=selector,
            options=options,
        ))

    logger.debug("Scraped %d fields from page", len(fields))
    return fields


def is_eeo_field(label: str) -> bool:
    """Return True if this looks like an EEO/demographic voluntary disclosure field."""
    label_lower = label.lower()
    eeo_terms = [
        "gender", "race", "ethnicity", "veteran", "disability",
        "hispanic", "latino", "demographic", "eeo", "equal opportunity",
    ]
    return any(t in label_lower for t in eeo_terms)


def field_answer_hint(field: FormField) -> str | None:
    """Return a hint for the LLM about how to answer this specific field type.

    Used to augment the LLM prompt for tricky fields.
    """
    label_lower = field.label.lower()

    if field.field_type == "select" and field.options:
        opts = ", ".join(f'"{o}"' for o in field.options[:20])
        return f"This is a dropdown. Valid options: {opts}. Reply with one of these exact values."

    if field.field_type == "checkbox":
        return "This is a checkbox. Reply with 'yes' to check it or 'no' to leave it unchecked."

    if is_eeo_field(field.label):
        return "This is a voluntary EEO/demographic field. Reply with 'Decline to self-identify' unless profile specifies otherwise."

    if re.search(r"salary|compensation|pay|rate", label_lower):
        return "If not in the profile, reply NEEDS_USER_INPUT."

    if re.search(r"authorized|eligible|legally|visa|sponsorship", label_lower):
        return "Answer based on the profile. If not present, reply NEEDS_USER_INPUT."

    return None
