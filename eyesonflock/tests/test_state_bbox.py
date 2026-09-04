#!/usr/bin/env python3
"""Tests for state_bbox — shared bounding-box plausibility check."""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)

from state_bbox import STATE_BBOX, is_in_state_bbox


class TestIsInStateBbox:
    def test_point_inside_state(self):
        # Downtown Dallas, TX
        assert is_in_state_bbox(32.7767, -96.7970, "TX") is True

    def test_point_outside_state(self):
        # Los Angeles coords claimed as Louisiana
        assert is_in_state_bbox(34.0549, -118.2426, "LA") is False

    def test_indiana_garbage_point_rejected(self):
        # The (38.7946, -106.5348) Google-garbage centroid for IN queries
        assert is_in_state_bbox(38.7946, -106.5348, "IN") is False

    def test_curry_county_nm_mis_geocode_rejected(self):
        # Google returned Texas coords for a New Mexico query
        assert is_in_state_bbox(29.5379, -96.493, "NM") is False

    def test_unknown_state_code_passes(self):
        # We can't validate without a bbox — don't reject
        assert is_in_state_bbox(0.0, 0.0, "ZZ") is True

    def test_missing_state_passes(self):
        assert is_in_state_bbox(34.0, -118.0, None) is True
        assert is_in_state_bbox(34.0, -118.0, "") is True

    def test_san_francisco_override_inside_ca(self):
        # Sanity check that the SF manual-override coords are considered valid
        assert is_in_state_bbox(37.7749, -122.4194, "CA") is True

    def test_alaska_western_longitude(self):
        # Alaska crosses into eastern-hemisphere longitudes; bbox must cover it
        assert is_in_state_bbox(60.0, -150.0, "AK") is True


class TestStateBbox:
    def test_fifty_states_plus_dc_and_pr(self):
        # 50 states + DC + PR = 52 entries; AK is included, HI is included
        assert len(STATE_BBOX) == 52
        assert "DC" in STATE_BBOX
        assert "PR" in STATE_BBOX
        assert "AK" in STATE_BBOX
        assert "HI" in STATE_BBOX

    def test_bbox_ordering(self):
        # Every entry must have min < max on both axes
        for state, (min_lat, max_lat, min_lng, max_lng) in STATE_BBOX.items():
            assert min_lat < max_lat, f"{state}: min_lat >= max_lat"
            assert min_lng < max_lng, f"{state}: min_lng >= max_lng"
