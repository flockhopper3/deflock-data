#!/usr/bin/env python3
"""Tests for 00_fetch_eyesonflock — validation, diff, sanity check, atomic write.

Does NOT hit the live API. All tests operate on in-memory fixtures.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)

_fetch = importlib.import_module("00_fetch_eyesonflock")


def _portal(slug: str, **overrides) -> dict:
    base = {
        "slug": slug,
        "state": "MI",
        "city": "Somewhere",
        "type": "PD",
        "organizations_shared_with": [],
        "total_cameras": 1,
        "total_searches": 2,
        "vehicles_captured": 3,
        "population": 4,
        "hotlist_hits": 5,
        "data_retention": 30,
    }
    base.update(overrides)
    return base


def _snapshot(portals: list[dict]) -> dict:
    return {"summary": {"total_portals_found": len(portals)}, "portals": portals}


class TestValidate:
    def test_accepts_good_payload(self):
        _fetch.validate(_snapshot([_portal("a")]))

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="expected dict"):
            _fetch.validate([])

    def test_rejects_missing_portals(self):
        with pytest.raises(ValueError, match="missing or non-list"):
            _fetch.validate({"summary": {}})

    def test_rejects_empty_portals(self):
        with pytest.raises(ValueError, match="empty"):
            _fetch.validate({"summary": {}, "portals": []})

    def test_rejects_missing_summary(self):
        with pytest.raises(ValueError, match="summary"):
            _fetch.validate({"portals": [_portal("a")]})

    def test_rejects_portal_missing_required_keys(self):
        # drop `slug` — parser depends on it
        p = _portal("a")
        del p["slug"]
        with pytest.raises(ValueError, match="missing required keys"):
            _fetch.validate({"summary": {}, "portals": [p]})


class TestDiffSummary:
    def test_first_run_returns_zeros(self):
        assert _fetch.diff_summary(None, _snapshot([_portal("a")])) == (0, 0, 0, 0)

    def test_detects_added_and_removed(self):
        prior = _snapshot([_portal("a"), _portal("b")])
        new = _snapshot([_portal("a"), _portal("c"), _portal("d")])
        added, removed, sharing_changed, metadata_changed = _fetch.diff_summary(prior, new)
        assert added == 2   # c, d
        assert removed == 1  # b
        assert sharing_changed == 0
        assert metadata_changed == 0

    def test_detects_sharing_list_change(self):
        prior = _snapshot([_portal("a", organizations_shared_with=["X PD"])])
        new = _snapshot([_portal("a", organizations_shared_with=["X PD", "Y PD"])])
        assert _fetch.diff_summary(prior, new) == (0, 0, 1, 0)

    def test_ignores_sharing_list_reordering(self):
        prior = _snapshot([_portal("a", organizations_shared_with=["X PD", "Y PD"])])
        new = _snapshot([_portal("a", organizations_shared_with=["Y PD", "X PD"])])
        # Set semantics — order doesn't matter
        assert _fetch.diff_summary(prior, new) == (0, 0, 0, 0)

    def test_detects_metadata_change(self):
        prior = _snapshot([_portal("a", total_cameras=10)])
        new = _snapshot([_portal("a", total_cameras=15)])
        assert _fetch.diff_summary(prior, new) == (0, 0, 0, 1)

    def test_sharing_and_metadata_counted_independently(self):
        prior = _snapshot([_portal("a", total_cameras=10, organizations_shared_with=[])])
        new = _snapshot([_portal("a", total_cameras=15, organizations_shared_with=["X PD"])])
        assert _fetch.diff_summary(prior, new) == (0, 0, 1, 1)


class TestCheckSanity:
    def test_no_prior_passes(self):
        _fetch.check_sanity(None, _snapshot([_portal("a")]))

    def test_similar_size_passes(self):
        prior = _snapshot([_portal(f"p{i}") for i in range(100)])
        new = _snapshot([_portal(f"p{i}") for i in range(95)])
        _fetch.check_sanity(prior, new)  # 95% retained — fine

    def test_modest_growth_passes(self):
        prior = _snapshot([_portal(f"p{i}") for i in range(100)])
        new = _snapshot([_portal(f"p{i}") for i in range(200)])
        _fetch.check_sanity(prior, new)

    def test_drop_below_threshold_fails(self):
        prior = _snapshot([_portal(f"p{i}") for i in range(100)])
        new = _snapshot([_portal(f"p{i}") for i in range(40)])  # 40% — below 50% floor
        with pytest.raises(ValueError, match="Refusing to overwrite"):
            _fetch.check_sanity(prior, new)

    def test_prior_empty_passes(self):
        prior = _snapshot([])
        # empty prior shouldn't trigger division-by-zero or false reject
        new = _snapshot([_portal("a")])
        _fetch.check_sanity(prior, new)


class TestWriteAtomic:
    def test_writes_json(self, tmp_path):
        dest = tmp_path / "out.json"
        _fetch.write_atomic(dest, {"hello": "world"})
        assert json.loads(dest.read_text()) == {"hello": "world"}

    def test_overwrites_existing(self, tmp_path):
        dest = tmp_path / "out.json"
        dest.write_text('{"old": true}')
        _fetch.write_atomic(dest, {"new": True})
        assert json.loads(dest.read_text()) == {"new": True}

    def test_no_temp_file_left_on_success(self, tmp_path):
        dest = tmp_path / "out.json"
        _fetch.write_atomic(dest, {"x": 1})
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("out.json.")]
        assert leftovers == []
