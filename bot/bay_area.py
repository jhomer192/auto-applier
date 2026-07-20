"""San Francisco Bay Area location gate.

Hard constraint (Jack's directive): the applier may apply ONLY to jobs located in
the Bay Area — no matter how the application was triggered (/force, a saved search,
or a discovery source). Two enforcement points use this module:
  - apply_via_mcp injects BAY_AREA_RULE into the apply prompt so the agent reads the
    live page and aborts before submitting anything off-target (covers /force and any
    URL whose location isn't known until the page is read).
  - auto_apply.process_queued_jobs calls is_bay_area() on the analyzed location to
    hard-pass non-Bay jobs before spending an apply.

is_bay_area() is intentionally STRICT: an unknown/empty/elsewhere location returns
False, so the autonomous path errs toward skipping rather than applying off-target.
"""
from __future__ import annotations

import re

# Substrings (lowercased) that positively identify a Bay Area location. Kept as a
# flat set of cities/regions plus the umbrella terms. Matched as substrings against
# a normalized location string — fine for "City, CA"-style location text.
_BAY_TERMS: set[str] = {
    # umbrella
    "bay area", "sf bay", "san francisco bay", "silicon valley", "the peninsula",
    "north bay", "east bay", "south bay",
    # Names shared with better-known places elsewhere are qualified with ", ca" —
    # bare "brisbane" matched an Australian role, bare "albany"/"lafayette"/"saratoga"/
    # "union city"/"richmond"/"danville"/"concord"/"belmont"/"dublin"/"newark" would
    # all match their East Coast or foreign namesakes. Keep in sync with scripts/source.py.
    # San Francisco
    "san francisco", "sf,", "s.f.", "daly city", "brisbane, ca", "south san francisco",
    # Peninsula
    "san mateo", "redwood city", "palo alto", "menlo park", "foster city",
    "burlingame", "san bruno", "millbrae", "san carlos", "belmont, ca", "half moon bay",
    "east palo alto",
    # South Bay / Silicon Valley
    "san jose", "santa clara", "sunnyvale", "mountain view", "cupertino", "milpitas",
    "los gatos", "campbell, ca", "saratoga, ca", "morgan hill", "gilroy",
    # East Bay
    "oakland", "berkeley", "emeryville", "alameda", "fremont, ca", "hayward",
    "richmond, ca", "walnut creek", "pleasanton", "dublin, ca", "san ramon",
    "concord, ca", "union city, ca", "newark, ca", "castro valley", "san leandro",
    "pleasant hill", "danville, ca", "livermore",
    "martinez, ca", "lafayette, ca", "orinda", "albany, ca",
    # North Bay / Marin
    "marin", "sausalito", "san rafael", "novato", "mill valley", "corte madera",
    "larkspur", "tiburon", "petaluma",
}

# California-but-NOT-Bay-Area cities that could otherwise sneak past a loose "CA"
# check. (Not strictly needed since matching is positive-only, but documents intent
# and guards against future loosening.)
_CA_NON_BAY = {
    "los angeles", "san diego", "sacramento", "irvine", "san luis obispo",
    "santa barbara", "santa monica", "pasadena", "fresno", "long beach",
}


def is_bay_area(*texts: str) -> bool:
    """True iff any provided text names a Bay Area location.

    Strict: empty/unknown/elsewhere → False. Pass the analyzed location plus any
    other location-bearing strings (title, snippet) you have.
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob.strip():
        return False
    return any(term in blob for term in _BAY_TERMS)


# Injected verbatim into the apply prompt — the agent reads the live posting and
# enforces this before filling/submitting anything.
BAY_AREA_RULE = """\
LOCATION RESTRICTION (mandatory — overrides every other instruction):
This applier may apply ONLY to jobs located in the San Francisco Bay Area. Right
after opening the page, determine the role's location. If it is NOT in the Bay Area,
STOP immediately — do NOT fill or submit anything — and report exactly:
RESULT: BLOCKED not-bay-area
The Bay Area = San Francisco; the East Bay (Oakland, Berkeley, Emeryville, Alameda,
Fremont, Hayward, Richmond, Walnut Creek, Pleasanton, Dublin, San Ramon, Concord,
Livermore); the Peninsula (South San Francisco, San Mateo, Redwood City, Palo Alto,
Menlo Park, Foster City, Burlingame); the South Bay / Silicon Valley (San Jose, Santa
Clara, Sunnyvale, Mountain View, Cupertino, Milpitas); and the North Bay / Marin
(Sausalito, San Rafael, Novato, Mill Valley, Petaluma). A hybrid role based in the Bay
Area counts. A fully-remote role counts ONLY if the listing names a Bay Area location
or the company is headquartered in the Bay Area; if a remote role is not clearly
Bay-Area-anchored, treat it as NOT Bay Area and block."""
