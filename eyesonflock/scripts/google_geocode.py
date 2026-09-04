#!/usr/bin/env python3
"""Google Maps Geocoding API client for ALPR organization geocoding.

Provides a cached geocoding function that queries the Google Maps Geocoding API
as a fallback for organizations that Census gazetteers can't resolve. Results
are cached to a JSON file to avoid redundant API calls.

Usage:
    client = GoogleGeocoder(api_key="...")
    lat, lng = client.geocode_org(org)

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
        self._api_calls = 0

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
        """True when an API key is present and cache misses will hit Google."""
        return self.api_key is not None

    @property
    def api_calls_made(self) -> int:
        """Number of actual API calls made (cache misses)."""
        return self._api_calls

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

    def _call_api(self, query: str) -> dict | None:
        """Make a geocoding API call.

        Args:
            query: The address/query string.

        Returns:
            Dict with 'lat' and 'lng' keys, or None if geocoding failed.
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
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return None

        self._api_calls += 1

        if data.get("status") != "OK" or not data.get("results"):
            return None

        location = data["results"][0]["geometry"]["location"]
        return {"lat": location["lat"], "lng": location["lng"]}

    def invalidate(self, org: dict) -> bool:
        """Remove this org's cache entry so the next run can re-query.

        Used when the cached result fails a downstream plausibility check
        (e.g., coords outside the declared state). Returns True if an entry
        was removed.
        """
        query = self._build_query(org)
        return self._cache.pop(query, None) is not None

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

        # Cache-only mode: never hit the network, never record the miss.
        if not self.is_live:
            return None

        # Call API
        result = self._call_api(query)
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
