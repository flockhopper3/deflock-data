#!/usr/bin/env python3
"""Geocoding quality audit — generates a report on geocode method breakdown.

Reads:  <work>/intermediate/geocoded_orgs.json
Writes: <work>/intermediate/geocode_audit.txt   (see paths.py)

Reports:
  - Method breakdown (counts and percentages)
  - List of remaining state/default orgs for manual review
  - Cross-check: verify Google results fall within expected state bounding box
"""

import json

from paths import AUDIT_FILE, GEOCODED_ORGS_FILE
from state_bbox import STATE_BBOX

INPUT_FILE = GEOCODED_ORGS_FILE
OUTPUT_FILE = AUDIT_FILE


def main():
    print("=== Geocoding Quality Audit ===")

    if not INPUT_FILE.exists():
        print(f"  ERROR: {INPUT_FILE.name} not found. Run 02_geocode_orgs.py first.")
        return

    with open(INPUT_FILE) as f:
        geocoded = json.load(f)

    total = len(geocoded)
    lines = []
    lines.append("=" * 60)
    lines.append("GEOCODING QUALITY AUDIT")
    lines.append("=" * 60)
    lines.append(f"\nTotal organizations: {total:,}\n")

    # Method breakdown
    method_counts = {}
    for entry in geocoded.values():
        method = entry.get("geocode_method", "unknown")
        method_counts[method] = method_counts.get(method, 0) + 1

    lines.append("METHOD BREAKDOWN")
    lines.append("-" * 40)
    for method in ["manual", "place", "place_variant", "county", "google", "google_places", "state", "default", "junk"]:
        count = method_counts.get(method, 0)
        pct = 100 * count / total if total > 0 else 0
        lines.append(f"  {method:16s} {count:>5,}  ({pct:5.1f}%)")

    # Any unknown methods
    known = {"manual", "place", "place_variant", "county", "google", "google_places", "state", "default", "junk"}
    for method, count in sorted(method_counts.items()):
        if method not in known:
            pct = 100 * count / total if total > 0 else 0
            lines.append(f"  {method:16s} {count:>5,}  ({pct:5.1f}%)  [UNEXPECTED]")

    # Accuracy summary
    good = sum(method_counts.get(m, 0) for m in ("manual", "place", "place_variant", "county", "google", "google_places"))
    good_pct = 100 * good / total if total > 0 else 0
    lines.append(f"\n  Accurate (manual+place+variant+county+google+places): {good:,} ({good_pct:.1f}%)")

    # Remaining state/default orgs
    state_orgs = [
        (slug, e) for slug, e in geocoded.items()
        if e.get("geocode_method") == "state"
    ]
    default_orgs = [
        (slug, e) for slug, e in geocoded.items()
        if e.get("geocode_method") == "default"
    ]

    lines.append(f"\n\nREMAINING STATE-CENTROID ORGS ({len(state_orgs)})")
    lines.append("-" * 60)
    for slug, e in sorted(state_orgs, key=lambda x: x[1].get("state", "")):
        raw = e.get("raw_name", slug)
        st = e.get("state", "??")
        lines.append(f"  [{st}] {raw}")

    lines.append(f"\n\nREMAINING DEFAULT (DC) ORGS ({len(default_orgs)})")
    lines.append("-" * 60)
    for slug, e in sorted(default_orgs, key=lambda x: x[1].get("raw_name", "")):
        raw = e.get("raw_name", slug)
        lines.append(f"  {raw}")

    # Cross-check: verify Google results fall within expected state bbox
    google_orgs = [
        (slug, e) for slug, e in geocoded.items()
        if e.get("geocode_method") in ("google", "google_places")
    ]
    out_of_bounds = []
    for slug, e in google_orgs:
        state = e.get("state")
        if not state or state not in STATE_BBOX:
            continue
        min_lat, max_lat, min_lng, max_lng = STATE_BBOX[state]
        lat = e.get("lat", 0)
        lng = e.get("lng", 0)
        if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
            out_of_bounds.append((slug, e, state, lat, lng))

    lines.append(f"\n\nGOOGLE RESULTS BOUNDING BOX CHECK")
    lines.append("-" * 60)
    lines.append(f"  Google geocoded: {len(google_orgs)}")
    lines.append(f"  Out of expected state bounds: {len(out_of_bounds)}")
    if out_of_bounds:
        for slug, e, state, lat, lng in out_of_bounds[:50]:
            raw = e.get("raw_name", slug)
            lines.append(f"  WARNING: [{state}] {raw} -> ({lat}, {lng})")
        if len(out_of_bounds) > 50:
            lines.append(f"  ... and {len(out_of_bounds) - 50} more")

    lines.append("\n" + "=" * 60)

    report = "\n".join(lines)
    print(report)

    # Save to file
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(report + "\n")
    print(f"\n  Report saved: {OUTPUT_FILE.name}")
    print("Done.")


if __name__ == "__main__":
    main()
