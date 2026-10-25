"""LinkedIn employer discovery: card parsing and the lane/metro gate on names."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import search_jobs as J  # noqa: E402

CARD = """<li><div class="base-card"><h3 class="base-search-card__title">
  {title}
</h3><h4 class="base-search-card__subtitle">
  <a class="hidden-nested-link" href="x">
    {company}
  </a></h4><span class="job-search-card__location">
  {loc}
</span></div></li>"""


def _page(*cards):
    return "".join(CARD.format(title=t, company=c, loc=l) for t, c, l in cards)


def test_card_regex_reads_title_company_location():
    body = _page(("SOC Analyst", "Richemont &amp; Co", "New York, NY"),
                 ("Help Desk Technician", "CLEAR", "Austin, TX"))
    got = J._LI_CARD.findall(body)
    assert got == [("SOC Analyst", "Richemont &amp; Co", "New York, NY"),
                   ("Help Desk Technician", "CLEAR", "Austin, TX")]


def test_names_keep_only_in_lane_in_metro(monkeypatch):
    cards = [("SOC Analyst", "Acme", "New York, NY"),
             ("SOC Analyst", "Fedco", "McLean, VA"),          # NoVA: never
             ("Senior Software Engineer", "Codeco", "Austin, TX"),  # out of lane
             ("Security Analyst", "Denverco", "Denver, CO")]   # not a metro
    monkeypatch.setattr(J, "linkedin_cards", lambda *a: cards)
    monkeypatch.setattr(J, "LI_LOCATIONS", (("x", ""),))
    assert J.linkedin_names(1, titles=("SOC Analyst",)) == ["Acme"]
