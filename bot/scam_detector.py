"""Pure-Python scam detection — no I/O, no bot dependencies."""
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Precompile at import time
_GENERIC_EMAIL_RE = re.compile(r'\b[\w.+-]+@(gmail|yahoo|hotmail|outlook)\.com\b', re.IGNORECASE)

_TTGBT_PHRASES = [
    "work from home",
    "no experience needed",
    "earn $",
    "unlimited earning",
    "be your own boss",
    "passive income",
    "make money fast",
    "guaranteed income",
    "from home earn",
    "work at home",
    "residual income",
]

_SUSPICIOUS_TLDS = {'.xyz', '.tk', '.ml', '.ga', '.cf', '.top', '.buzz'}

_URL_SHORTENERS = {'bit.ly', 'tinyurl.com', 'forms.gle', 'sites.google.com', 't.co', 'ow.ly'}

_KNOWN_ATS_DOMAINS = {
    'greenhouse.io', 'lever.co', 'workday.com', 'myworkdayjobs.com',
    'icims.com', 'smartrecruiters.com', 'bamboohr.com', 'jobvite.com',
    'taleo.net', 'ashbyhq.com', 'linkedin.com', 'indeed.com', 'ziprecruiter.com',
    'glassdoor.com', 'careers.google.com', 'amazon.jobs', 'microsoft.com',
    'apple.com', 'meta.com', 'netflix.com', 'stripe.com', 'airbnb.com',
    'uber.com', 'lyft.com', 'twitter.com', 'x.com', 'salesforce.com',
    'workable.com', 'recruitee.com', 'dover.com', 'rippling.com',
}

_FAANG_NAMES = {
    'google', 'amazon', 'apple', 'meta', 'facebook', 'netflix',
    'microsoft', 'twitter', 'uber', 'airbnb', 'stripe',
}

# Placeholder company patterns
_BARE_LLC_RE = re.compile(r'^(\S+\s+){0,2}(LLC|Inc|Corp|Ltd)\.?$', re.IGNORECASE)
_ALL_CAPS_SHORT_RE = re.compile(r'^[A-Z]{1,4}$')
_DIGITS_IN_NAME_RE = re.compile(r'\d')


@dataclass
class ScamResult:
    score: int = 0
    verdict: str = "clean"   # "clean" | "flagged" | "rejected"
    signals: list[str] = field(default_factory=list)


def _extract_hostname(url: str) -> str:
    """Return the netloc hostname from a URL, stripping leading www."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        host = host.lower()
        if host.startswith('www.'):
            host = host[4:]
        return host
    except Exception:
        return url.lower()


def _is_known_domain(hostname: str) -> bool:
    """Return True if hostname matches or is a subdomain of a known ATS/company domain."""
    if hostname in _KNOWN_ATS_DOMAINS:
        return True
    for known in _KNOWN_ATS_DOMAINS:
        if hostname.endswith('.' + known):
            return True
    return False


def check_scam(url: str, title: str, company: str, raw_description: str = "") -> ScamResult:
    """Score a job posting for scam signals. Synchronous, pure-Python. No I/O. Thread-safe.

    Args:
        url: The job posting URL.
        title: Job title as scraped.
        company: Company name as scraped.
        raw_description: Raw description text (plain text, no HTML).

    Returns:
        ScamResult with numeric score (0–100), verdict, and list of signal strings.
    """
    result = ScamResult()
    score = 0
    signals: list[str] = []

    desc_lower = raw_description.lower()
    title_lower = title.lower()
    hostname = _extract_hostname(url)

    # --- Signal: Generic contact email in description (+30) ---
    if _GENERIC_EMAIL_RE.search(raw_description):
        score += 30
        signals.append("Generic contact email")

    # --- Signal: Too-good-to-be-true language (max +40, +20 per phrase up to 2 hits) ---
    ttgbt_hits = 0
    for phrase in _TTGBT_PHRASES:
        if phrase in desc_lower:
            if ttgbt_hits < 2:
                score += 20
                ttgbt_hits += 1
            signals.append(f"Suspicious language: {phrase}")

    # --- Signal: Very short description (+20) ---
    word_count = len(raw_description.split()) if raw_description.strip() else 0
    if word_count < 150:
        score += 20
        signals.append("Very short job description")

    # --- Signal: Suspicious TLD (+35) ---
    tld = '.' + hostname.rsplit('.', 1)[-1] if '.' in hostname else ''
    if tld in _SUSPICIOUS_TLDS:
        score += 35
        signals.append("Suspicious URL domain")

    # --- Signal: URL shortener host (+35) ---
    if hostname in _URL_SHORTENERS:
        score += 35
        signals.append("URL shortener")

    # --- Signal: Unknown hosting domain (+25) ---
    if not _is_known_domain(hostname) and hostname not in _URL_SHORTENERS and tld not in _SUSPICIOUS_TLDS:
        score += 25
        signals.append("Unknown hosting domain")

    # --- Signal: Company name placeholder (+15) ---
    company_stripped = company.strip()
    placeholder = False
    if _ALL_CAPS_SHORT_RE.match(company_stripped):
        placeholder = True
    elif _BARE_LLC_RE.match(company_stripped):
        # bare "LLC" or "Inc" with fewer than 3 words before it
        words_before = len(company_stripped.split()) - 1
        if words_before < 3:
            placeholder = True
    elif _DIGITS_IN_NAME_RE.search(company_stripped):
        placeholder = True

    if placeholder:
        score += 15
        signals.append("Unverifiable company name")

    # --- Signal: FAANG impersonation (+40) ---
    # Title mentions a FAANG name but the URL hostname does not match it
    for brand in _FAANG_NAMES:
        if brand in title_lower and brand not in hostname:
            score += 40
            signals.append(f"Possible impersonation")
            break  # one hit is enough

    # Cap at 100
    score = min(score, 100)

    result.score = score
    result.signals = signals

    if score >= 80:
        result.verdict = "rejected"
    elif score >= 40:
        result.verdict = "flagged"
    else:
        result.verdict = "clean"

    return result
