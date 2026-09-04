#!/usr/bin/env python3
"""Tests for geocode_lib — geocoding organizations against Census gazetteers."""

import sys
import tempfile
from pathlib import Path

# Allow import of geocode_lib from sharing-network/scripts/
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)

from geocode_lib import build_place_lookup, build_county_lookup, build_state_lookup, geocode_org


# ---------------------------------------------------------------------------
# build_place_lookup
# ---------------------------------------------------------------------------

class TestBuildPlaceLookup:
    def test_parses_tab_separated_gazetteer(self):
        """build_place_lookup should parse a tab-separated place file."""
        # Note: real Census headers have trailing whitespace — we add some here
        content = (
            "USPS\tGEOID\tANSICODE\tNAME\tLSAD\tFUNCSTAT\tALAND\tAWATER\t"
            "ALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG                \n"
            "AL\t0100124\t02403054\tAbbeville city\t25\tA\t40255361\t107642\t"
            "15.543\t0.042\t31.564706\t-85.259121\n"
            "CA\t0644000\t02411768\tLos Angeles city\t25\tA\t121337448\t0\t"
            "468.483\t0.000\t34.019394\t-118.410825\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            f.flush()
            lookup = build_place_lookup(Path(f.name))

        # Should have 2 entries
        assert len(lookup) == 2

        # Keys are (name_lower, state)
        assert ("abbeville city", "AL") in lookup
        assert ("los angeles city", "CA") in lookup

        # Values are (lat, lng) tuples
        lat, lng = lookup[("abbeville city", "AL")]
        assert abs(lat - 31.564706) < 0.0001
        assert abs(lng - (-85.259121)) < 0.0001

    def test_strips_header_whitespace(self):
        """Headers with trailing whitespace should be handled correctly."""
        content = (
            "USPS\tGEOID\tANSICODE\tNAME   \tLSAD\tFUNCSTAT\tALAND\tAWATER\t"
            "ALAND_SQMI\tAWATER_SQMI\tINTPTLAT   \tINTPTLONG   \n"
            "TX\t4835000\t02411789\tHouston city\t25\tA\t0\t0\t0\t0\t29.786100\t-95.387400\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            f.flush()
            lookup = build_place_lookup(Path(f.name))

        assert ("houston city", "TX") in lookup

    def test_skips_invalid_coordinates(self):
        """Rows with missing coordinates should be skipped."""
        content = (
            "USPS\tGEOID\tANSICODE\tNAME\tLSAD\tFUNCSTAT\tALAND\tAWATER\t"
            "ALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG\n"
            "TX\t4835000\t02411789\tHouston city\t25\tA\t0\t0\t0\t0\t29.786100\t-95.387400\n"
            "XX\t0000000\t00000000\tBadPlace\t25\tA\t0\t0\t0\t0\t\t\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            f.flush()
            lookup = build_place_lookup(Path(f.name))

        assert len(lookup) == 1
        assert ("houston city", "TX") in lookup


# ---------------------------------------------------------------------------
# build_county_lookup
# ---------------------------------------------------------------------------

class TestBuildCountyLookup:
    def test_parses_county_gazetteer(self):
        """build_county_lookup should parse counties and store name variants."""
        content = (
            "USPS\tGEOID\tANSICODE\tNAME\tALAND\tAWATER\tALAND_SQMI\t"
            "AWATER_SQMI\tINTPTLAT\tINTPTLONG\n"
            "AL\t01001\t00161526\tAutauga County\t1539631461\t25677536\t"
            "594.455\t9.914\t32.532237\t-86.64644\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            f.flush()
            lookup = build_county_lookup(Path(f.name))

        # Should store both the full name and the name as-is
        assert ("autauga county", "AL") in lookup
        lat, lng = lookup[("autauga county", "AL")]
        assert abs(lat - 32.532237) < 0.0001
        assert abs(lng - (-86.64644)) < 0.0001


# ---------------------------------------------------------------------------
# build_state_lookup
# ---------------------------------------------------------------------------

class TestBuildStateLookup:
    def test_parses_state_gazetteer(self):
        """build_state_lookup should return {state: (lat, lng)}."""
        content = (
            "USPS\tGEOID\tNAME\tALAND\tAWATER\tALAND_SQMI\t"
            "AWATER_SQMI\tINTPTLAT\tINTPTLONG\n"
            "AL\t01\tAlabama\t131185049346\t4582326383\t50650.834\t"
            "1769.244\t+32.7598003\t-86.8302257\n"
            "CA\t06\tCalifornia\t403673296401\t20291770234\t155859.140\t"
            "7834.695\t+37.1477708\t-119.5469251\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            f.flush()
            lookup = build_state_lookup(Path(f.name))

        assert "AL" in lookup
        assert "CA" in lookup
        lat, lng = lookup["AL"]
        assert abs(lat - 32.7598003) < 0.0001


# ---------------------------------------------------------------------------
# geocode_org — exact place match
# ---------------------------------------------------------------------------

class TestGeocodeOrgPlaceMatch:
    def test_exact_place_match(self):
        """When city+state matches a place, should return 'place' method."""
        place_lookup = {("houston city", "TX"): (29.7861, -95.3874)}
        county_lookup = {}
        state_lookup = {"TX": (31.0, -100.0)}

        org = {"city": "Houston", "state": "TX", "type": "pd"}
        lat, lng, method = geocode_org(org, place_lookup, county_lookup, state_lookup)

        assert method == "place"
        assert abs(lat - 29.7861) < 0.001
        assert abs(lng - (-95.3874)) < 0.001

    def test_place_match_case_insensitive(self):
        """Place lookup should be case insensitive."""
        place_lookup = {("los angeles city", "CA"): (34.0194, -118.4108)}
        county_lookup = {}
        state_lookup = {"CA": (37.0, -120.0)}

        org = {"city": "Los Angeles", "state": "CA", "type": "pd"}
        lat, lng, method = geocode_org(org, place_lookup, county_lookup, state_lookup)

        assert method == "place"


# ---------------------------------------------------------------------------
# geocode_org — county fallback
# ---------------------------------------------------------------------------

class TestGeocodeOrgCountyFallback:
    def test_county_fallback(self):
        """When city is a county name, should fall back to county lookup."""
        place_lookup = {}
        county_lookup = {("autauga county", "AL"): (32.5322, -86.6464)}
        state_lookup = {"AL": (32.76, -86.83)}

        org = {"city": "Autauga County", "state": "AL", "type": "so"}
        lat, lng, method = geocode_org(org, place_lookup, county_lookup, state_lookup)

        assert method == "county"
        assert abs(lat - 32.5322) < 0.001
        assert abs(lng - (-86.6464)) < 0.001


# ---------------------------------------------------------------------------
# geocode_org — state fallback
# ---------------------------------------------------------------------------

class TestGeocodeOrgStateFallback:
    def test_state_fallback(self):
        """When no place or county match, should fall back to state centroid."""
        place_lookup = {}
        county_lookup = {}
        state_lookup = {"TX": (31.0, -100.0)}

        org = {"city": "Nonexistent City", "state": "TX", "type": "pd"}
        lat, lng, method = geocode_org(org, place_lookup, county_lookup, state_lookup)

        assert method == "state"
        assert abs(lat - 31.0) < 0.001
        assert abs(lng - (-100.0)) < 0.001


# ---------------------------------------------------------------------------
# geocode_org — default fallback (no state)
# ---------------------------------------------------------------------------

class TestGeocodeOrgDefaultFallback:
    def test_default_fallback_no_state(self):
        """When org has no state, should return DC default coordinates."""
        place_lookup = {}
        county_lookup = {}
        state_lookup = {}

        org = {"city": None, "state": None, "type": "other"}
        lat, lng, method = geocode_org(org, place_lookup, county_lookup, state_lookup)

        assert method == "default"
        assert abs(lat - 38.8816) < 0.001
        assert abs(lng - (-77.0910)) < 0.001

    def test_default_fallback_state_not_in_lookup(self):
        """When org has state but it's not in lookup, should use default."""
        place_lookup = {}
        county_lookup = {}
        state_lookup = {}  # Empty — no states loaded

        org = {"city": "SomeCity", "state": "ZZ", "type": "pd"}
        lat, lng, method = geocode_org(org, place_lookup, county_lookup, state_lookup)

        assert method == "default"


# ---------------------------------------------------------------------------
# MANUAL_OVERRIDES
# ---------------------------------------------------------------------------

from geocode_lib import MANUAL_OVERRIDES


class TestManualOverrides:
    def test_san_francisco_uses_override(self):
        """San Francisco CA must resolve to the downtown override, not a gazetteer centroid."""
        org = {"city": "San Francisco", "state": "CA", "type": "pd"}
        lat, lng, method = geocode_org(org, place_lookup={}, county_lookup={}, state_lookup={})
        assert method == "manual"
        assert abs(lat - 37.7749) < 0.001
        assert abs(lng - (-122.4194)) < 0.001

    def test_override_is_case_insensitive(self):
        org = {"city": "SAN FRANCISCO", "state": "CA", "type": "pd"}
        lat, lng, method = geocode_org(org, place_lookup={}, county_lookup={}, state_lookup={})
        assert method == "manual"

    def test_override_beats_place_lookup(self):
        """Even when a place match exists, the manual override wins."""
        place_lookup = {("san francisco city", "CA"): (99.0, 99.0)}
        org = {"city": "San Francisco", "state": "CA", "type": "pd"}
        lat, lng, method = geocode_org(org, place_lookup, county_lookup={}, state_lookup={})
        assert method == "manual"
        assert lat != 99.0

    def test_non_override_city_ignores_override_table(self):
        """A city not in MANUAL_OVERRIDES falls through to the normal cascade."""
        place_lookup = {("houston city", "TX"): (29.7604, -95.3698)}
        org = {"city": "Houston", "state": "TX", "type": "pd"}
        lat, lng, method = geocode_org(org, place_lookup, county_lookup={}, state_lookup={})
        assert method == "place"
        assert abs(lat - 29.7604) < 0.001

    def test_override_table_contains_san_francisco(self):
        assert ("san francisco", "CA") in MANUAL_OVERRIDES
