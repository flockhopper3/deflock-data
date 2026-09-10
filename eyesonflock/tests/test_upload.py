#!/usr/bin/env python3
"""Tests for upload.sh — the R2 publish step. Runs the script in --dry-run
mode against a synthetic work dir, so no network and no credentials."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "upload.sh"
BASH = shutil.which("bash") or "/bin/bash"   # resolved now, so a sandboxed PATH only affects the script

ENV = {
    "R2_NETWORK_BUCKET": "deflock",
    "R2_NETWORK_ENDPOINT": "https://example.r2.cloudflarestorage.com",
    "PUBLIC_BASE_URL": "https://deflockdata.example.com",
}


def _work_dir(tmp_path: Path, *, meta: dict | None = ..., nodes: bool = True, adjacency: bool = True) -> Path:
    out = tmp_path / "output"
    out.mkdir()
    if nodes:
        (out / "sharing-network-nodes.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    if adjacency:
        (out / "sharing-network-adjacency.json").write_text("{}")
    if meta is ...:
        meta = {"generatedAt": "2026-09-05T00:00:00Z", "featureCount": 6793, "portalCount": 1044,
                "directedEdges": 289794, "runId": "123"}
    if meta is not None:
        (out / "meta.json").write_text(json.dumps(meta))
    return tmp_path


def _run(work: Path, *args: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **ENV, **(env_overrides or {})}
    return subprocess.run([BASH, str(SCRIPT), "--dry-run", str(work), *args],
                          capture_output=True, text=True, env=env)


def _cp_lines(out: str) -> list[str]:
    return [l for l in out.splitlines() if "aws s3 cp" in l]


class TestDryRun:
    def test_uploads_three_objects_with_expected_keys(self, tmp_path):
        r = _run(_work_dir(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        lines = _cp_lines(r.stdout)
        assert len(lines) == 3
        keys = [l.split("s3://deflock/")[1].split()[0] for l in lines]
        assert keys == ["sharing-network-nodes.geojson", "sharing-network-adjacency.json", "sharing-network-meta.json"]

    def test_big_files_are_gzipped_with_content_encoding(self, tmp_path):
        r = _run(_work_dir(tmp_path))
        nodes, adj, meta = _cp_lines(r.stdout)
        assert "--content-encoding gzip" in nodes and "--content-encoding gzip" in adj
        assert "--content-encoding" not in meta
        assert "--content-type application/geo+json" in nodes
        assert "--content-type application/json" in adj and "--content-type application/json" in meta

    def test_cache_control_and_metadata(self, tmp_path):
        r = _run(_work_dir(tmp_path))
        nodes, adj, meta = _cp_lines(r.stdout)
        for l in (nodes, adj):
            assert "--cache-control public, max-age=3600" in l
            assert "x-generated-at=2026-09-05T00:00:00Z" in l
            assert "x-feature-count=6793" in l
            assert "x-source=eyesonflock" in l
            assert "x-run-id=123" in l
        assert "--cache-control public, max-age=300" in meta

    def test_endpoint_is_passed(self, tmp_path):
        r = _run(_work_dir(tmp_path))
        assert all("--endpoint-url https://example.r2.cloudflarestorage.com" in l for l in _cp_lines(r.stdout))

    def test_prints_public_urls(self, tmp_path):
        r = _run(_work_dir(tmp_path))
        assert "https://deflockdata.example.com/sharing-network-nodes.geojson" in r.stdout


class TestRefusals:
    def test_refuses_without_meta(self, tmp_path):
        r = _run(_work_dir(tmp_path, meta=None))
        assert r.returncode != 0 and "meta.json" in r.stderr

    def test_refuses_zero_features(self, tmp_path):
        r = _run(_work_dir(tmp_path, meta={"generatedAt": "x", "featureCount": 0, "runId": None}))
        assert r.returncode != 0 and "featureCount" in r.stderr

    def test_refuses_missing_output_file(self, tmp_path):
        r = _run(_work_dir(tmp_path, adjacency=False))
        assert r.returncode != 0 and "adjacency" in r.stderr

    @pytest.mark.parametrize("var", ["R2_NETWORK_BUCKET", "R2_NETWORK_ENDPOINT"])
    def test_refuses_missing_env(self, tmp_path, var):
        env = {**os.environ, **ENV}
        env.pop(var)
        r = subprocess.run([BASH, str(SCRIPT), "--dry-run", str(_work_dir(tmp_path))],
                           capture_output=True, text=True, env=env)
        assert r.returncode != 0 and var in r.stderr

    def test_dry_run_never_invokes_aws(self, tmp_path):
        # A PATH holding only the tools the script legitimately needs — and no
        # aws binary — must still succeed in dry-run mode.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        for tool in ("gzip", "wc", "tr", "mktemp", "rm"):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        r = _run(_work_dir(tmp_path), env_overrides={"PATH": str(bin_dir)})
        assert r.returncode == 0, r.stderr
        assert shutil.which("aws", path=str(bin_dir)) is None
