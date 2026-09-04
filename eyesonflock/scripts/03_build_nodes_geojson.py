#!/usr/bin/env python3
"""Build GeoJSON FeatureCollection of sharing network nodes.

Reads:  <work>/intermediate/geocoded_orgs.json
        <work>/raw/eyesonflock_full_data.json
Writes: <work>/output/sharing-network-nodes.geojson   (see paths.py)

Each geocoded org becomes a GeoJSON Point feature with properties:
  id, name, city, state, type,
  isPortal, isJunk, isInactive, isLikelyAggregator, portalSlug,
  cameras, searches, vehiclesCaptured, connectionCount,
  population, hotlistHits, geocodeMethod, aliases

Aggregator detection: nodes with an improbably high connection count relative
to their population (suggesting Flock 'national coop' / template accounts
rather than organic small-town sharing) are flagged isLikelyAggregator=True
so the frontend can style or hide them. Treat the flag as an alert, not a
judgment.
"""

import json
import re
from collections import Counter

from parse_orgs_lib import canonical_slug, parse_org_name, portal_canonical_slug
from paths import GEOCODED_ORGS_FILE, NODES_FILE, SNAPSHOT_FILE

GEOCODED_ORGS = GEOCODED_ORGS_FILE
EYESONFLOCK = SNAPSHOT_FILE
OUTPUT_FILE = NODES_FILE

# Heuristic: a node with >= AGGREGATOR_MIN_DEGREE edges AND population <
# AGGREGATOR_MAX_POP is very likely a Flock 'national coop' / template
# account rather than an organic sharing hub. Real metropolitan sheriffs
# (e.g., Shelby County TN SO, pop ~938k) have high degrees too — the
# low-pop filter excludes them. Treat the flag as an alert, not a judgment.
AGGREGATOR_MIN_DEGREE = 500
AGGREGATOR_MAX_POP = 50_000

_INACTIVE_RE = re.compile(
    r"\b(?:inactive|dnu|do\s*not\s*use|deleted|removed|deactivated|dead)\b",
    re.IGNORECASE,
)


def _build_connection_counts(portals: list[dict]) -> Counter:
    """Count unique neighbors per canonical slug in the bidirectional graph.

    Uses set semantics (same as 04_build_adjacency.py) so that connectionCount
    always equals the node's adjacency-list length. Drops self-edges introduced
    when an alias in a portal's sharing list canonicalizes back to the portal's
    own slug.
    """
    adjacency: dict[str, set[str]] = {}
    name_cache: dict[str, str] = {}

    for portal in portals:
        shared = portal.get("organizations_shared_with") or []
        p_slug = portal_canonical_slug(portal)
        if not shared or not p_slug:
            continue

        connected: set[str] = set()
        for org_name in shared:
            slug = name_cache.get(org_name)
            if slug is None:
                slug = canonical_slug(parse_org_name(org_name), org_name)
                name_cache[org_name] = slug
            if slug and slug != p_slug:
                connected.add(slug)

        adjacency.setdefault(p_slug, set()).update(connected)
        for other in connected:
            adjacency.setdefault(other, set()).add(p_slug)

    return Counter({k: len(v) for k, v in adjacency.items()})


def _is_inactive(org: dict) -> bool:
    """True if this org's raw name OR any alias carries a status tag."""
    names = [org.get("raw_name") or ""] + (org.get("aliases") or [])
    return any(_INACTIVE_RE.search(n) for n in names)


def _is_likely_aggregator(connection_count: int, population: int) -> bool:
    if connection_count < AGGREGATOR_MIN_DEGREE:
        return False
    # Missing population is treated as small (unknown small agency)
    pop = population or 0
    return pop < AGGREGATOR_MAX_POP


def main():
    print("=== Build Nodes GeoJSON ===")

    with open(GEOCODED_ORGS) as f:
        orgs = json.load(f)

    with open(EYESONFLOCK) as f:
        data = json.load(f)

    connection_counts = _build_connection_counts(data["portals"])

    features = []
    aggregator_count = 0
    junk_count = 0
    inactive_count = 0

    for slug, org in orgs.items():
        lat = org.get("lat")
        lng = org.get("lng")
        if lat is None or lng is None:
            continue

        cc = connection_counts.get(slug, 0)
        pop = org.get("population", 0) or 0
        is_junk = bool(org.get("is_junk", False))
        is_inactive = _is_inactive(org)
        is_likely_aggregator = _is_likely_aggregator(cc, pop)

        if is_likely_aggregator:
            aggregator_count += 1
        if is_junk:
            junk_count += 1
        if is_inactive:
            inactive_count += 1

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lng, lat],  # GeoJSON is [lon, lat]
            },
            "properties": {
                "id": org["id"],
                "name": org["raw_name"],
                "city": org.get("city") or "",
                "state": org.get("state") or "",
                "type": org.get("type", "other"),
                "isPortal": org.get("is_portal", False),
                "isJunk": is_junk,
                "isInactive": is_inactive,
                "isLikelyAggregator": is_likely_aggregator,
                "portalSlug": org.get("portal_slug"),
                "cameras": org.get("cameras", 0),
                "searches": org.get("searches", 0),
                "vehiclesCaptured": org.get("vehicles_captured", 0),
                "connectionCount": cc,
                "population": pop,
                "hotlistHits": org.get("hotlist_hits", 0),
                "geocodeMethod": org.get("geocode_method", "unknown"),
                "aliases": org.get("aliases", []),
            },
        }
        features.append(feature)

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(geojson, f)

    size_kb = OUTPUT_FILE.stat().st_size / 1024
    portal_count = sum(1 for ft in features if ft["properties"]["isPortal"])
    print(f"  Features:   {len(features):,}")
    print(f"  Portals:    {portal_count:,}")
    print(f"  Junk:       {junk_count:,}")
    print(f"  Inactive:   {inactive_count:,}")
    print(f"  Aggregator-flagged: {aggregator_count:,}  (>= {AGGREGATOR_MIN_DEGREE} edges, pop < {AGGREGATOR_MAX_POP:,})")
    print(f"  Saved to {OUTPUT_FILE.name} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
