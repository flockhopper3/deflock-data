#!/usr/bin/env python3
"""Build directional adjacency list JSON for sharing network arcs.

Reads:  <work>/raw/eyesonflock_full_data.json
        <work>/intermediate/parsed_orgs.json
Writes: <work>/output/sharing-network-adjacency.json   (see paths.py)

Emits OUTBOUND-ONLY edges: adj[A] = [B, C] means "A shares outbound data to
B and C" and only that. The reverse edge (B shares to A) appears in B's own
adjacency list — and only if B's portal actually declares it. The web app
builds the inbound index itself at load time.

Each portal's `organizations_shared_with` is treated as that portal's
outbound sharing list. Non-portal agencies (agencies that appear only as
values in some other portal's shared list) have no outbound information —
they don't appear as keys in this file.

Slugs are computed via the canonical slugger in parse_orgs_lib so that
edges always terminate at the same canonical node as 03_build_nodes_geojson.py
produces (IDs match properties.id in sharing-network-nodes.geojson).
"""

import json

from parse_orgs_lib import canonical_slug, parse_org_name, portal_canonical_slug
from paths import ADJACENCY_FILE, PARSED_ORGS_FILE, SNAPSHOT_FILE

EYESONFLOCK = SNAPSHOT_FILE
PARSED_ORGS = PARSED_ORGS_FILE
OUTPUT_FILE = ADJACENCY_FILE


def _load_alias_map() -> dict[str, str]:
    """Build raw_name → canonical_slug from parsed_orgs.json.

    parsed_orgs.json holds the authoritative slug for every known raw name,
    including rescued stateless entries (e.g., "Yuba County Sheriffs Office"
    → "yuba-county-ca-so") that pure canonical_slug() can't resolve on its
    own. This map ensures adjacency terminates at the same node 03 emits.
    """
    if not PARSED_ORGS.exists():
        return {}
    with open(PARSED_ORGS) as f:
        parsed = json.load(f)
    alias_map: dict[str, str] = {}
    for slug, entry in parsed.items():
        alias_map[entry["raw_name"]] = slug
        for alias in entry.get("aliases", []):
            alias_map[alias] = slug
    return alias_map


def _slug_for_shared_name(raw_name: str, alias_map: dict[str, str], _cache: dict = {}) -> str:
    """Canonical slug for a name that appears in organizations_shared_with.
    Consults the alias map first so rescued names resolve correctly, then
    falls back to canonical_slug. Memoized since many portals share with
    the same agencies."""
    slug = _cache.get(raw_name)
    if slug is None:
        slug = alias_map.get(raw_name) or canonical_slug(parse_org_name(raw_name), raw_name)
        _cache[raw_name] = slug
    return slug


def main():
    print("=== Build Adjacency JSON (directional, outbound-only) ===")

    with open(EYESONFLOCK) as f:
        data = json.load(f)

    alias_map = _load_alias_map()

    adjacency: dict[str, set[str]] = {}

    for portal in data["portals"]:
        p_slug = portal_canonical_slug(portal)
        shared = portal.get("organizations_shared_with") or []

        if not shared or not p_slug:
            continue

        connected_slugs = {_slug_for_shared_name(name, alias_map) for name in shared}
        # Drop self-edges and empty slugs
        connected_slugs.discard(p_slug)
        connected_slugs.discard("")

        # Outbound only: portal → each org in its sharing list.
        # The reverse direction (B → A) is NOT added here; it will only appear
        # if portal B independently declares A in its own sharing list.
        adjacency.setdefault(p_slug, set()).update(connected_slugs)

    # Convert sets to sorted lists for JSON serialization
    adj_json = {k: sorted(v) for k, v in adjacency.items()}

    total_directed = sum(len(v) for v in adj_json.values())
    max_out = max((len(v) for v in adj_json.values()), default=0)

    # Count mutual vs one-way for visibility; the validation script in the
    # repo-level README does the same in Node.
    mutual_directed = 0
    asymmetric = 0
    mutual_pairs: set[tuple[str, str]] = set()
    for src, tgts in adj_json.items():
        for tgt in tgts:
            if tgt in adj_json and src in adj_json[tgt]:
                mutual_directed += 1
                pair = tuple(sorted((src, tgt)))
                mutual_pairs.add(pair)
            else:
                asymmetric += 1

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(adj_json, f)

    size_kb = OUTPUT_FILE.stat().st_size / 1024
    print(f"  Keys (portals with outbound sharing): {len(adj_json):,}")
    print(f"  Total directed edges:                 {total_directed:,}")
    print(f"  Mutual (both directions present):     {mutual_directed:,}  ({len(mutual_pairs):,} pairs)")
    print(f"  Asymmetric (one-way):                 {asymmetric:,}")
    pct = (100.0 * asymmetric / total_directed) if total_directed else 0.0
    print(f"  Asymmetric share:                     {pct:.1f}%")
    print(f"  Max outbound degree (single node):    {max_out:,}")
    print(f"  Saved to {OUTPUT_FILE.name} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
