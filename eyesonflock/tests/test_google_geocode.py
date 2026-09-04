#!/usr/bin/env python3
"""Tests for google_geocode.GoogleGeocoder — cache behaviour, no live API."""

import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from google_geocode import GoogleGeocoder


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Every test in this module must run without touching the network."""
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("unexpected network call"),
    )


def _org(**overrides) -> dict:
    base = {"raw_name": "Allen Park MI PD", "type": "pd", "city": "Allen Park", "state": "MI"}
    base.update(overrides)
    return base


class TestCacheOnlyMode:
    def test_cache_hit_without_key(self, tmp_path):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"Allen Park, MI": {"lat": 42.25, "lng": -83.21}}))
        g = GoogleGeocoder(api_key=None, cache_path=cache)
        assert g.geocode_org(_org()) == (42.25, -83.21)
        assert g.api_calls_made == 0

    def test_cache_miss_without_key_returns_none(self, tmp_path):
        g = GoogleGeocoder(api_key=None, cache_path=tmp_path / "c.json")
        assert g.geocode_org(_org(city="Nowhere", state="TX")) is None
        assert g.api_calls_made == 0

    def test_miss_without_key_is_not_cached_as_negative(self, tmp_path):
        # A keyed run later must still be able to try this query.
        g = GoogleGeocoder(api_key=None, cache_path=tmp_path / "c.json")
        g.geocode_org(_org(city="Nowhere", state="TX"))
        assert g.cache_size == 0

    def test_cached_negative_result_still_honoured(self, tmp_path):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"Nowhere, TX": None}))
        g = GoogleGeocoder(api_key=None, cache_path=cache)
        assert g.geocode_org(_org(city="Nowhere", state="TX")) is None

    def test_invalidate_drops_entry(self, tmp_path):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"Allen Park, MI": {"lat": 1.0, "lng": 2.0}}))
        g = GoogleGeocoder(api_key=None, cache_path=cache)
        assert g.invalidate(_org()) is True
        assert g.cache_size == 0
        assert g.invalidate(_org()) is False

    def test_save_cache_round_trips(self, tmp_path):
        cache = tmp_path / "sub" / "c.json"
        g = GoogleGeocoder(api_key=None, cache_path=cache)
        g._cache["X, TX"] = {"lat": 1.0, "lng": 2.0}
        g.save_cache()
        assert json.loads(cache.read_text()) == {"X, TX": {"lat": 1.0, "lng": 2.0}}

    def test_is_live_property(self, tmp_path):
        assert GoogleGeocoder(api_key=None, cache_path=tmp_path / "c.json").is_live is False
        assert GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json").is_live is True


class TestQueryBuilding:
    def test_pd_query_is_city_state(self):
        g = GoogleGeocoder(api_key=None)
        assert g._build_query(_org()) == "Allen Park, MI"

    def test_so_query_appends_county(self):
        g = GoogleGeocoder(api_key=None)
        assert g._build_query(_org(type="so", city="Coconino")) == "Coconino County, AZ".replace("AZ", "MI")

    def test_other_falls_back_to_raw_name_and_state(self):
        g = GoogleGeocoder(api_key=None)
        assert g._build_query(_org(type="other", city=None, raw_name="ACRATT")) == "ACRATT, MI"
