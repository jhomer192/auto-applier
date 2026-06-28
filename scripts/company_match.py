"""Shared canonical company matching helpers.

Goals:
- "Cloudflare" matches "Cloudflare Inc" / "cloudflare" / "Cloudflare,"
- "Acme AI" matches "acme.ai"
- "Globex" matches "Globex Corp"
- "Cloudflakes" does NOT match "Cloudflare" (loose-character similarity is wrong)
"""
from __future__ import annotations

import re

# Suffixes safe to strip — corporate forms only. Don't strip "AI" — too many
# real company names end in AI (Anthropic, Anthropic AI, etc.).
_SUFFIXES = frozenset({
    "inc", "incorporated",
    "corp", "corporation",
    "ltd", "limited",
    "llc", "lp", "plc",
    "co", "company",
    "gmbh", "sa", "sas",
})


def canonical(name: str) -> str:
    """Lowercase, drop punctuation, strip common corporate suffixes."""
    if not name:
        return ""
    s = re.sub(r"[.,'\-/]", " ", name.lower())
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = s.split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens) if tokens else s


def matches(stored: str, query: str) -> bool:
    """Strict-ish equivalence after canonicalization. No loose char similarity."""
    cs, cq = canonical(stored), canonical(query)
    if not cs or not cq:
        return False
    if cs == cq:
        return True
    # Same with whitespace removed — catches "acme ai" vs "acmeai".
    if cs.replace(" ", "") == cq.replace(" ", ""):
        return True
    # Whole-word substring (with word boundaries) so "Cloudflare" matches
    # "Cloudflare Workers" but not "Cloudflakes".
    cs_padded, cq_padded = f" {cs} ", f" {cq} "
    if cs_padded in cq_padded or cq_padded in cs_padded:
        return True
    return False
