#!/usr/bin/env python3
"""Tests for paths — the single source of truth for pipeline file locations."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def _reload(monkeypatch, env: str | None):
    if env is None:
        monkeypatch.delenv("EYESONFLOCK_WORK_DIR", raising=False)
    else:
        monkeypatch.setenv("EYESONFLOCK_WORK_DIR", env)
    import paths
    return importlib.reload(paths)


def test_default_work_dir_is_inside_hub(monkeypatch):
    p = _reload(monkeypatch, None)
    assert p.EOF_ROOT.name == "eyesonflock"
    assert p.WORK_DIR == p.EOF_ROOT / "work"
    assert p.RAW_DIR == p.WORK_DIR / "raw"
    assert p.INT_DIR == p.WORK_DIR / "intermediate"
    assert p.OUTPUT_DIR == p.WORK_DIR / "output"


def test_env_override_relocates_generated_paths_only(monkeypatch, tmp_path):
    p = _reload(monkeypatch, str(tmp_path))
    assert p.WORK_DIR == tmp_path
    assert p.SNAPSHOT_FILE == tmp_path / "raw" / "eyesonflock_full_data.json"
    assert p.PARSED_ORGS_FILE == tmp_path / "intermediate" / "parsed_orgs.json"
    assert p.GEOCODED_ORGS_FILE == tmp_path / "intermediate" / "geocoded_orgs.json"
    assert p.AUDIT_FILE == tmp_path / "intermediate" / "geocode_audit.txt"
    assert p.GOOGLE_RUN_STATUS_FILE == tmp_path / "intermediate" / "google_geocode_run.json"
    assert p.NODES_FILE == tmp_path / "output" / "sharing-network-nodes.geojson"
    assert p.ADJACENCY_FILE == tmp_path / "output" / "sharing-network-adjacency.json"
    assert p.META_FILE == tmp_path / "output" / "meta.json"
    # Committed inputs never move with the work dir.
    assert p.GAZETTEER_DIR == p.EOF_ROOT / "gazetteer"
    assert p.GOOGLE_CACHE_FILE == p.EOF_ROOT / "google_geocode_cache.json"
    assert p.ENV_FILE == p.EOF_ROOT / ".env"


def test_empty_env_override_falls_back_to_default(monkeypatch):
    p = _reload(monkeypatch, "")
    assert p.WORK_DIR == p.EOF_ROOT / "work"


def test_gazetteer_files_exist(monkeypatch):
    p = _reload(monkeypatch, None)
    assert p.GAZETTEER_VINTAGE == "2023"
    for f in (p.PLACE_GAZ, p.COUNTY_GAZ, p.STATE_GAZ):
        assert f.is_file(), f
        assert f.name.startswith(p.GAZETTEER_VINTAGE)


def test_google_cache_seed_exists(monkeypatch):
    p = _reload(monkeypatch, None)
    assert p.GOOGLE_CACHE_FILE.is_file()
