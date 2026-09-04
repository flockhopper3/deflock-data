#!/usr/bin/env python3
"""Tests for 03_build_nodes_geojson — feature-property emission."""

import importlib
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)

_build03 = importlib.import_module("03_build_nodes_geojson")


def _make_org(**overrides):
    base = {
        "raw_name": "Allen Park MI PD",
        "id": "allen-park-mi-pd",
        "city": "Allen Park",
        "state": "MI",
        "type": "pd",
        "is_portal": True,
        "is_junk": False,
        "aliases": [],
        "cameras": 0,
        "searches": 0,
        "vehicles_captured": 0,
        "population": 28000,
        "hotlist_hits": 0,
        "data_retention": None,
        "portal_slug": "allen-park-mi",
        "lat": 42.2573,
        "lng": -83.2102,
        "geocode_method": "place",
    }
    base.update(overrides)
    return base


def _run_build(monkeypatch, tmp_path, orgs: dict, portals: list, parsed: dict | None = None):
    """Helper: point the build script at temp files and run main().

    `parsed` is the parsed_orgs.json content (alias map source). When None the
    file is absent, which must degrade to bare canonical slugging.
    """
    geocoded = tmp_path / "geocoded_orgs.json"
    eyes = tmp_path / "eyesonflock.json"
    parsed_f = tmp_path / "parsed_orgs.json"
    out = tmp_path / "sharing-network-nodes.geojson"

    geocoded.write_text(json.dumps(orgs))
    eyes.write_text(json.dumps({"portals": portals}))
    if parsed is not None:
        parsed_f.write_text(json.dumps(parsed))

    monkeypatch.setattr(_build03, "GEOCODED_ORGS", geocoded)
    monkeypatch.setattr(_build03, "EYESONFLOCK", eyes)
    monkeypatch.setattr(_build03, "PARSED_ORGS", parsed_f)
    monkeypatch.setattr(_build03, "OUTPUT_FILE", out)

    _build03.main()
    return json.loads(out.read_text())


class TestPortalSlugOnFeature:
    def test_portal_feature_carries_portal_slug(self, monkeypatch, tmp_path):
        orgs = {"allen-park-mi-pd": _make_org()}
        portals = [{"slug": "allen-park-mi", "organizations_shared_with": []}]
        result = _run_build(monkeypatch, tmp_path, orgs, portals)
        assert len(result["features"]) == 1
        props = result["features"][0]["properties"]
        assert props["isPortal"] is True
        assert props["portalSlug"] == "allen-park-mi"

    def test_non_portal_feature_has_null_portal_slug(self, monkeypatch, tmp_path):
        orgs = {
            "somewhere-tx-pd": _make_org(
                raw_name="Somewhere TX PD",
                id="somewhere-tx-pd",
                city="Somewhere",
                state="TX",
                is_portal=False,
                portal_slug=None,
            )
        }
        result = _run_build(monkeypatch, tmp_path, orgs, [])
        props = result["features"][0]["properties"]
        assert props["isPortal"] is False
        assert props["portalSlug"] is None


class TestLikelyAggregatorFlag:
    def test_small_town_with_500_edges_flagged(self, monkeypatch, tmp_path):
        """>=500 edges AND pop<50k => isLikelyAggregator=True, isHub not emitted."""
        orgs = {}
        aggregator = _make_org(
            raw_name="Pittsboro IN PD",
            id="pittsboro-in-pd",
            city="Pittsboro",
            state="IN",
            population=3000,
        )
        orgs["pittsboro-in-pd"] = aggregator
        # Make 500 portals all share with Pittsboro so connection_count >= 500
        portals = [
            {
                "slug": f"p-{i}",
                "city": f"City{i}",
                "state": "IN",
                "organizations_shared_with": ["Pittsboro IN PD"],
            }
            for i in range(500)
        ]
        result = _run_build(monkeypatch, tmp_path, orgs, portals)
        feat = next(
            f for f in result["features"] if f["properties"]["id"] == "pittsboro-in-pd"
        )
        assert feat["properties"]["isLikelyAggregator"] is True
        assert "isHub" not in feat["properties"]

    def test_metropolitan_not_flagged(self, monkeypatch, tmp_path):
        """>=500 edges but pop>=50k => isLikelyAggregator=False."""
        orgs = {
            "shelby-county-tn-so": _make_org(
                raw_name="Shelby County TN SO",
                id="shelby-county-tn-so",
                city="Shelby",
                state="TN",
                type="so",
                population=938000,
            )
        }
        portals = [
            {
                "slug": f"p-{i}",
                "city": f"City{i}",
                "state": "TN",
                "organizations_shared_with": ["Shelby County TN SO"],
            }
            for i in range(500)
        ]
        result = _run_build(monkeypatch, tmp_path, orgs, portals)
        feat = next(
            f for f in result["features"] if f["properties"]["id"] == "shelby-county-tn-so"
        )
        assert feat["properties"]["isLikelyAggregator"] is False


class TestAliasAwareConnectionCount:
    """connectionCount must use the same name→slug resolution as step 04, so a
    sharing-list name that only resolves through parsed_orgs aliases (e.g. the
    bare "Berkeley" manual alias) is counted."""

    def _fixture(self):
        orgs = {
            "berkeley-ca-pd": _make_org(
                raw_name="Berkeley CA PD", id="berkeley-ca-pd",
                city="Berkeley", state="CA", portal_slug="berkeley-ca",
            )
        }
        portals = [
            {"slug": "berkeley-ca", "city": "Berkeley", "state": "CA", "type": "PD",
             "organizations_shared_with": []},
            {"slug": "other-ca", "city": "Other", "state": "CA", "type": "PD",
             "organizations_shared_with": ["Berkeley"]},
        ]
        parsed = {"berkeley-ca-pd": {"raw_name": "Berkeley CA PD", "aliases": ["Berkeley"]}}
        return orgs, portals, parsed

    def test_alias_counts_toward_portal(self, monkeypatch, tmp_path):
        orgs, portals, parsed = self._fixture()
        result = _run_build(monkeypatch, tmp_path, orgs, portals, parsed=parsed)
        feat = next(f for f in result["features"] if f["properties"]["id"] == "berkeley-ca-pd")
        assert feat["properties"]["connectionCount"] == 1

    def test_without_parsed_orgs_falls_back_to_bare_slugging(self, monkeypatch, tmp_path):
        orgs, portals, _ = self._fixture()
        result = _run_build(monkeypatch, tmp_path, orgs, portals, parsed=None)
        feat = next(f for f in result["features"] if f["properties"]["id"] == "berkeley-ca-pd")
        assert feat["properties"]["connectionCount"] == 0
