#!/usr/bin/env python3
"""Parse all unique organization names from EyesOnFlock ALPR transparency data.

Reads:  <work>/raw/eyesonflock_full_data.json
Writes: <work>/intermediate/parsed_orgs.json     (see paths.py)

For each unique org name found in portal sharing lists, this script parses
structured fields (city, state, type) and assigns a canonical slug. Names
that refer to the same agency in different forms (e.g., "Allen Park MI PD"
and "Allen Park PD MI") resolve to the same slug and are merged into a
single entry with an `aliases` list.

Portal metadata (cameras, searches, hotlist hits, etc.) is attached to the
matching canonical slug. Portals that don't appear in any sharing list are
added as standalone nodes.

Output: dict keyed by canonical slug, with each entry containing:
  raw_name, id, city, state, type, is_portal, is_junk, aliases,
  cameras, searches, vehicles_captured, population, hotlist_hits, data_retention,
  portal_slug
"""

import json

from parse_orgs_lib import (
    canonical_slug,
    parse_org_name,
    portal_as_org_name,
    portal_canonical_slug,
)
from junk_filter import is_junk
from paths import PARSED_ORGS_FILE, SNAPSHOT_FILE

INPUT_FILE = SNAPSHOT_FILE
OUTPUT_FILE = PARSED_ORGS_FILE


# Manual aliases for ambiguous raw sharing-list names whose canonical slug
# can't be inferred by the parser alone (no state code, no type suffix,
# too generic for fuzzy matching). Each entry maps a raw-name string to
# the Flock portal slug it should merge into.
MANUAL_ALIASES: dict[str, str] = {
    # One-word "Berkeley" in sharing lists refers to Berkeley CA PD (52 cams);
    # 127 portals cite this bare form. Confirmed with project owner.
    "Berkeley": "berkeley-ca-pd",
}


def _portal_meta(portal: dict) -> dict:
    """Extract numeric portal metadata fields plus the source portal slug."""
    return {
        "cameras": portal.get("total_cameras", 0) or 0,
        "searches": portal.get("total_searches", 0) or 0,
        "vehicles_captured": portal.get("vehicles_captured", 0) or 0,
        "population": portal.get("population", 0) or 0,
        "hotlist_hits": portal.get("hotlist_hits", 0) or 0,
        "data_retention": portal.get("data_retention"),
        "portal_slug": portal.get("slug"),
    }


def _empty_meta() -> dict:
    """Return zeroed-out metadata for orgs without a portal."""
    return {
        "cameras": 0,
        "searches": 0,
        "vehicles_captured": 0,
        "population": 0,
        "hotlist_hits": 0,
        "data_retention": None,
        "portal_slug": None,
    }


def _score_name_cleanliness(name: str) -> tuple:
    """Score a raw name — lower = cleaner. Used to pick the canonical raw_name
    when multiple aliases collapse to the same slug.

    Prefers names that are:
    - shorter (no trailing junk)
    - tag-free (no brackets/parens/DNU/inactive)
    - simpler punctuation
    """
    low = name.lower()
    has_bracket = "[" in name or "]" in name
    has_paren = "(" in name or ")" in name
    has_status = any(
        w in low for w in ("inactive", "dnu", "do not use", "duplicate", "dead", "raven", "insight", "flex")
    )
    return (has_status, has_bracket, has_paren, len(name), name)


def _merge_entry(parsed: dict, slug: str, raw_name: str, info: dict) -> None:
    """Add or merge a parsed name into the parsed dict, keyed by canonical slug."""
    if slug not in parsed:
        parsed[slug] = {
            "raw_name": raw_name,
            "id": slug,
            "city": info.get("city"),
            "state": info.get("state"),
            "type": info.get("type", "other"),
            "is_portal": False,
            "is_junk": is_junk(raw_name),
            "aliases": [],
            **_empty_meta(),
        }
        return

    entry = parsed[slug]
    # Already present — record alias and promote to cleaner raw_name if this one wins
    if raw_name != entry["raw_name"] and raw_name not in entry["aliases"]:
        entry["aliases"].append(raw_name)
    if _score_name_cleanliness(raw_name) < _score_name_cleanliness(entry["raw_name"]):
        # Swap: the incoming name is cleaner
        if entry["raw_name"] not in entry["aliases"]:
            entry["aliases"].append(entry["raw_name"])
        entry["raw_name"] = raw_name
        # Re-parse structured fields from the cleaner name
        entry["city"] = info.get("city") or entry["city"]
        entry["state"] = info.get("state") or entry["state"]
        entry["type"] = info.get("type", entry["type"])
    # An entry is junk only if EVERY known form is junk
    if not is_junk(raw_name):
        entry["is_junk"] = False


def main():
    print("=== Parse Organization Names ===")

    # Load input data
    print(f"  Loading {INPUT_FILE.name}...")
    with open(INPUT_FILE) as f:
        data = json.load(f)

    portals = data["portals"]
    print(f"  {len(portals):,} portals loaded")

    # Step 1: Collect all unique org names from sharing lists
    all_org_names: set[str] = set()
    for portal in portals:
        for org in portal.get("organizations_shared_with") or []:
            all_org_names.add(org)

    print(f"  {len(all_org_names):,} unique org names found in sharing lists")

    # Step 2: Parse each org name and merge into canonical slugs
    parsed: dict[str, dict] = {}
    # Pre-build portal lookup so MANUAL_ALIASES can seed entries with the
    # portal's canonical name (more informative than the bare alias text).
    portal_by_slug = {portal_canonical_slug(p): p for p in portals}
    for raw_name in sorted(all_org_names):
        info = parse_org_name(raw_name)
        manual_slug = MANUAL_ALIASES.get(raw_name)
        if manual_slug and manual_slug in portal_by_slug:
            # Seed with portal data; record the manual alias as an alias, not raw_name.
            portal = portal_by_slug[manual_slug]
            portal_name = portal_as_org_name(portal) or raw_name
            pinfo = parse_org_name(portal_name)
            _merge_entry(parsed, manual_slug, portal_name, pinfo)
            if raw_name != portal_name and raw_name not in parsed[manual_slug]["aliases"]:
                parsed[manual_slug]["aliases"].append(raw_name)
            continue
        slug = manual_slug or canonical_slug(info, raw_name)
        _merge_entry(parsed, slug, raw_name, info)

    # Step 2b: Rescue stateless SO/PD entries by matching to a portal on
    # (type, normalized city). Some sharing-list names like
    # "Yuba County Sheriffs Office" carry neither "CA" nor "SO", so the
    # parser can infer type and city but not state. If exactly one Flock
    # portal matches on (type, city), adopt its state and reslug. This
    # collapses the ghost node into the real portal.
    stateless = [
        (slug, e) for slug, e in parsed.items()
        if not e["state"] and e["type"] in ("so", "pd") and e["city"]
    ]
    portals_by_type_city: dict[tuple[str, str], list[dict]] = {}
    for portal in portals:
        name = portal_as_org_name(portal)
        if not name:
            continue
        pinfo = parse_org_name(name)
        ptype = (pinfo.get("type") or "").lower()
        pcity = (pinfo.get("city") or "").lower().strip()
        if ptype in ("so", "pd") and pcity:
            portals_by_type_city.setdefault((ptype, pcity), []).append(portal)

    rescued = 0
    for old_slug, entry in stateless:
        key = (entry["type"], entry["city"].lower().strip())
        candidates = portals_by_type_city.get(key, [])
        if len(candidates) != 1:
            continue
        portal = candidates[0]
        new_slug = portal_canonical_slug(portal)
        if not new_slug or new_slug == old_slug:
            continue
        old = parsed.pop(old_slug)
        # Merge into the target, seeding it with the portal's canonical name
        # so the stateless spelled-out form is recorded as an alias rather
        # than becoming the node's display name.
        portal_name = portal_as_org_name(portal) or new_slug
        pinfo = parse_org_name(portal_name)
        if new_slug not in parsed:
            parsed[new_slug] = {
                "raw_name": portal_name,
                "id": new_slug,
                "city": pinfo.get("city") or old["city"],
                "state": pinfo.get("state") or (portal.get("state") or "").strip(),
                "type": pinfo.get("type") or old["type"],
                "is_portal": False,
                "is_junk": False,
                "aliases": [],
                **_empty_meta(),
            }
        target = parsed[new_slug]
        for raw in [old["raw_name"], *old["aliases"]]:
            if raw and raw != target["raw_name"] and raw not in target["aliases"]:
                target["aliases"].append(raw)
        if not old["is_junk"]:
            target["is_junk"] = False
        rescued += 1

    # Step 3: Match portals to parsed orgs and merge metadata
    portals_merged = 0
    portals_added = 0
    for portal in portals:
        p_slug = portal_canonical_slug(portal)
        if not p_slug:
            continue

        if p_slug in parsed:
            entry = parsed[p_slug]
            if not entry["is_portal"]:
                entry["is_portal"] = True
                entry.update(_portal_meta(portal))
                portals_merged += 1
            # Promote the portal's synthesized name to raw_name if it's
            # cleaner than what the sharing-list parse produced
            # (e.g., "Kings County CA SO" beats "Kings County Sheriff's
            # Office CA"). Demote the old raw_name to aliases.
            portal_name = portal_as_org_name(portal)
            if (
                portal_name
                and portal_name != entry["raw_name"]
                and _score_name_cleanliness(portal_name) < _score_name_cleanliness(entry["raw_name"])
            ):
                if entry["raw_name"] not in entry["aliases"]:
                    entry["aliases"].append(entry["raw_name"])
                entry["raw_name"] = portal_name
            # A real portal with a clean synthesized name un-flags junk. This
            # rescues agencies whose only sharing-list references happened to
            # carry a "(duplicate)" / "(do not use)" admin tag.
            if portal_name and not is_junk(portal_name):
                entry["is_junk"] = False
            continue

        # Portal has no matching org in any sharing list — add as standalone node
        canonical_name = portal_as_org_name(portal) or portal.get("slug") or ""
        info = parse_org_name(canonical_name)
        parsed[p_slug] = {
            "raw_name": canonical_name,
            "id": p_slug,
            "city": info.get("city"),
            "state": info.get("state"),
            "type": info.get("type", "other"),
            "is_portal": True,
            "is_junk": False,
            "aliases": [],
            **_portal_meta(portal),
        }
        portals_added += 1

    # Step 4: Write output
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(parsed, f, indent=2)

    # Stats
    total = len(parsed)
    with_state = sum(1 for e in parsed.values() if e["state"])
    portal_count = sum(1 for e in parsed.values() if e["is_portal"])
    junk_count = sum(1 for e in parsed.values() if e["is_junk"])
    alias_count = sum(len(e["aliases"]) for e in parsed.values())
    type_counts: dict[str, int] = {}
    for e in parsed.values():
        t = e["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"\n  Output: {OUTPUT_FILE.name}")
    print(f"  Raw unique names:  {len(all_org_names):,}")
    print(f"  Canonical orgs:    {total:,}")
    print(f"  Collapsed aliases: {alias_count:,}  ({alias_count} raw names merged into existing slugs)")
    print(f"  With state:        {with_state:,} ({100*with_state/total:.1f}%)")
    print(f"  Stateless rescued: {rescued:,}  (spelled-out Sheriff/Police names unified with portal)")
    print(f"  Portal matches:    {portals_merged:,}")
    print(f"  Portals added:     {portals_added:,}  (not in any sharing list)")
    print(f"  Total portals:     {portal_count:,}")
    print(f"  Junk-tagged:       {junk_count:,}")
    print(f"  By type:")
    for t in sorted(type_counts, key=type_counts.get, reverse=True):
        print(f"    {t:10s} {type_counts[t]:>5,}")
    print("Done.")


if __name__ == "__main__":
    main()
