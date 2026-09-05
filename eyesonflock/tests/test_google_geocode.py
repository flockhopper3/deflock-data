#!/usr/bin/env python3
"""Tests for google_geocode.GoogleGeocoder — cache behaviour, no live API."""

import json
import sys
import urllib.error
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


# ── API failure handling ───────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _serve(monkeypatch, payloads: list[dict]) -> list[str]:
    """urlopen returns the next canned payload each call; returns the call log."""
    calls: list[str] = []
    it = iter(payloads)
    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(next(it))
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


OK = {"status": "OK", "results": [{"geometry": {"location": {"lat": 42.19, "lng": -71.83}}}]}
ZERO = {"status": "ZERO_RESULTS", "results": []}
DENIED = {"status": "REQUEST_DENIED", "error_message": "This API key is not authorized to use this service or API.", "results": []}
OVER = {"status": "OVER_QUERY_LIMIT", "results": []}


class TestApiFailureHandling:
    def test_ok_result_is_cached(self, tmp_path, monkeypatch):
        _serve(monkeypatch, [OK])
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        assert g.geocode_org(_org(city="Auburn", state="MA")) == (42.19, -71.83)
        assert g.cache_size == 1 and g.api_calls_made == 1 and g.last_error is None

    def test_zero_results_is_cached_as_negative(self, tmp_path, monkeypatch):
        calls = _serve(monkeypatch, [ZERO])
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        org = _org(city="Nowhere", state="TX")
        assert g.geocode_org(org) is None
        assert g.geocode_org(org) is None          # served from cache
        assert len(calls) == 1 and g.cache_size == 1 and g.last_error is None

    def test_request_denied_is_not_cached_and_opens_circuit(self, tmp_path, monkeypatch):
        calls = _serve(monkeypatch, [DENIED, OK, OK])
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        assert g.geocode_org(_org(city="Auburn", state="MA")) is None
        assert g.cache_size == 0                    # refusal must never poison the cache
        assert g.last_error == ("REQUEST_DENIED", "This API key is not authorized to use this service or API.")
        assert g.is_live is False                   # circuit open: no more paid calls this run
        assert g.geocode_org(_org(city="Bellmawr", state="NJ")) is None
        assert len(calls) == 1 and g.api_calls_made == 1
        assert g.rejected_calls == 1

    def test_over_query_limit_opens_circuit(self, tmp_path, monkeypatch):
        calls = _serve(monkeypatch, [OVER, OK])
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        assert g.geocode_org(_org(city="Auburn", state="MA")) is None
        assert g.geocode_org(_org(city="Bellmawr", state="NJ")) is None
        assert len(calls) == 1 and g.cache_size == 0 and g.last_error[0] == "OVER_QUERY_LIMIT"

    def test_network_error_is_not_cached(self, tmp_path, monkeypatch):
        def boom(req, timeout=None): raise urllib.error.URLError("dns")
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        assert g.geocode_org(_org(city="Auburn", state="MA")) is None
        assert g.cache_size == 0 and g.api_calls_made == 0
        assert g.last_error[0] == "NETWORK_ERROR"

    def test_cache_hits_still_served_after_circuit_opens(self, tmp_path, monkeypatch):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"Allen Park, MI": {"lat": 42.25, "lng": -83.21}}))
        _serve(monkeypatch, [DENIED])
        g = GoogleGeocoder(api_key="k", cache_path=cache)
        g.geocode_org(_org(city="Auburn", state="MA"))   # opens the circuit
        assert g.geocode_org(_org()) == (42.25, -83.21)

    def test_three_consecutive_network_errors_open_circuit(self, tmp_path, monkeypatch):
        attempts: list[int] = []
        def boom(req, timeout=None):
            attempts.append(1); raise urllib.error.URLError("timeout")
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        for city in ("A", "B", "C", "D", "E"):
            assert g.geocode_org(_org(city=city, state="TX")) is None
        assert len(attempts) == 3            # 4th and 5th never touched the network
        assert g.is_live is False and g.cache_size == 0
