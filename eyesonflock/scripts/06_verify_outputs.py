#!/usr/bin/env python3
"""Verify the two output files and write meta.json — the pipeline's fail-closed gate.

Reads:  <work>/output/sharing-network-nodes.geojson
        <work>/output/sharing-network-adjacency.json
        <work>/raw/eyesonflock_full_data.json      (optional; meta only)
Writes: <work>/output/meta.json                   (only when every check passes)

Exits 1 on any violation, so a scheduled run never publishes a broken build
and the workflow log says *why*. The checks are the contract the website's
networkStore.ts depends on (file shape, the frozen property-key set) plus the
cross-file consistency METHODOLOGY.md promises: every adjacency reference
resolves to a node, and connectionCount == |outbound ∪ inbound|.

Thresholds are floors well below the 2026-04 reference run (6,461 features,
906 portals, 272,290 directed edges). They catch a truncated or broken build,
not a modest real-world decline.
"""

import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from paths import ADJACENCY_FILE, META_FILE, NODES_FILE, SNAPSHOT_FILE

NODES = NODES_FILE
ADJACENCY = ADJACENCY_FILE
SNAPSHOT = SNAPSHOT_FILE
META = META_FILE

SOURCE_URL = "https://eyesonflock.com/api/v1/data"

MIN_FEATURES = 5_000
MIN_PORTALS = 700
MIN_DIRECTED_EDGES = 100_000
# Share of features geocoded to the Washington DC default — the parse-failure
# signal. Reference run: 0 of 6,461.
MAX_DEFAULT_SHARE = 0.02
# Per rule, list at most this many offending features before summarising, so a
# systemic break (e.g. every feature gaining a key) stays readable in CI logs.
MAX_LISTED_PER_RULE = 10

# The website contract. Adding or removing a key here is a breaking change for
# networkStore.ts — do it deliberately, in both places.
EXPECTED_PROPERTY_KEYS = frozenset({
    "id", "name", "city", "state", "type",
    "isPortal", "isJunk", "isInactive", "isLikelyAggregator", "portalSlug",
    "cameras", "searches", "vehiclesCaptured", "connectionCount",
    "population", "hotlistHits", "geocodeMethod", "aliases",
})


class _Violations:
    """Collects violation messages, capping repeats of the same rule."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._per_rule: Counter = Counter()

    def add(self, rule: str, message: str) -> None:
        self._per_rule[rule] += 1
        n = self._per_rule[rule]
        if n <= MAX_LISTED_PER_RULE:
            self.messages.append(message)
        elif n == MAX_LISTED_PER_RULE + 1:
            self.messages.append(f"... further '{rule}' violations not listed")

    def total(self) -> int:
        return sum(self._per_rule.values())


def _is_finite_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _check_feature(i: int, feat: object, v: _Violations) -> tuple[str | None, dict | None]:
    """Validate one feature. Returns (id, properties) — id is None when unusable."""
    props = feat.get("properties") if isinstance(feat, dict) else None
    if not isinstance(props, dict):
        v.add("shape", f"feature #{i}: missing properties object")
        return None, None

    fid = props.get("id")
    has_id = isinstance(fid, str) and bool(fid)
    label = fid if has_id else f"#{i}"
    if not has_id:
        v.add("id", f"feature #{i}: empty id")

    keys = set(props)
    if keys != EXPECTED_PROPERTY_KEYS:
        missing = sorted(EXPECTED_PROPERTY_KEYS - keys)
        extra = sorted(keys - EXPECTED_PROPERTY_KEYS)
        v.add("keys", f"feature {label}: property keys differ from schema (missing={missing}, extra={extra})")

    geom = feat.get("geometry")
    if not isinstance(geom, dict) or geom.get("type") != "Point":
        v.add("geom", f"feature {label}: geometry is not a Point")
    else:
        c = geom.get("coordinates")
        ok = (
            isinstance(c, list)
            and len(c) == 2
            and all(_is_finite_number(x) for x in c)
            and -180 <= c[0] <= 180
            and -90 <= c[1] <= 90
        )
        if not ok:
            v.add("coords", f"feature {label}: bad coordinates {c!r}")

    is_portal = props.get("isPortal") is True
    slug = props.get("portalSlug")
    if is_portal and not (isinstance(slug, str) and slug):
        v.add("slug", f"feature {label}: isPortal but portalSlug is {slug!r}")
    if not is_portal and slug is not None:
        v.add("slug", f"feature {label}: not a portal but portalSlug is {slug!r}")

    return (fid if has_id else None), props


def verify(nodes: object, adjacency: object) -> list[str]:
    """Return every invariant violation found (empty list = pass). Pure."""
    v = _Violations()

    # ── nodes.geojson ─────────────────────────────────────────────────────
    if not isinstance(nodes, dict) or nodes.get("type") != "FeatureCollection":
        v.add("fc", "nodes.geojson is not a FeatureCollection")
        return v.messages
    features = nodes.get("features")
    if not isinstance(features, list):
        v.add("fc", "nodes.geojson has no features list")
        return v.messages
    if len(features) < MIN_FEATURES:
        v.add("count", f"fewer than {MIN_FEATURES:,} features ({len(features):,})")

    connection_counts: dict[str, object] = {}  # id → declared connectionCount
    portal_count = 0
    method_counts: Counter = Counter()
    for i, feat in enumerate(features):
        fid, props = _check_feature(i, feat, v)
        if props is None:
            continue
        if props.get("isPortal") is True:
            portal_count += 1
        method_counts[props.get("geocodeMethod")] += 1
        if fid is not None:
            if fid in connection_counts:
                v.add("dup", f"duplicate id: {fid}")
            else:
                connection_counts[fid] = props.get("connectionCount")

    if portal_count < MIN_PORTALS:
        v.add("portals", f"fewer than {MIN_PORTALS:,} portals ({portal_count:,})")
    if features:
        share = method_counts.get("default", 0) / len(features)
        if share > MAX_DEFAULT_SHARE:
            v.add("default", f"default (DC fallback) geocode share {share:.1%} exceeds {MAX_DEFAULT_SHARE:.0%} — parse failures")

    # ── adjacency.json ────────────────────────────────────────────────────
    if not isinstance(adjacency, dict):
        v.add("adj", "adjacency.json is not an object")
        return v.messages

    neighbours: dict[str, set[str]] = defaultdict(set)
    directed = 0
    for src, targets in adjacency.items():
        if src not in connection_counts:
            v.add("unknown", f"adjacency references unknown node id: {src}")
        if not isinstance(targets, list):
            v.add("list", f"adjacency[{src}] is not a list")
            continue
        if targets != sorted(set(targets)):
            v.add("sorted", f"adjacency[{src}] is not sorted and deduplicated")
        if src in targets:
            v.add("self", f"adjacency[{src}] contains a self-edge")
        for t in targets:
            if t not in connection_counts:
                v.add("unknown", f"adjacency references unknown node id: {t}")
        directed += len(targets)
        tset = set(targets)
        tset.discard(src)
        neighbours[src].update(tset)
        for t in tset:
            neighbours[t].add(src)

    if directed < MIN_DIRECTED_EDGES:
        v.add("edges", f"fewer than {MIN_DIRECTED_EDGES:,} directed edges ({directed:,})")

    for fid, declared in connection_counts.items():
        expected = len(neighbours.get(fid, ()))
        if declared != expected:
            v.add("cc", f"feature {fid}: connectionCount {declared} != {expected} (outbound ∪ inbound)")

    return v.messages


def build_meta(nodes: dict, adjacency: dict, snapshot: dict | None, run_id: str | None) -> dict:
    """Summary object for the run — consumed by the workflow summary and, in
    phase 2, by the R2 uploader for object metadata."""
    props = [f["properties"] for f in nodes["features"]]
    return {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE_URL,
        "snapshotPortals": len(snapshot.get("portals", [])) if isinstance(snapshot, dict) else None,
        "featureCount": len(props),
        "portalCount": sum(1 for p in props if p.get("isPortal") is True),
        "adjacencyKeys": len(adjacency),
        "directedEdges": sum(len(v) for v in adjacency.values()),
        "geocodeMethods": dict(sorted(Counter(p.get("geocodeMethod") for p in props).items())),
        "junk": sum(1 for p in props if p.get("isJunk") is True),
        "inactive": sum(1 for p in props if p.get("isInactive") is True),
        "likelyAggregators": sum(1 for p in props if p.get("isLikelyAggregator") is True),
        "runId": run_id,
    }


def _load(path: Path) -> object:
    with open(path) as f:
        return json.load(f)


def main() -> int:
    print("=== Verify Outputs ===")
    try:
        nodes = _load(NODES)
        adjacency = _load(ADJACENCY)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ERROR: cannot read outputs: {e}")
        return 1

    snapshot = None
    if SNAPSHOT.exists():
        try:
            snapshot = _load(SNAPSHOT)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARNING: cannot read snapshot for meta: {e}")

    violations = verify(nodes, adjacency)
    if violations:
        print(f"  FAIL: {len(violations)} violation(s):")
        for m in violations:
            print(f"    - {m}")
        return 1

    meta = build_meta(nodes, adjacency, snapshot, os.environ.get("GITHUB_RUN_ID") or None)
    META.parent.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(meta, indent=2) + "\n")

    print(f"  Features:        {meta['featureCount']:,}  (portals {meta['portalCount']:,})")
    print(f"  Adjacency keys:  {meta['adjacencyKeys']:,}  directed edges {meta['directedEdges']:,}")
    print(f"  Geocode methods: {meta['geocodeMethods']}")
    print(f"  Flags:           junk {meta['junk']}, inactive {meta['inactive']}, aggregators {meta['likelyAggregators']}")
    print(f"  All invariants hold. Wrote {META}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
