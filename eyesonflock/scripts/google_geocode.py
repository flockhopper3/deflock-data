#!/usr/bin/env python3
"""Google Maps client for ALPR organization geocoding — Geocoding API plus a
Places (New) text-search fallback.

Two lookups, one on-disk cache:

* ``geocode_org(org)`` — Geocoding API. For PD/SO agencies the query is the
  parsed ``City, ST`` / ``X County, ST``; for everything else the raw name.
  Cheap; returns the jurisdiction's centroid.
* ``places_lookup(org)`` — Places API (New) ``places:searchText`` with the raw
  agency name. For names no jurisdiction can be parsed from ("Panhandle Auto
  Burglary and Theft Unit", "San Diego County District Attorney"). Returns the
  office itself when Google knows it. Cached under a ``places:`` key prefix.

Usage:
    client = GoogleGeocoder(api_key="...")
    lat, lng = client.geocode_org(org) or client.places_lookup(org)

Cache-only mode: pass ``api_key=None`` and the client serves cache hits but
never calls the network — misses return None and are *not* recorded, so a
later keyed run can still resolve them. This is how CI runs without a key
while keeping the ~1,400 precise geocodes the committed cache already holds.

Cache lives at eyesonflock/google_geocode_cache.json (see paths.GOOGLE_CACHE_FILE).
"""

import json
import time
import urllib.request
import urllib.parse
from pathlib import Path


# Rate limit: 50 QPS safety margin (Google allows 50 QPS on standard plan)
_MIN_REQUEST_INTERVAL = 0.02  # 20ms between requests

_GEOCODE_API_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_PLACES_API_URL = "https://places.googleapis.com/v1/places:searchText"
# The field mask decides the Places SKU; location + name + address is "Text Search Pro".
_PLACES_FIELD_MASK = "places.location,places.displayName,places.formattedAddress"
_PLACES_CACHE_PREFIX = "places:"
_REQUEST_TIMEOUT_S = 10

# Statuses that mean "this query has no answer" — safe to cache as a negative
# so the next run doesn't pay for it again.
_NEGATIVE_STATUSES = frozenset({"ZERO_RESULTS"})

# Statuses that mean the key, project, or quota is the problem. One of these
# opens that API's circuit: no further paid calls to it this run, cache hits
# still served, nothing cached. Caching these would poison the file — a later
# run with a fixed key would never retry the query. Each API has its own
# circuit because enablement is per API (Geocoding may work while Places is
# not enabled, or vice versa).
_CIRCUIT_BREAKER_STATUSES = frozenset({"REQUEST_DENIED", "OVER_DAILY_LIMIT", "OVER_QUERY_LIMIT"})
_PLACES_CIRCUIT_BREAKER_STATUSES = frozenset({"PERMISSION_DENIED", "UNAUTHENTICATED", "RESOURCE_EXHAUSTED"})
_PLACES_CIRCUIT_BREAKER_HTTP = frozenset({401, 403, 429})

# Consecutive network failures before the circuit opens; each one already
# burned the 10s request timeout, and ~1.5k candidates × 10s would blow the
# CI job timeout.
_MAX_CONSECUTIVE_NETWORK_ERRORS = 3


class GoogleGeocoder:
    """Cached Google Maps Geocoding API client."""

    def __init__(self, api_key: str | None, cache_path: Path | None = None):
        """Initialize the geocoder.

        Args:
            api_key: Google Maps API key, or None for cache-only mode.
            cache_path: Path to JSON cache file. If None, nothing is persisted.
        """
        self.api_key = api_key or None
        self.cache_path = cache_path
        self._cache: dict[str, dict] = {}
        self._last_request_time = 0.0
        # Per-API bookkeeping, keyed "geocoding" / "places".
        self._api_calls = {"geocoding": 0, "places": 0}
        self._rejected = {"geocoding": 0, "places": 0}
        self._circuit_open = {"geocoding": False, "places": False}
        self._consecutive_network_errors = {"geocoding": 0, "places": 0}
        # (status, message) of the most recent non-answer per API, and overall.
        self.errors: dict[str, tuple[str, str]] = {}
        self.last_error: tuple[str, str] | None = None

        if self.cache_path and self.cache_path.exists():
            self._load_cache()

    def _load_cache(self):
        """Load cache from disk."""
        try:
            with open(self.cache_path) as f:
                self._cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._cache = {}

    def save_cache(self):
        """Save cache to disk."""
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w") as f:
                json.dump(self._cache, f, indent=2)

    @property
    def is_live(self) -> bool:
        """True when Geocoding cache misses will hit Google: a key is present and
        the Geocoding circuit hasn't opened on a refusal, quota error, or
        repeated network failure."""
        return self.api_key is not None and not self._circuit_open["geocoding"]

    @property
    def places_live(self) -> bool:
        """Same as is_live, for the Places text-search circuit."""
        return self.api_key is not None and not self._circuit_open["places"]

    @property
    def rejected_calls(self) -> int:
        """Geocoding calls that returned a status other than OK/ZERO_RESULTS."""
        return self._rejected["geocoding"]

    @property
    def places_rejected_calls(self) -> int:
        return self._rejected["places"]

    @property
    def api_calls_made(self) -> int:
        """Geocoding API calls actually made (cache misses)."""
        return self._api_calls["geocoding"]

    @property
    def places_calls_made(self) -> int:
        return self._api_calls["places"]

    def api_status(self, api: str) -> dict:
        """Summary for the run-status file: calls, rejections, error, circuit."""
        err = self.errors.get(api)
        return {
            "apiCalls": self._api_calls[api],
            "rejectedCalls": self._rejected[api],
            "error": None if err is None else {"status": err[0], "message": err[1]},
            "circuitOpen": self._circuit_open[api],
        }

    def _record_error(self, api: str, status: str, message: str, *, open_circuit: bool) -> None:
        self.errors[api] = (status, message)
        self.last_error = (status, message)
        if open_circuit:
            self._circuit_open[api] = True

    def _record_network_error(self, api: str, exc: Exception) -> None:
        self._consecutive_network_errors[api] += 1
        self._record_error(
            api, "NETWORK_ERROR", str(exc),
            open_circuit=self._consecutive_network_errors[api] >= _MAX_CONSECUTIVE_NETWORK_ERRORS,
        )

    @property
    def cache_size(self) -> int:
        """Number of entries in cache."""
        return len(self._cache)

    def _build_query(self, org: dict) -> str:
        """Build a geocoding query string based on org type.

        Args:
            org: Parsed org dict with raw_name, city, state, type.

        Returns:
            Query string for Google Maps Geocoding API.
        """
        org_type = org.get("type", "other")
        city = org.get("city") or ""
        state = org.get("state") or ""
        raw_name = org.get("raw_name", "")

        if org_type == "pd" and city and state:
            return f"{city}, {state}"
        elif org_type == "so" and city and state:
            # For SOs, city is usually "X County"
            county = city
            if not county.lower().endswith(" county"):
                county = f"{county} County"
            return f"{county}, {state}"
        elif state:
            return f"{raw_name}, {state}"
        else:
            return raw_name

    def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.time()

    def _call_api(self, query: str) -> tuple[dict | None, str]:
        """Make one geocoding API call.

        Returns:
            (result, status): result is {'lat', 'lng'} on success, else None.
            status is Google's status string, "ZERO_RESULTS" for an OK response
            with no results, or "NETWORK_ERROR" when the request never got an answer.
            Side effects: counts calls, records last_error, opens the circuit.
        """
        self._rate_limit()

        params = urllib.parse.urlencode({
            "address": query,
            "key": self.api_key,
            "components": "country:US",
        })
        url = f"{_GEOCODE_API_URL}?{params}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self._record_network_error("geocoding", e)
            return None, "NETWORK_ERROR"

        self._api_calls["geocoding"] += 1
        self._consecutive_network_errors["geocoding"] = 0
        status = str(data.get("status") or "UNKNOWN")

        if status == "OK":
            results = data.get("results") or []
            if not results:
                return None, "ZERO_RESULTS"
            location = results[0]["geometry"]["location"]
            return {"lat": location["lat"], "lng": location["lng"]}, status

        if status in _NEGATIVE_STATUSES:
            return None, status

        self._rejected["geocoding"] += 1
        self._record_error(
            "geocoding", status, str(data.get("error_message") or ""),
            open_circuit=status in _CIRCUIT_BREAKER_STATUSES,
        )
        return None, status

    # ── Places (New) text search ─────────────────────────────────────────────

    def _build_places_query(self, org: dict) -> str:
        """Raw agency name (minus the [Federal] tag) plus the state, for text search."""
        name = (org.get("raw_name") or "").replace("[Federal]", "").strip()
        state = (org.get("state") or "").strip()
        return f"{name}, {state}" if name and state else name

    def _call_places(self, query: str) -> tuple[dict | None, str]:
        """One Places text-search call. Same contract as _call_api."""
        self._rate_limit()
        body = json.dumps({"textQuery": query, "regionCode": "US", "pageSize": 1}).encode("utf-8")
        req = urllib.request.Request(
            _PLACES_API_URL, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": _PLACES_FIELD_MASK,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # The new API reports refusals as HTTP 4xx with {"error": {...}}.
            try:
                err = json.loads(e.read().decode("utf-8") or "{}").get("error") or {}
            except (OSError, json.JSONDecodeError, AttributeError):
                err = {}
            status = str(err.get("status") or f"HTTP_{e.code}")
            self._api_calls["places"] += 1
            self._consecutive_network_errors["places"] = 0
            self._rejected["places"] += 1
            self._record_error(
                "places", status, str(err.get("message") or ""),
                open_circuit=e.code in _PLACES_CIRCUIT_BREAKER_HTTP or status in _PLACES_CIRCUIT_BREAKER_STATUSES,
            )
            return None, status
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self._record_network_error("places", e)
            return None, "NETWORK_ERROR"

        self._api_calls["places"] += 1
        self._consecutive_network_errors["places"] = 0
        places = data.get("places") or []
        if not places:
            return None, "ZERO_RESULTS"
        top = places[0]
        loc = top.get("location") or {}
        if "latitude" not in loc or "longitude" not in loc:
            return None, "ZERO_RESULTS"
        return {
            "lat": loc["latitude"],
            "lng": loc["longitude"],
            "name": (top.get("displayName") or {}).get("text"),
            "address": top.get("formattedAddress"),
        }, "OK"

    def places_lookup(self, org: dict) -> tuple[float, float] | None:
        """Resolve an org by its raw name via Places text search. Cached under
        a ``places:`` key so it never collides with Geocoding entries. Returns
        (lat, lng) or None. Never hits the network without a key or with the
        Places circuit open."""
        query = self._build_places_query(org)
        if not query.strip():
            return None
        key = _PLACES_CACHE_PREFIX + query

        if key in self._cache:
            cached = self._cache[key]
            return None if cached is None else (cached["lat"], cached["lng"])

        if not self.places_live:
            return None

        result, status = self._call_places(query)
        if result is not None or status in _NEGATIVE_STATUSES:
            self._cache[key] = result
        return (result["lat"], result["lng"]) if result else None

    def invalidate(self, org: dict, source: str = "geocode") -> bool:
        """Remove this org's cache entry (Geocoding by default, or
        ``source="places"``) so a later run re-queries it. Returns True if an
        entry was removed.
        """
        if source == "places":
            key = _PLACES_CACHE_PREFIX + self._build_places_query(org)
        else:
            key = self._build_query(org)
        return self._cache.pop(key, None) is not None

    def geocode_org(self, org: dict) -> tuple[float, float] | None:
        """Geocode an organization using the Google Maps API.

        Results are cached by the query string to avoid duplicate API calls.

        Args:
            org: Parsed org dict with raw_name, city, state, type.

        Returns:
            Tuple of (lat, lng) or None if geocoding failed.
        """
        query = self._build_query(org)
        if not query.strip():
            return None

        # Check cache
        if query in self._cache:
            cached = self._cache[query]
            if cached is None:
                return None
            return (cached["lat"], cached["lng"])

        # Cache-only mode (no key, or circuit open): never hit the network,
        # never record the miss.
        if not self.is_live:
            return None

        result, status = self._call_api(query)
        # Only genuine answers are cached: a hit, or Google saying "no such
        # place". Refusals, quota errors and network failures are not answers.
        if result is not None or status in _NEGATIVE_STATUSES:
            self._cache[query] = result
        if result:
            return (result["lat"], result["lng"])
        return None


def load_api_key(env_path: Path) -> str | None:
    """Load the Google Maps API key from a .env file.

    Looks for a line like: GOOGLEMAPSAPI=AIza...

    Args:
        env_path: Path to .env file.

    Returns:
        API key string, or None if not found.
    """
    if not env_path.exists():
        return None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GOOGLEMAPSAPI="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None
