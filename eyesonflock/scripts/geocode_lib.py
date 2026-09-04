#!/usr/bin/env python3
"""Geocoding library for matching parsed organizations to Census gazetteer coordinates.

Provides lookup builders for place, county, and state gazetteers, plus a
geocode_org function that tries multiple matching strategies in order:
  1. Exact place match (city + state)
  2. Place variant match (St./Saint, city suffix, etc.)
  3. County match
  4. State centroid fallback
  5. Default (Washington DC)

Census gazetteer files are tab-separated with headers that may have trailing
whitespace.  All parsers strip() column headers and values.
"""

import csv
import re
from pathlib import Path

# Default fallback coordinates: Washington DC
DEFAULT_LAT = 38.8816
DEFAULT_LNG = -77.0910

# Manual geocode overrides for places where the Census cascade produces
# incorrect coordinates. Key: (city_lower, state_upper). Value: (lat, lng).
# Keep this list short and well-justified — each entry bypasses the entire
# cascade and is tagged geocode_method="manual" so 05_audit_geocoding.py
# surfaces them for review.
MANUAL_OVERRIDES: dict[tuple[str, str], tuple[float, float]] = {
    # San Francisco is a consolidated city-county; the county centroid
    # sits near the Farallon Islands. Override to the downtown centroid.
    ("san francisco", "CA"): (37.7749, -122.4194),
}


# ---------------------------------------------------------------------------
# Gazetteer lookup builders
# ---------------------------------------------------------------------------

def build_place_lookup(gazetteer_path: Path) -> dict:
    """Parse the Census place gazetteer into a lookup dict.

    Args:
        gazetteer_path: Path to 2023_Gaz_place_national.txt

    Returns:
        Dict mapping (name_lower, state) -> (lat, lng).
        Name includes the LSAD suffix (e.g., "abbeville city", "houston city").
    """
    lookup = {}
    with open(gazetteer_path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        # Strip whitespace from field names (Census headers have trailing spaces)
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            try:
                state = row["USPS"].strip()
                name = row["NAME"].strip()
                lat_str = row["INTPTLAT"].strip()
                lng_str = row["INTPTLONG"].strip()
                if not lat_str or not lng_str:
                    continue
                lat = float(lat_str)
                lng = float(lng_str)
                lookup[(name.lower(), state)] = (lat, lng)
            except (ValueError, KeyError):
                continue
    return lookup


def build_county_lookup(gazetteer_path: Path) -> dict:
    """Parse the Census county gazetteer into a lookup dict.

    Args:
        gazetteer_path: Path to 2023_Gaz_counties_national.txt

    Returns:
        Dict mapping (name_lower, state) -> (lat, lng).
        Stores both the full name (e.g., "autauga county") and, if the name
        ends with " County", also the bare name (e.g., "autauga").
    """
    lookup = {}
    with open(gazetteer_path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            try:
                state = row["USPS"].strip()
                name = row["NAME"].strip()
                lat_str = row["INTPTLAT"].strip()
                lng_str = row["INTPTLONG"].strip()
                if not lat_str or not lng_str:
                    continue
                lat = float(lat_str)
                lng = float(lng_str)
                coords = (lat, lng)
                # Store full name (e.g., "autauga county")
                lookup[(name.lower(), state)] = coords
                # Also store bare name without " County" suffix
                lower = name.lower()
                if lower.endswith(" county"):
                    bare = lower[: -len(" county")]
                    lookup[(bare, state)] = coords
            except (ValueError, KeyError):
                continue
    return lookup


def build_state_lookup(gazetteer_path: Path) -> dict:
    """Parse the Census state gazetteer into a lookup dict.

    Args:
        gazetteer_path: Path to 2023_Gaz_state_national.txt

    Returns:
        Dict mapping state_code -> (lat, lng).
    """
    lookup = {}
    with open(gazetteer_path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            try:
                state = row["USPS"].strip()
                lat_str = row["INTPTLAT"].strip().lstrip("+")
                lng_str = row["INTPTLONG"].strip()
                if not lat_str or not lng_str:
                    continue
                lat = float(lat_str)
                lng = float(lng_str)
                lookup[state] = (lat, lng)
            except (ValueError, KeyError):
                continue
    return lookup


# ---------------------------------------------------------------------------
# City name variations
# ---------------------------------------------------------------------------

def _city_variations(city: str) -> list[str]:
    """Generate common name variations for fuzzy place matching.

    Tries transformations:
    - "St." <-> "Saint"
    - Append " city"
    - Remove Township/Borough/Village/Town suffixes
    - Remove "County" and try as place name

    Args:
        city: Original city name.

    Returns:
        List of alternative city name strings (all lowercased).
    """
    variations = []
    lower = city.lower().strip()

    # St. <-> Saint
    if lower.startswith("st. "):
        variations.append("saint " + lower[4:])
    elif lower.startswith("saint "):
        variations.append("st. " + lower[6:])
    if "st. " in lower:
        variations.append(lower.replace("st. ", "saint "))
    if "saint " in lower:
        variations.append(lower.replace("saint ", "st. "))

    # Append " city" — many Census place names end with " city"
    variations.append(lower + " city")

    # Remove common suffixes and try bare name
    for suffix in [" township", " borough", " village", " town"]:
        if lower.endswith(suffix):
            bare = lower[: -len(suffix)].strip()
            variations.append(bare)
            variations.append(bare + " city")
            break

    # Remove "County" and try as place
    if "county" in lower:
        bare = re.sub(r"\s*county\s*", " ", lower).strip()
        if bare:
            variations.append(bare)
            variations.append(bare + " city")

    # Constable precincts ("X County Constable Pct N") — fall back to county lookup
    m = re.match(r"^(.+?\s+county)\s+constable.*$", lower)
    if m:
        variations.append(m.group(1))  # "harris county" — matches county gazetteer

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for v in variations:
        if v not in seen and v != lower:
            seen.add(v)
            unique.append(v)
    return unique


# ---------------------------------------------------------------------------
# Main geocoding function
# ---------------------------------------------------------------------------

def geocode_org(
    org: dict,
    place_lookup: dict,
    county_lookup: dict,
    state_lookup: dict,
) -> tuple[float, float, str]:
    """Geocode a single organization using cascading lookup strategies.

    Strategy order:
      1. Exact place match: (city.lower(), state) in place_lookup
      2. Place variant match: try _city_variations()
      3. County match: (city.lower(), state) in county_lookup
      4. State fallback: state in state_lookup
      5. Default: Washington DC (38.8816, -77.0910)

    Args:
        org: Dict with at least 'city' and 'state' keys.
        place_lookup: From build_place_lookup().
        county_lookup: From build_county_lookup().
        state_lookup: From build_state_lookup().

    Returns:
        Tuple of (lat, lng, method) where method is one of:
        "manual" (from MANUAL_OVERRIDES), "place", "place_variant",
        "county", "state", "default".
    """
    city = org.get("city") or ""
    state = org.get("state") or ""
    city_lower = city.lower().strip()

    # Manual overrides win over the entire cascade.
    if city and state:
        override = MANUAL_OVERRIDES.get((city.lower(), state.upper()))
        if override:
            return override[0], override[1], "manual"

    # 1. Exact place match
    if city_lower and state:
        key = (city_lower, state)
        if key in place_lookup:
            lat, lng = place_lookup[key]
            return lat, lng, "place"

        # Also try with " city" appended (Census places often have this suffix)
        key_city = (city_lower + " city", state)
        if key_city in place_lookup:
            lat, lng = place_lookup[key_city]
            return lat, lng, "place"

        # Also try with " town" appended
        key_town = (city_lower + " town", state)
        if key_town in place_lookup:
            lat, lng = place_lookup[key_town]
            return lat, lng, "place"

        # Also try with " village" appended
        key_village = (city_lower + " village", state)
        if key_village in place_lookup:
            lat, lng = place_lookup[key_village]
            return lat, lng, "place"

        # Also try with " CDP" appended (Census Designated Place)
        key_cdp = (city_lower + " cdp", state)
        if key_cdp in place_lookup:
            lat, lng = place_lookup[key_cdp]
            return lat, lng, "place"

    # 2. Place variant match
    if city_lower and state:
        for variant in _city_variations(city_lower):
            key = (variant, state)
            if key in place_lookup:
                lat, lng = place_lookup[key]
                return lat, lng, "place_variant"

    # 3. County match
    if city_lower and state:
        key = (city_lower, state)
        if key in county_lookup:
            lat, lng = county_lookup[key]
            return lat, lng, "county"

        # Constable precinct: city is "X County Constable Pct N" —
        # geocode to the county centroid since precincts aren't in the gazetteer
        m = re.match(r"^(.+?\s+county)\s+constable", city_lower)
        if m:
            county_key = (m.group(1), state)
            if county_key in county_lookup:
                lat, lng = county_lookup[county_key]
                return lat, lng, "county"

    # 4. State fallback
    if state and state in state_lookup:
        lat, lng = state_lookup[state]
        return lat, lng, "state"

    # 5. Default fallback
    return DEFAULT_LAT, DEFAULT_LNG, "default"
