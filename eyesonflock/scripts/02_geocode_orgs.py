#!/usr/bin/env python3
"""Geocode parsed organizations using Census gazetteer data.

Reads:  <work>/intermediate/parsed_orgs.json
        gazetteer/<vintage>_Gaz_place_national.txt
        gazetteer/<vintage>_Gaz_counties_national.txt
        gazetteer/<vintage>_Gaz_state_national.txt
Writes: <work>/intermediate/geocoded_orgs.json   (see paths.py)

For each organization, adds lat, lng, and geocode_method fields by matching
city/state against Census place, county, and state gazetteers in order of
specificity. Entries already flagged `is_junk=True` by 01_parse_orgs.py are
short-circuited with geocode_method='junk' and the state centroid (if known)
or the DC default — they retain coordinates so they still render, but the
frontend can hide them via the `isJunk` property.

Orgs that fall back to `state` or `default` here are candidates for the
optional 02a_google_geocode.py step, which upgrades them to precise
coordinates via the Google Maps API. See geocode_lib.py for the core
matching strategy.
"""

import json

from geocode_lib import (
    DEFAULT_LAT,
    DEFAULT_LNG,
    build_place_lookup,
    build_county_lookup,
    build_state_lookup,
    geocode_org,
)
from paths import COUNTY_GAZ, GEOCODED_ORGS_FILE, PARSED_ORGS_FILE, PLACE_GAZ, STATE_GAZ

INPUT_FILE = PARSED_ORGS_FILE
OUTPUT_FILE = GEOCODED_ORGS_FILE


def main():
    print("=== Geocode Organizations (Census) ===")

    # Load gazetteers
    print(f"  Loading place gazetteer ({PLACE_GAZ.name})...")
    place_lookup = build_place_lookup(PLACE_GAZ)
    print(f"    {len(place_lookup):,} places loaded")

    print(f"  Loading county gazetteer ({COUNTY_GAZ.name})...")
    county_lookup = build_county_lookup(COUNTY_GAZ)
    print(f"    {len(county_lookup):,} county entries loaded")

    print(f"  Loading state gazetteer ({STATE_GAZ.name})...")
    state_lookup = build_state_lookup(STATE_GAZ)
    print(f"    {len(state_lookup):,} states loaded")

    # Load parsed orgs
    print(f"  Loading {INPUT_FILE.name}...")
    with open(INPUT_FILE) as f:
        parsed_orgs = json.load(f)
    print(f"    {len(parsed_orgs):,} orgs loaded")

    # Geocode each org
    print("  Geocoding...")
    method_counts: dict[str, int] = {}
    geocoded: dict[str, dict] = {}

    for slug, org in parsed_orgs.items():
        entry = dict(org)

        if org.get("is_junk"):
            # Junk entries still get coordinates (so they remain mappable if
            # the frontend wants to show them) — but they're tagged so the
            # default view can filter them out.
            state = org.get("state")
            if state and state in state_lookup:
                lat, lng = state_lookup[state]
            else:
                lat, lng = DEFAULT_LAT, DEFAULT_LNG
            method = "junk"
        else:
            lat, lng, method = geocode_org(
                org, place_lookup, county_lookup, state_lookup
            )

        entry["lat"] = round(lat, 4)
        entry["lng"] = round(lng, 4)
        entry["geocode_method"] = method
        geocoded[slug] = entry
        method_counts[method] = method_counts.get(method, 0) + 1

    # Save output
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(geocoded, f, indent=2)

    total = len(geocoded)
    print(f"\n  Output: {OUTPUT_FILE.name}")
    print(f"  Total orgs geocoded: {total:,}")
    print(f"\n  Method breakdown:")
    for method in ["manual", "place", "place_variant", "county", "state", "default", "junk"]:
        count = method_counts.get(method, 0)
        pct = 100 * count / total if total > 0 else 0
        print(f"    {method:16s} {count:>5,}  ({pct:5.1f}%)")

    lats = [e["lat"] for e in geocoded.values()]
    lngs = [e["lng"] for e in geocoded.values()]
    print(f"\n  Lat range:  {min(lats):.4f} to {max(lats):.4f}")
    print(f"  Lng range: {min(lngs):.4f} to {max(lngs):.4f}")
    print("Done.")


if __name__ == "__main__":
    main()
