#!/usr/bin/env python3
"""Parser library for EyesOnFlock ALPR organization names.

Extracts structured fields (city, state, type) from raw organization names
found in Flock Safety transparency portal data.

Patterns handled (in priority order):
  1. [Federal] prefix         → type="federal"
  2. XX - Name                → state-prefix pattern
  3. School/University/College → type="school"
  4. Name -XX  or Name-XX     → dash-state pattern
  5. Name (XX)                → parenthesized state
  6. ... Const ST Pct N       → constable (type="pd")
  7. ... PD                   → type="pd"
  8. ... SO                   → type="so"
  9. City of X ST             → extract city
 10. Fallback: scan for any 2-letter state code
"""

import re

# ── Valid US state/territory codes ────────────────────────────────────────────

VALID_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
    "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI",
    "WY", "PR", "GU", "VI", "AS", "MP",
}

# Words that are also state codes but commonly appear as words/abbreviations
# in names. We only accept these as states when they are adjacent to the type
# suffix (PD/SO/SD), follow "County", or sit at end-of-string. "CO" is in this
# set because names frequently abbreviate "County" as "Co"/"CO" (e.g.,
# "RIV CO ARSON TF CA PD" means Riverside County, California — not Colorado).
_AMBIGUOUS_WORDS = {"IN", "OR", "OK", "ME", "HI", "AL", "PA", "ID", "CO"}

# Full state name → code mapping for fallback matching
_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_valid_state(code: str) -> bool:
    """Check if a 2-letter code is a valid US state/territory."""
    return code.upper() in VALID_STATES


def _find_state_in_text(text: str, *, strict: bool = True) -> str | None:
    """Scan *text* for a 2-letter state code token.

    When *strict* is True (default) we skip codes that are also common
    English words (IN, OR, OK …) unless the code appears right before PD/SO
    or at the very end of the string.

    Returns the first match found (scanning left-to-right), or None.
    """
    tokens = text.split()
    for i, tok in enumerate(tokens):
        # Strip parentheses
        clean = tok.strip("()")
        if len(clean) == 2 and clean.isalpha() and clean.isupper() and _is_valid_state(clean):
            if strict and clean in _AMBIGUOUS_WORDS:
                # Allow if it's right before PD/SO/at end/after County
                next_tok = tokens[i + 1] if i + 1 < len(tokens) else ""
                prev_tok = tokens[i - 1] if i > 0 else ""
                if next_tok in ("PD", "SO", "SD") or i == len(tokens) - 1:
                    return clean
                if prev_tok in ("County",):
                    return clean
                continue
            return clean
    return None


def _extract_city_from_pd(name: str, state: str) -> str | None:
    """Extract city name from a PD-type org name, given the state code."""
    # "City ST PD" → City is everything before " ST PD"
    m = re.match(rf"^(.+?)\s+{re.escape(state)}\s+PD$", name)
    if m:
        return m.group(1).strip()

    # "City PD (ST)" → City is everything before " PD"
    m = re.match(r"^(.+?)\s+PD\b", name)
    if m:
        city = m.group(1).strip()
        # Remove state code if it's at the end of city
        city = re.sub(rf"\s+{re.escape(state)}$", "", city)
        return city if city else None

    return None


def _extract_city_from_so(name: str, state: str) -> str | None:
    """Extract city/county name from an SO-type org name."""
    # "County ST SO" pattern
    m = re.match(rf"^(.+?)\s+{re.escape(state)}\s+SO\b", name)
    if m:
        return m.group(1).strip()

    # "County SO ST" pattern
    m = re.match(rf"^(.+?)\s+SO\s+{re.escape(state)}\b", name)
    if m:
        return m.group(1).strip()

    # "County SO (ST)" pattern
    m = re.match(r"^(.+?)\s+SO\b", name)
    if m:
        city = m.group(1).strip()
        city = re.sub(rf"\s+{re.escape(state)}$", "", city)
        return city if city else None

    return None


# ── Status-tag stripping ──────────────────────────────────────────────────────

# Matches status tags that Flock portal users append to agency names.
# These don't identify distinct agencies — they're administrative metadata.
# Stripping them lets duplicate references resolve to the same canonical slug.
_STATUS_TAG_INSIDE = (
    r"inactive|dnu|do\s*not\s*use|do\s*not\s*activate|deleted|removed|"
    r"dup(?:licate)?|old|dead(?:/old)?|deactivated|dn[ou]|raven(?:\s*v?\d+)?|"
    r"insight|flex|demo|demo\s*cam|test"
)
_BRACKET_TAG_RE = re.compile(
    rf"\s*[\[(]\s*(?:{_STATUS_TAG_INSIDE})\s*[\])]", re.IGNORECASE
)
_SUFFIX_TAG_RE = re.compile(
    rf"\s*[-–—]\s*(?:{_STATUS_TAG_INSIDE})(?:\s+.*)?$", re.IGNORECASE
)
_PREFIX_TAG_RE = re.compile(
    rf"^\s*(?:dnu|do\s*not\s*use|\(dead/?old\)|\(revoked\s+mou\))\s*[-:]?\s*",
    re.IGNORECASE,
)


def _strip_status_tags(name: str) -> str:
    """Remove administrative/status tags that don't identify a distinct agency.

    Strips [Inactive], (DNU), - DUPLICATE, "DNU - ", "(Dead/Old) ", etc., in
    whatever combination appears. Collapses internal whitespace to single spaces.
    """
    prev = None
    cleaned = name
    # Apply repeatedly so combinations like "[Inactive] - DO NOT USE" fully resolve
    while prev != cleaned:
        prev = cleaned
        cleaned = _BRACKET_TAG_RE.sub("", cleaned)
        cleaned = _SUFFIX_TAG_RE.sub("", cleaned)
        cleaned = _PREFIX_TAG_RE.sub("", cleaned)
    # Collapse any doubled whitespace left behind
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ── Agency-suffix normalization ───────────────────────────────────────────────

# Flock sharing lists occasionally use the spelled-out forms ("Sheriff's Office",
# "Police Department") instead of the "SO"/"PD" abbreviations the rest of the
# parser expects. Collapse the long forms to the abbreviations so the existing
# patterns match and sharing-list references unify with the matching portal.
_SHERIFF_SUFFIX_RE = re.compile(
    r"\bSheriff(?:['’]?s)?"
    r"(?:\s+(?:Office|Department|Dept\.?))?"
    r"\b\.?",
    re.IGNORECASE,
)
_POLICE_SUFFIX_RE = re.compile(r"\bPolice\s+Department\b", re.IGNORECASE)

# "(XX)" embedded mid-string — unwrap to a bare state token so the PD/SO
# patterns can match. Only rewrites when XX is a valid state code.
_PAREN_STATE_RE = re.compile(r"\(([A-Z]{2})\)")


def _unwrap_paren_state(name: str) -> str:
    """Replace "(XX)" with "XX" when XX is a valid state code.

    Agencies like "Everett (WA) Police Department" wrap the state in parens
    in the middle of the name. The paren-state pattern only matches at EOS,
    so unwrapping lets PD/SO detection find the state in the regular path.
    """
    def repl(m: re.Match) -> str:
        code = m.group(1)
        return code if _is_valid_state(code) else m.group(0)
    return _PAREN_STATE_RE.sub(repl, name)


def _normalize_agency_suffix(name: str) -> str:
    """Collapse spelled-out agency suffixes to the SO/PD abbreviations.

    Examples:
      "Yuba County Sheriffs Office"        → "Yuba County SO"
      "Clark Co. IL Sheriff's Dept."       → "Clark Co. IL SO."  (trailing dot harmless)
      "Kings County Sheriff's Office CA"   → "Kings County SO CA"
      "Everett (WA) Police Department"     → "Everett (WA) PD"
    """
    # Police Department → PD first (less ambiguous; also won't touch "PD").
    cleaned = _POLICE_SUFFIX_RE.sub("PD", name)
    # Sheriff / Sheriff's / Sheriffs / Sheriff's Office / Sheriff's Dept → SO
    cleaned = _SHERIFF_SUFFIX_RE.sub("SO", cleaned)
    # Collapse whitespace in case a replacement left doubled spaces.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_org_name(raw_name: str) -> dict:
    """Parse a raw organization name into structured fields.

    Returns a dict with keys: raw_name, city, state, type.
    """
    result = {
        "raw_name": raw_name,
        "city": None,
        "state": None,
        "type": "other",
    }

    # Working copy — strip administrative tags and normalize spelled-out
    # agency suffixes before pattern-matching. raw_name is preserved in the
    # result; parsed fields ignore status tags and "Sheriff's Office"/"Police
    # Department" long forms.
    name = _strip_status_tags(raw_name.strip())
    name = _normalize_agency_suffix(name)
    name = _unwrap_paren_state(name)

    # ── 2. Federal ────────────────────────────────────────────────────────
    if name.startswith("[Federal]"):
        result["type"] = "federal"
        inner = name[len("[Federal]"):].strip()
        state = _find_state_in_text(inner, strict=False)
        result["state"] = state if state else "DC"
        return result

    # ── 3. State-prefix: "XX - Name" or "XX- Name" ───────────────────────
    m = re.match(r"^([A-Z]{2})\s*-\s*(.+)$", name)
    if m and _is_valid_state(m.group(1)):
        state = m.group(1)
        remainder = m.group(2).strip()
        result["state"] = state

        # Determine type from remainder
        if _has_school_keyword(remainder):
            result["type"] = "school"
        elif remainder.endswith(" PD") or " PD " in remainder:
            result["type"] = "pd"
            city = _extract_city_from_pd(remainder, state)
            if city:
                result["city"] = city
            else:
                # "Newburgh PD" → city = "Newburgh"
                m2 = re.match(r"^(.+?)\s+PD\b", remainder)
                if m2:
                    result["city"] = m2.group(1).strip()
        elif " SO" in remainder:
            result["type"] = "so"
            m2 = re.match(r"^(.+?)\s+SO\b", remainder)
            if m2:
                result["city"] = m2.group(1).strip()
        else:
            # Try to find any state in remainder too (shouldn't be needed)
            pass

        return result

    # ── 4. School / University / College ──────────────────────────────────
    if _has_school_keyword(name):
        result["type"] = "school"
        state = _find_state_in_text(name)
        result["state"] = state
        return result

    # ── 5. Dash-state: "Name -XX" or "Name-XX" ───────────────────────────
    m = re.match(r"^(.+?)\s*-([A-Z]{2})$", name)
    if m and _is_valid_state(m.group(2)):
        state = m.group(2)
        remainder = m.group(1).strip()
        result["state"] = state

        if " SO" in remainder or remainder.endswith(" SO"):
            result["type"] = "so"
            m2 = re.match(r"^(.+?)\s+SO\b", remainder)
            if m2:
                result["city"] = m2.group(1).strip()
        elif " PD" in remainder or remainder.endswith(" PD"):
            result["type"] = "pd"
            m2 = re.match(r"^(.+?)\s+PD\b", remainder)
            if m2:
                result["city"] = m2.group(1).strip()

        return result

    # ── 5b. Constable pattern (must run before paren-state) ──────────────
    # "X County Const ST Pct N"
    m = re.match(
        r"^(.+?\s+County)\s+Const(?:able)?(?:'s)?\s+([A-Z]{2})\s+Pct\s+(\d+)",
        name,
    )
    if m and _is_valid_state(m.group(2)):
        result["type"] = "pd"
        # Include precinct in city so slugs don't collapse across precincts
        result["city"] = f"{m.group(1).strip()} Constable Pct {m.group(3)}"
        result["state"] = m.group(2)
        return result

    # "X County ST Constable Pct N" and "X County TX Constable's Pct N"
    m = re.match(
        r"^(.+?\s+County)\s+([A-Z]{2})\s+Constable(?:'s)?\s+Pct\s+(\d+)",
        name,
    )
    if m and _is_valid_state(m.group(2)):
        result["type"] = "pd"
        result["city"] = f"{m.group(1).strip()} Constable Pct {m.group(3)}"
        result["state"] = m.group(2)
        return result

    # "X County Const Pct N (TX)" — and its unwrapped form "X County Const Pct N TX"
    # (the paren-unwrap preprocessor converts "(TX)" → "TX" before we get here).
    m = re.match(
        r"^(.+?\s+County)\s+Const(?:able)?(?:'s)?\s+Pct\s+(\d+)\s*\(?([A-Z]{2})\)?\s*$",
        name,
    )
    if m and _is_valid_state(m.group(3)):
        result["type"] = "pd"
        result["city"] = f"{m.group(1).strip()} Constable Pct {m.group(2)}"
        result["state"] = m.group(3)
        return result

    # ── 5c. Parenthesized state: "Name (XX)" ─────────────────────────────
    m = re.match(r"^(.+?)\s*\(([A-Z]{2})\)\s*$", name)
    if m and _is_valid_state(m.group(2)):
        state = m.group(2)
        remainder = m.group(1).strip()
        result["state"] = state

        if " SO" in remainder or remainder.endswith(" SO"):
            result["type"] = "so"
            m2 = re.match(r"^(.+?)\s+SO\b", remainder)
            if m2:
                result["city"] = m2.group(1).strip()
        elif " PD" in remainder or remainder.endswith(" PD"):
            result["type"] = "pd"
            m2 = re.match(r"^(.+?)\s+PD\b", remainder)
            if m2:
                result["city"] = m2.group(1).strip()

        return result

    # ── 7. PD pattern ─────────────────────────────────────────────────────
    if " PD" in name:
        result["type"] = "pd"
        # Try to find state
        state = _find_state_in_text(name)
        result["state"] = state
        if state:
            result["city"] = _extract_city_from_pd(name, state)
        else:
            # Stateless: still capture "<city> PD" as the city so a later
            # pass can rescue the entry by matching against portal data.
            m = re.match(r"^(.+?)\s+PD\b", name)
            if m:
                result["city"] = m.group(1).strip()
        return result

    # ── 8. SO pattern ─────────────────────────────────────────────────────
    if " SO" in name:
        result["type"] = "so"
        state = _find_state_in_text(name)
        result["state"] = state
        if state:
            result["city"] = _extract_city_from_so(name, state)
        else:
            # Stateless: still capture "<county> SO" as the city so a later
            # pass can rescue the entry by matching against portal data.
            m = re.match(r"^(.+?)\s+SO\b", name)
            if m:
                result["city"] = m.group(1).strip()
        return result

    # ── 9. "City of X ST" ────────────────────────────────────────────────
    m = re.match(r"^City\s+of\s+(.+)", name)
    if m:
        remainder = m.group(1).strip()
        state = _find_state_in_text(remainder)
        result["state"] = state
        if state:
            # Remove state code and any trailing type from city
            city = re.sub(rf"\s+{re.escape(state)}(\s+.*)?$", "", remainder).strip()
            result["city"] = city if city else None
        return result

    # ── 10. Fallback: scan for any state code ─────────────────────────────
    state = _find_state_in_text(name)
    result["state"] = state

    # ── 11. Last resort: match full state name in text ────────────────────
    if not state:
        state = _find_state_name_in_text(name)
        result["state"] = state

    return result


# School detection — tight enough to avoid cities that happen to contain the
# word "college" / "university" (Collegedale, College Place, University
# Heights, North College Hill, etc.). Matches:
#   - "University of X" / "College of X" anywhere  (universities named by state)
#   - name ending in "...University [ST] [PD]" / "...College [ST] [PD]"
#   - phrases that strongly signal an educational institution
_SCHOOL_OF_RE = re.compile(r"\b(?:university|college)\s+of\b", re.IGNORECASE)
_SCHOOL_END_RE = re.compile(
    r"\b(?:university|college|school)"
    r"(?:\s+[A-Z]{2})?"                              # optional state code
    r"(?:\s+(?:PD|SO|SD|DPS|Police|Campus\s+Police|Insight|Raven|Flex))*"  # optional type/brand
    r"\s*$",
    re.IGNORECASE,
)
_SCHOOL_PHRASE_RE = re.compile(
    r"\b(?:high\s+school|middle\s+school|elementary\s+school|"
    r"school\s+district|community\s+college|campus\s+police)\b",
    re.IGNORECASE,
)


def _has_school_keyword(text: str) -> bool:
    """Detect whether a name identifies a school/university/college entity.

    Tight enough to avoid cities whose names contain the substrings (e.g.,
    'College Place', 'University Heights', 'Collegedale', 'North College Hill').
    """
    return bool(
        _SCHOOL_OF_RE.search(text)
        or _SCHOOL_END_RE.search(text)
        or _SCHOOL_PHRASE_RE.search(text)
    )


def _find_state_name_in_text(text: str) -> str | None:
    """Find a full US state name in text and return its 2-letter code.

    Used as a last-resort fallback when no 2-letter code is found.
    Matches against the longest state name first to prefer
    "New York" over "New" or "York".
    """
    lower = text.lower()
    # Sort by length descending so "West Virginia" matches before "Virginia"
    for name, code in sorted(
        _STATE_NAME_TO_CODE.items(), key=lambda x: len(x[0]), reverse=True
    ):
        if name in lower:
            return code
    return None


# ── Slugify ───────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """Convert an organization name to a URL-safe slug.

    - Lowercase
    - Remove special characters (brackets, parentheses, apostrophes, ampersands)
    - Replace whitespace and dashes with single hyphens
    - Strip leading/trailing hyphens
    """
    s = name.lower()
    # Remove brackets and their contents like [Federal], [inactive]
    # Actually keep the text inside brackets, just remove the brackets themselves
    s = s.replace("[", "").replace("]", "")
    # Remove other special characters
    s = re.sub(r"[()&'\".,/]", "", s)
    # Replace whitespace and dashes with hyphens
    s = re.sub(r"[\s\-]+", "-", s)
    # Strip leading/trailing hyphens
    s = s.strip("-")
    return s


# ── Canonical slugging ────────────────────────────────────────────────────────

def _normalize_city(city: str, *, org_type: str) -> str:
    """Normalize a parsed city/county string before slugging.

    - Strip leading/trailing whitespace and dashes
    - Collapse internal whitespace to single spaces
    - For SO types, ensure " County" suffix is present (so that
      "Coconino AZ SO" and "Coconino County AZ SO" converge)
    """
    c = re.sub(r"\s+", " ", city).strip()
    c = c.strip("-—–").strip()
    if org_type == "so" and c and "county" not in c.lower():
        c = f"{c} County"
    return c


def canonical_slug(parsed: dict, raw_name: str) -> str:
    """Build a stable slug keyed on (canonical_city, state, type).

    Goal: two raw names that refer to the same agency (e.g., "Allen Park MI PD"
    and "Allen Park PD MI") produce the same slug.

    Fallback: when the parser could not extract enough structure (no state,
    or type=other with no clear pattern), slug the status-tag-stripped raw_name.
    This still dedupes inactive/DNU variants but keeps distinct unparseable
    agencies separate.
    """
    state = (parsed.get("state") or "").strip()
    ptype = (parsed.get("type") or "other").strip().lower()
    city = parsed.get("city") or ""

    if state and ptype in ("pd", "so") and city:
        city_norm = _normalize_city(city, org_type=ptype)
        if city_norm:
            return slugify(f"{city_norm} {state} {ptype.upper()}")

    # Schools, federal agencies, and anything else: slug the tag-stripped
    # raw name. Raw names for these categories typically already encode the
    # state/type (e.g., "Stanford University CA PD", "[Federal] ATF KY"), so
    # appending state here would produce doubled suffixes like "-ca-pd-ca".
    result = slugify(_normalize_agency_suffix(_strip_status_tags(raw_name)))
    # Protect against status-tag stripping that eats the entire name
    # (e.g., "DO NOT USE" → "") — fall back to the raw name slug.
    return result or slugify(raw_name) or "unnamed"


def portal_canonical_slug(portal: dict) -> str:
    """Build the canonical slug for an EyesOnFlock portal dict.

    Delegates to canonical_slug(parse_org_name(...)) via the synthesized
    portal name. This is the single source of truth for slugging, so a
    portal and any sharing-list reference to the same agency always resolve
    to the same slug — even when the portal's declared type (e.g., "PD")
    disagrees with the parser's classification of the name (e.g., "school"
    for universities).
    """
    name = portal_as_org_name(portal)
    if not name:
        return slugify(portal.get("slug") or "")
    return canonical_slug(parse_org_name(name), name)


def portal_as_org_name(portal: dict) -> str:
    """Reconstruct a canonical org name string from a portal's structured fields.

    Used when adding portals-not-in-any-sharing-list as standalone nodes
    (we want the node's `raw_name` to be readable, not Flock's URL slug).
    """
    ptype_raw = (portal.get("type") or "").upper()
    state = (portal.get("state") or "").strip()

    if ptype_raw == "SD":
        county = (portal.get("county") or "").strip()
        if county and "County" not in county:
            county = f"{county} County"
        return f"{county} {state} SO".strip()

    if ptype_raw == "PD":
        city = (portal.get("city") or "").strip()
        return f"{city} {state} PD".strip()

    return portal.get("slug") or ""
