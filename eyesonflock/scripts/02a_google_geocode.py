#!/usr/bin/env python3
"""Upgrade state/default-fallback orgs using the Google Maps Geocoding API.

Reads:  <work>/intermediate/geocoded_orgs.json  (produced by 02_geocode_orgs.py)
        eyesonflock/.env or environment variable GOOGLEMAPSAPI
Writes: <work>/intermediate/geocoded_orgs.json  (updated in place)
Cache:  eyesonflock/google_geocode_cache.json   (committed seed; read + written in place)

For every org currently geocoded with method `state` or `default`, this step
sends a structured query to Google Maps and — when Google returns a plausible
result — upgrades the org to lat/lng with method='google'.

Entries with method='junk' are NEVER upgraded (that's a deliberate tag).
Entries already at place/place_variant/county precision are not touched.

If GOOGLEMAPSAPI is missing, the step runs in cache-only mode: cached results
are still applied (no network calls), and a warning notes that uncached
candidates stay at state-centroid coordinates. The pipeline therefore stays
runnable — and keeps its precise geocodes — without paid API access.
"""

import json
import os
import sys

from google_geocode import GoogleGeocoder, load_api_key
from paths import ENV_FILE, GEOCODED_ORGS_FILE, GOOGLE_CACHE_FILE
from state_bbox import is_in_state_bbox

GEOCODED_FILE = GEOCODED_ORGS_FILE
CACHE_FILE = GOOGLE_CACHE_FILE

# Methods that are candidates for Google upgrade
_UPGRADE_METHODS = {"state", "default"}


def _load_api_key() -> str | None:
    """Try env var first, then .env file."""
    key = os.environ.get("GOOGLEMAPSAPI")
    if key:
        return key
    return load_api_key(ENV_FILE)


def main():
    print("=== Google Geocode Fallback ===")

    if not GEOCODED_FILE.exists():
        print(f"  ERROR: {GEOCODED_FILE.name} not found. Run 02_geocode_orgs.py first.")
        sys.exit(1)

    api_key = _load_api_key()
    if not api_key:
        print("  WARNING: GOOGLEMAPSAPI not set (env var or .env) — cache-only mode.")
        print("           Cached results are applied; uncached candidates stay at state centroids.")

    with open(GEOCODED_FILE) as f:
        geocoded = json.load(f)

    candidates = [
        (slug, org)
        for slug, org in geocoded.items()
        if org.get("geocode_method") in _UPGRADE_METHODS
    ]
    print(f"  {len(candidates):,} candidates for Google upgrade")
    if not candidates:
        print("  Nothing to upgrade. Done.")
        return

    client = GoogleGeocoder(api_key=api_key, cache_path=CACHE_FILE)
    print(f"  Mode:  {'live' if client.is_live else 'cache-only'}")
    print(f"  Cache: {client.cache_size:,} entries already")

    upgraded = 0
    failed = 0
    rejected = 0
    for i, (slug, org) in enumerate(candidates, 1):
        if i % 200 == 0:
            print(f"    {i}/{len(candidates)}  (API calls: {client.api_calls_made}, upgraded: {upgraded})")

        result = client.geocode_org(org)
        if result is None:
            failed += 1
            continue

        lat, lng = result
        # Plausibility: reject Google results that land outside the declared
        # state's bbox. Keeps the org at its prior state-centroid coords and
        # drops the poisoned cache entry so the next paid run can retry.
        if not is_in_state_bbox(lat, lng, org.get("state")):
            client.invalidate(org)
            rejected += 1
            continue

        org["lat"] = round(lat, 4)
        org["lng"] = round(lng, 4)
        org["geocode_method"] = "google"
        upgraded += 1

    if client.is_live:
        client.save_cache()

    with open(GEOCODED_FILE, "w") as f:
        json.dump(geocoded, f, indent=2)

    print(f"\n  Upgraded:    {upgraded:,}")
    print(f"  Rejected (out of state bbox): {rejected:,}")
    print(f"  Still at state/default:       {failed:,}")
    print(f"  API calls made:               {client.api_calls_made:,}")
    print(f"  Cache size now:               {client.cache_size:,}")
    print("Done.")


if __name__ == "__main__":
    main()
