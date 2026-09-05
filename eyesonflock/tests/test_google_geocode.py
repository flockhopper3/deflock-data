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


# ── Places (New) text-search fallback ─────────────────────────────────────────

PLACES_HIT = {"places": [{"displayName": {"text": "Chesapeake Sheriff's Office"},
                          "formattedAddress": "401 Albemarle Dr, Chesapeake, VA 23322",
                          "location": {"latitude": 36.7177, "longitude": -76.2517}}]}
PLACES_EMPTY: dict = {}


class _HTTPErr(urllib.error.HTTPError):
    def __init__(self, code, payload):
        super().__init__("https://places.googleapis.com/v1/places:searchText", code, "err", {}, None)
        self._b = json.dumps(payload).encode()
    def read(self): return self._b


def _serve_places(monkeypatch, outcomes: list) -> list[dict]:
    """Each outcome is a payload dict (HTTP 200) or an _HTTPErr to raise. Returns request log."""
    log: list[dict] = []
    it = iter(outcomes)
    def fake_urlopen(req, timeout=None):
        log.append({"url": req.full_url, "body": json.loads(req.data) if req.data else None,
                    "mask": req.get_header("X-goog-fieldmask")})
        out = next(it)
        if isinstance(out, Exception): raise out
        return _Resp(out)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return log


class TestPlacesLookup:
    def test_hit_returns_coords_and_caches_under_places_key(self, tmp_path, monkeypatch):
        log = _serve_places(monkeypatch, [PLACES_HIT])
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        org = _org(type="so", city=None, state="VA", raw_name="Chesapeake Sheriff's Office")
        assert g.places_lookup(org) == (36.7177, -76.2517)
        assert log[0]["url"].startswith("https://places.googleapis.com/v1/places:searchText")
        assert log[0]["body"]["textQuery"] == "Chesapeake Sheriff's Office, VA"
        assert log[0]["body"]["regionCode"] == "US"
        assert "places.location" in log[0]["mask"]
        assert g.places_calls_made == 1
        assert g.places_lookup(org) == (36.7177, -76.2517)       # served from cache
        assert g.places_calls_made == 1
        g.save_cache()
        assert "places:Chesapeake Sheriff's Office, VA" in json.loads((tmp_path / "c.json").read_text())

    def test_query_without_state_is_raw_name(self, tmp_path, monkeypatch):
        log = _serve_places(monkeypatch, [PLACES_HIT])
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        g.places_lookup(_org(type="other", city=None, state=None, raw_name="Burr Ridge Village Center"))
        assert log[0]["body"]["textQuery"] == "Burr Ridge Village Center"

    def test_empty_result_is_cached_negative(self, tmp_path, monkeypatch):
        log = _serve_places(monkeypatch, [PLACES_EMPTY])
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        org = _org(type="other", city=None, state="TX", raw_name="Panhandle Auto Burglary and Theft Unit")
        assert g.places_lookup(org) is None
        assert g.places_lookup(org) is None
        assert len(log) == 1 and g.cache_size == 1

    def test_permission_denied_opens_places_circuit_only(self, tmp_path, monkeypatch):
        log = _serve_places(monkeypatch, [_HTTPErr(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                                                  "message": "Places API (New) has not been used in project"}}),
                                          OK])  # a later Geocoding call must still be allowed
        g = GoogleGeocoder(api_key="k", cache_path=tmp_path / "c.json")
        assert g.places_lookup(_org(type="other", city=None, state="IL", raw_name="X")) is None
        assert g.places_live is False and g.is_live is True
        assert g.last_error == ("PERMISSION_DENIED", "Places API (New) has not been used in project")
        assert g.cache_size == 0
        assert g.places_lookup(_org(type="other", city=None, state="IL", raw_name="Y")) is None
        assert g.geocode_org(_org(city="Auburn", state="MA")) == (42.19, -71.83)
        assert len(log) == 2

    def test_cache_only_mode_serves_places_hits(self, tmp_path):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"places:Foo, IL": {"lat": 1.5, "lng": 2.5}}))
        g = GoogleGeocoder(api_key=None, cache_path=cache)
        assert g.places_lookup(_org(type="other", city=None, state="IL", raw_name="Foo")) == (1.5, 2.5)
        assert g.places_lookup(_org(type="other", city=None, state="IL", raw_name="Bar")) is None
        assert g.places_calls_made == 0

    def test_invalidate_places_entry(self, tmp_path):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"places:Foo, IL": {"lat": 1.5, "lng": 2.5}, "Foo, IL": {"lat": 9, "lng": 9}}))
        g = GoogleGeocoder(api_key=None, cache_path=cache)
        org = _org(type="other", city=None, state="IL", raw_name="Foo")
        assert g.invalidate(org, source="places") is True
        assert g.cache_size == 1                          # geocode entry untouched
        assert g.invalidate(org, source="places") is False
