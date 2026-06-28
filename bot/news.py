"""Stub for optional company-news enrichment (cover-letter hook).

This lineage shipped without bot/news.py; the apply flow imports two helpers
from it. Returning empty values keeps applications working without external
news lookups (the cover letter simply omits a news hook).
"""


async def fetch_company_news(company: str, limit: int = 3) -> list:
    return []


def get_news_hook_block(news_items: list) -> str:
    return ""
