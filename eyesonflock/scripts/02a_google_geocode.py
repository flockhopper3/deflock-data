#!/usr/bin/env python3
"""Upgrade state/default-fallback orgs via Google: Geocoding, then Places.

Reads:  <work>/intermediate/geocoded_orgs.json  (produced by 02_geocode_orgs.py)
        eyesonflock/.env or environment variable GOOGLEMAPSAPI
Writes: <work>/intermediate/geocoded_orgs.json  (updated in place)
        <work>/intermediate/google_geocode_run.json (what this step did; shown in the CI job summary)
Cache:  eyesonflock/google_geocode_cache.json   (committed seed; read + written in place)

For every org currently geocoded with method `state` or `default`, this step
asks the Geocoding API for the parsed jurisdiction (``City, ST`` / ``County,
ST``, or the raw name when nothing parsed). If that yields nothing plausible,
it asks Places (New) text search for the raw agency name — the tool for
"Panhandle Auto Burglary and Theft Unit"-style names. A plausible answer
upgrades the org to method='google' or 'google_places'.

A result whose coordinates fall outside the declared state's bbox is not
plausible: it is skipped (the cache keeps Google's answer so the next run
re-checks it for free instead of paying to hear it again) and the cascade
moves to the next source.

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
from paths import ENV_FILE, GEOCODED_ORGS_FILE, GOOGLE_CACHE_FILE, GOOGLE_RUN_STATUS_FILE
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
    mode = "live" if client.is_live else "cache-only"
    cache_before = client.cache_size
    print(f"  Mode:  {mode}")
    print(f"  Cache: {cache_before:,} entries already")

    upgraded = 0
    upgraded_by = {"google": 0, "google_places": 0}
    failed = 0
    rejected = 0
    cascade = (("google", client.geocode_org), ("google_places", client.places_lookup))
    for i, (slug, org) in enumerate(candidates, 1):
        if i % 200 == 0:
            print(f"    {i}/{len(candidates)}  (API calls: {client.api_calls_made} + {client.places_calls_made} places, upgraded: {upgraded})")

        resolved = False
        for method, lookup in cascade:
            result = lookup(org)
            if result is None:
                continue
            lat, lng = result
            # Plausibility: a result outside the declared state's bbox is not an
            # answer for this org. Fall through to the next source.
            if not is_in_state_bbox(lat, lng, org.get("state")):
                rejected += 1
                continue
            org["lat"] = round(lat, 4)
            org["lng"] = round(lng, 4)
            org["geocode_method"] = method
            upgraded += 1
            upgraded_by[method] += 1
            resolved = True
            break
        if not resolved:
            failed += 1

    # Save whenever a key was present: refusals are never cached, so the file
    # only ever gains genuine answers.
    if api_key:
        client.save_cache()

    with open(GEOCODED_FILE, "w") as f:
        json.dump(geocoded, f, indent=2)

    status = {
        "mode": mode,
        "candidates": len(candidates),
        "upgraded": upgraded,
        "upgradedBy": upgraded_by,
        "rejectedOutOfState": rejected,
        "stillAtFallback": failed,
        "geocoding": client.api_status("geocoding"),
        "places": client.api_status("places"),
        "cacheEntriesBefore": cache_before,
        "cacheEntriesAfter": client.cache_size,
    }
    GOOGLE_RUN_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(GOOGLE_RUN_STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)

    print(f"\n  Upgraded:    {upgraded:,}  (geocoding {upgraded_by['google']:,}, places {upgraded_by['google_places']:,})")
    print(f"  Rejected (out of state bbox): {rejected:,}")
    print(f"  Still at state/default:       {failed:,}")
    print(f"  API calls made:               {client.api_calls_made:,} geocoding, {client.places_calls_made:,} places")
    print(f"  Cache size now:               {client.cache_size:,}")
    for api in ("geocoding", "places"):
        err = client.errors.get(api)
        if err is not None:
            st, msg = err
            print(f"\n  WARNING: Google {api} API error: {st} — {msg or '(no message)'}")
            if client._circuit_open[api]:
                print(f"           Stopped calling the {api} API for this run; nothing was cached for the refused queries.")
    if failed:
        print(f"           {failed:,} candidate(s) remain at state centroids.")
    print("Done.")


if __name__ == "__main__":
    main()
