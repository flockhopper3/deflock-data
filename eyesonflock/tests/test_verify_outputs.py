#!/usr/bin/env python3
"""Tests for 06_verify_outputs — the fail-closed gate on the two output files.

Every invariant gets a fixture that breaks exactly that invariant, and the
happy path must produce zero violations and a meta.json.
"""

import importlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

_verify = importlib.import_module("06_verify_outputs")

KEYS = _verify.EXPECTED_PROPERTY_KEYS


def _feature(i: int, *, is_portal: bool, connection_count: int, method: str = "place") -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [-97.0 + (i % 100) * 0.01, 30.0 + (i // 100) * 0.01],
        },
        "properties": {
            "id": f"org-{i}-tx-pd",
            "name": f"Org {i} TX PD",
            "city": f"Org {i}",
            "state": "TX",
            "type": "pd",
            "isPortal": is_portal,
            "isJunk": False,
            "isInactive": False,
            "isLikelyAggregator": False,
            "portalSlug": f"org-{i}-tx" if is_portal else None,
            "cameras": 1,
            "searches": 2,
            "vehiclesCaptured": 3,
            "connectionCount": connection_count,
            "population": 10_000,
            "hotlistHits": 0,
            "geocodeMethod": method,
            "aliases": [],
        },
    }


def _good(
    n_features: int = _verify.MIN_FEATURES,
    n_portals: int = _verify.MIN_PORTALS,
    edges_target: int = _verify.MIN_DIRECTED_EDGES,
) -> tuple[dict, dict]:
    """A synthetic nodes+adjacency pair that satisfies every invariant."""
    ids = [f"org-{i}-tx-pd" for i in range(n_features)]
    per_portal = math.ceil(edges_target / n_portals)
    adjacency: dict[str, list[str]] = {}
    for i in range(n_portals):
        targets = {ids[(i + 1 + j) % n_features] for j in range(per_portal)}
        targets.discard(ids[i])
        adjacency[ids[i]] = sorted(targets)

    neighbours: dict[str, set[str]] = defaultdict(set)
    for src, targets in adjacency.items():
        neighbours[src].update(targets)
        for t in targets:
            neighbours[t].add(src)

    features = [
        _feature(i, is_portal=i < n_portals, connection_count=len(neighbours[ids[i]]))
        for i in range(n_features)
    ]
    return {"type": "FeatureCollection", "features": features}, adjacency


def _snapshot(n: int = 908) -> dict:
    return {"summary": {}, "portals": [{"slug": f"p{i}"} for i in range(n)]}


def _violations(nodes, adjacency) -> list[str]:
    return _verify.verify(nodes, adjacency)


def _has(msgs: list[str], *needles: str) -> bool:
    return any(all(n in m for n in needles) for m in msgs)


# ── Happy path ───────────────────────────────────────────────────────────────

def test_good_fixture_has_no_violations():
    nodes, adjacency = _good()
    assert _violations(nodes, adjacency) == []


def test_thresholds_sit_below_reference_run():
    # 2026-04 reference: 6,461 features / 906 portals / 272,290 edges.
    assert 3000 <= _verify.MIN_FEATURES < 6461
    assert 300 <= _verify.MIN_PORTALS < 906
    assert 50_000 <= _verify.MIN_DIRECTED_EDGES < 272_290
    assert 0 < _verify.MAX_DEFAULT_SHARE <= 0.05
    assert len(KEYS) == 18


# ── nodes.geojson invariants ─────────────────────────────────────────────────

def test_rejects_non_feature_collection():
    nodes, adjacency = _good()
    nodes["type"] = "Feature"
    assert _has(_violations(nodes, adjacency), "FeatureCollection")


def test_rejects_too_few_features():
    nodes, adjacency = _good()
    nodes["features"] = nodes["features"][: _verify.MIN_FEATURES - 1]
    assert _has(_violations(nodes, adjacency), "fewer than", "features")


def test_rejects_non_point_geometry():
    nodes, adjacency = _good()
    nodes["features"][0]["geometry"]["type"] = "LineString"
    assert _has(_violations(nodes, adjacency), "org-0-tx-pd", "Point")


@pytest.mark.parametrize("coords", [[200.0, 30.0], [-97.0, 95.0], ["-97", "30"], [-97.0], [float("nan"), 30.0]])
def test_rejects_bad_coordinates(coords):
    nodes, adjacency = _good()
    nodes["features"][0]["geometry"]["coordinates"] = coords
    assert _has(_violations(nodes, adjacency), "org-0-tx-pd", "coordinates")


def test_rejects_extra_property_key():
    nodes, adjacency = _good()
    nodes["features"][0]["properties"]["isHub"] = True
    assert _has(_violations(nodes, adjacency), "org-0-tx-pd", "property keys", "isHub")


def test_rejects_missing_property_key():
    nodes, adjacency = _good()
    del nodes["features"][0]["properties"]["portalSlug"]
    assert _has(_violations(nodes, adjacency), "org-0-tx-pd", "property keys", "portalSlug")


def test_rejects_duplicate_id():
    nodes, adjacency = _good()
    nodes["features"][1]["properties"]["id"] = "org-0-tx-pd"
    assert _has(_violations(nodes, adjacency), "duplicate id", "org-0-tx-pd")


def test_rejects_empty_id():
    nodes, adjacency = _good()
    nodes["features"][0]["properties"]["id"] = ""
    assert _has(_violations(nodes, adjacency), "empty id")


def test_rejects_portal_without_slug():
    nodes, adjacency = _good()
    nodes["features"][0]["properties"]["portalSlug"] = None  # feature 0 is a portal
    assert _has(_violations(nodes, adjacency), "org-0-tx-pd", "portalSlug")


def test_rejects_non_portal_with_slug():
    nodes, adjacency = _good()
    last = nodes["features"][-1]["properties"]  # last feature is not a portal
    assert last["isPortal"] is False
    last["portalSlug"] = "ghost"
    assert _has(_violations(nodes, adjacency), last["id"], "portalSlug")


def test_rejects_too_few_portals():
    nodes, adjacency = _good()
    # Demote one portal; the adjacency still references it so no other rule trips.
    props = nodes["features"][0]["properties"]
    props["isPortal"] = False
    props["portalSlug"] = None
    assert _has(_violations(nodes, adjacency), "fewer than", "portals")


def test_rejects_default_geocode_share_above_limit():
    nodes, adjacency = _good()
    limit = int(_verify.MAX_DEFAULT_SHARE * len(nodes["features"])) + 1
    for f in nodes["features"][:limit]:
        f["properties"]["geocodeMethod"] = "default"
    assert _has(_violations(nodes, adjacency), "default", "share")


def test_accepts_default_geocode_share_at_limit():
    nodes, adjacency = _good()
    limit = int(_verify.MAX_DEFAULT_SHARE * len(nodes["features"]))
    for f in nodes["features"][:limit]:
        f["properties"]["geocodeMethod"] = "default"
    assert not _has(_violations(nodes, adjacency), "default", "share")


# ── adjacency.json invariants ────────────────────────────────────────────────

def test_rejects_non_object_adjacency():
    nodes, _ = _good()
    assert _has(_violations(nodes, []), "adjacency.json is not an object")


def test_rejects_unknown_key():
    nodes, adjacency = _good()
    adjacency["nobody-tx-pd"] = []
    assert _has(_violations(nodes, adjacency), "unknown node", "nobody-tx-pd")


def test_rejects_unknown_target():
    nodes, adjacency = _good()
    adjacency["org-0-tx-pd"].append("zz-nobody-tx-pd")  # still sorted (z sorts last)
    assert _has(_violations(nodes, adjacency), "unknown node", "zz-nobody-tx-pd")


def test_rejects_unsorted_list():
    nodes, adjacency = _good()
    lst = adjacency["org-0-tx-pd"]
    lst[0], lst[1] = lst[1], lst[0]
    assert _has(_violations(nodes, adjacency), "org-0-tx-pd", "sorted")


def test_rejects_duplicate_target():
    nodes, adjacency = _good()
    lst = adjacency["org-0-tx-pd"]
    lst.insert(1, lst[0])
    assert _has(_violations(nodes, adjacency), "org-0-tx-pd", "sorted")


def test_rejects_self_edge():
    nodes, adjacency = _good()
    adjacency["org-0-tx-pd"] = sorted(adjacency["org-0-tx-pd"] + ["org-0-tx-pd"])
    assert _has(_violations(nodes, adjacency), "org-0-tx-pd", "self-edge")


def test_rejects_too_few_directed_edges():
    nodes, adjacency = _good()
    for k in list(adjacency)[: len(adjacency) // 2]:
        adjacency[k] = []
    assert _has(_violations(nodes, adjacency), "fewer than", "directed edges")


def test_rejects_connection_count_mismatch():
    nodes, adjacency = _good()
    nodes["features"][5]["properties"]["connectionCount"] += 1
    assert _has(_violations(nodes, adjacency), "org-5-tx-pd", "connectionCount")


# ── meta.json ────────────────────────────────────────────────────────────────

def test_build_meta_counts():
    nodes, adjacency = _good()
    nodes["features"][0]["properties"]["isJunk"] = True
    nodes["features"][1]["properties"]["isInactive"] = True
    nodes["features"][2]["properties"]["isLikelyAggregator"] = True
    meta = _verify.build_meta(nodes, adjacency, _snapshot(908), run_id="123")
    assert meta["featureCount"] == _verify.MIN_FEATURES
    assert meta["portalCount"] == _verify.MIN_PORTALS
    assert meta["adjacencyKeys"] == len(adjacency)
    assert meta["directedEdges"] == sum(len(v) for v in adjacency.values())
    assert meta["snapshotPortals"] == 908
    assert meta["geocodeMethods"] == {"place": _verify.MIN_FEATURES}
    assert meta["junk"] == 1 and meta["inactive"] == 1 and meta["likelyAggregators"] == 1
    assert meta["runId"] == "123"
    assert meta["source"] == "https://eyesonflock.com/api/v1/data"
    assert meta["generatedAt"].endswith("Z")


def test_build_meta_without_snapshot_or_run_id():
    nodes, adjacency = _good()
    meta = _verify.build_meta(nodes, adjacency, None, run_id=None)
    assert meta["snapshotPortals"] is None
    assert meta["runId"] is None


# ── main() ───────────────────────────────────────────────────────────────────

def _point_at_tmp(monkeypatch, tmp_path, nodes, adjacency, snapshot=None):
    nodes_f = tmp_path / "nodes.geojson"
    adj_f = tmp_path / "adjacency.json"
    snap_f = tmp_path / "snapshot.json"
    meta_f = tmp_path / "out" / "meta.json"
    nodes_f.write_text(json.dumps(nodes))
    adj_f.write_text(json.dumps(adjacency))
    if snapshot is not None:
        snap_f.write_text(json.dumps(snapshot))
    monkeypatch.setattr(_verify, "NODES", nodes_f)
    monkeypatch.setattr(_verify, "ADJACENCY", adj_f)
    monkeypatch.setattr(_verify, "SNAPSHOT", snap_f)
    monkeypatch.setattr(_verify, "META", meta_f)
    return meta_f


def test_main_writes_meta_on_success(monkeypatch, tmp_path):
    nodes, adjacency = _good()
    meta_f = _point_at_tmp(monkeypatch, tmp_path, nodes, adjacency, _snapshot(908))
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert _verify.main() == 0
    meta = json.loads(meta_f.read_text())
    assert meta["featureCount"] == _verify.MIN_FEATURES
    assert meta["snapshotPortals"] == 908
    assert meta["runId"] == "42"


def test_main_returns_1_and_writes_no_meta_on_violation(monkeypatch, tmp_path):
    nodes, adjacency = _good()
    nodes["features"][0]["properties"]["isHub"] = True
    meta_f = _point_at_tmp(monkeypatch, tmp_path, nodes, adjacency)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert _verify.main() == 1
    assert not meta_f.exists()


def test_main_returns_1_when_output_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(_verify, "NODES", tmp_path / "missing.geojson")
    monkeypatch.setattr(_verify, "ADJACENCY", tmp_path / "missing.json")
    monkeypatch.setattr(_verify, "SNAPSHOT", tmp_path / "missing-snap.json")
    monkeypatch.setattr(_verify, "META", tmp_path / "meta.json")
    assert _verify.main() == 1
