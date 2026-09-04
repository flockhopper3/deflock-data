#!/usr/bin/env python3
"""Fetch the latest EyesOnFlock transparency-portal snapshot.

Reads:  https://eyesonflock.com/api/v1/data   (live, no auth)
Writes: <work>/raw/eyesonflock_full_data.json (atomic overwrite; see paths.py)

This is the first step of the pipeline — all downstream steps read from the
file written here. The snapshot is the "as of" of every resulting analysis.

## Behavior

1. GET the API with a timeout.
2. Validate that the response is JSON with a non-empty `portals` list and
   a `summary` key (the contract our pipeline depends on).
3. Sanity-check the new snapshot:
   - Fail loudly if it has fewer than MIN_PORTAL_COUNT_ABS portals — an
     always-on floor that needs no prior snapshot (CI may not have one).
   - Fail loudly if the portal count dropped by >50% vs the previous
     snapshot, when one exists (guards against API returning a corrupt
     partial response that would silently nuke the existing snapshot).
4. Print a one-page diff summary: new portals, removed portals, and portals
   whose sharing list or metadata changed.
5. Write the new snapshot atomically (tempfile → rename) so a crash
   mid-download can't leave the pipeline reading a truncated file.

## Exit codes

0 — snapshot updated (or already identical)
1 — network error, bad response, failed validation; existing snapshot untouched

## Not handled

- History / versioning. We overwrite in place. If you want durable snapshots,
  tee to `<work>/raw/history/eyesonflock_YYYY-MM-DD.json` before calling this,
  or extend this script.
- Auth. The endpoint is currently public; if EyesOnFlock later requires an
  API key, add it via a new env var (e.g., `EYESONFLOCK_API_KEY`) following
  the GOOGLEMAPSAPI pattern in 02a_google_geocode.py.
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from paths import SNAPSHOT_FILE

OUTPUT_FILE = SNAPSHOT_FILE

API_URL = "https://eyesonflock.com/api/v1/data"
REQUEST_TIMEOUT_S = 60

# If the new snapshot reports fewer than this fraction of the prior portal
# count, abort rather than overwrite. Guards against API errors that return
# a partial dataset. Only applies when a prior snapshot is available.
MIN_PORTAL_COUNT_RATIO = 0.5

# Absolute floor, applied on every run whether or not a prior exists. The
# 2026-04 reference snapshot had 908 portals and the count only grows; a
# response below this is a truncated or broken payload, never real data.
MIN_PORTAL_COUNT_ABS = 500


def fetch() -> dict:
    """Fetch and parse the EyesOnFlock API response. Raises on network/JSON error."""
    print(f"  GET {API_URL}")
    req = urllib.request.Request(API_URL, headers={"User-Agent": "flockhopper-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        raw = resp.read()
    print(f"  Received {len(raw):,} bytes")
    return json.loads(raw)


def validate(data: dict) -> None:
    """Raise ValueError if the payload doesn't match the shape our pipeline expects."""
    if not isinstance(data, dict):
        raise ValueError(f"expected dict at top level, got {type(data).__name__}")
    portals = data.get("portals")
    if not isinstance(portals, list):
        raise ValueError("missing or non-list `portals` key")
    if not portals:
        raise ValueError("`portals` list is empty")
    if "summary" not in data:
        raise ValueError("missing `summary` key")
    # Spot-check the first portal for required fields downstream steps depend on.
    required = {"slug", "state", "type", "organizations_shared_with"}
    missing = required - set(portals[0].keys())
    if missing:
        raise ValueError(f"first portal is missing required keys: {sorted(missing)}")


def load_prior(path: Path) -> dict | None:
    """Load the prior snapshot if present. Returns None on first run or on parse error."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def diff_summary(prior: dict | None, new: dict) -> tuple[int, int, int, int]:
    """Compute (added, removed, sharing_changed, metadata_changed) vs prior.

    Returns zeros when prior is None (first run).
    """
    if prior is None:
        return (0, 0, 0, 0)

    prior_portals = {p["slug"]: p for p in prior.get("portals", []) if p.get("slug")}
    new_portals = {p["slug"]: p for p in new.get("portals", []) if p.get("slug")}

    added = len(set(new_portals) - set(prior_portals))
    removed = len(set(prior_portals) - set(new_portals))
    sharing_changed = 0
    metadata_changed = 0
    meta_fields = (
        "total_cameras", "total_searches", "vehicles_captured",
        "population", "hotlist_hits", "data_retention",
    )
    for slug in set(new_portals) & set(prior_portals):
        a = prior_portals[slug]
        b = new_portals[slug]
        if set(a.get("organizations_shared_with") or []) != set(b.get("organizations_shared_with") or []):
            sharing_changed += 1
        if any(a.get(f) != b.get(f) for f in meta_fields):
            metadata_changed += 1

    return (added, removed, sharing_changed, metadata_changed)


def check_sanity(prior: dict | None, new: dict) -> None:
    """Raise ValueError if the new snapshot is implausibly small.

    Two guards: an absolute floor (always), and a relative drop vs the prior
    snapshot (only when a prior exists).
    """
    new_n = len(new.get("portals", []))
    if new_n < MIN_PORTAL_COUNT_ABS:
        raise ValueError(
            f"new snapshot has only {new_n} portals, below the absolute floor "
            f"of {MIN_PORTAL_COUNT_ABS}. Refusing to overwrite."
        )
    if prior is None:
        return
    prior_n = len(prior.get("portals", []))
    if prior_n == 0:
        return
    if new_n < MIN_PORTAL_COUNT_RATIO * prior_n:
        raise ValueError(
            f"new snapshot has only {new_n} portals vs {prior_n} before "
            f"(<{int(MIN_PORTAL_COUNT_RATIO * 100)}% retained). Refusing to overwrite."
        )


def write_atomic(path: Path, data: dict) -> None:
    """Write JSON to `path` via tempfile + rename so partial writes can't corrupt the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp lives on the same filesystem as the destination so rename is atomic.
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on failure; don't hide the original exception.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> int:
    print("=== Fetch EyesOnFlock Snapshot ===")
    try:
        data = fetch()
    except urllib.error.URLError as e:
        print(f"  ERROR: network error: {e}")
        return 1
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ERROR: could not parse API response: {e}")
        return 1

    try:
        validate(data)
    except ValueError as e:
        print(f"  ERROR: invalid response: {e}")
        return 1

    prior = load_prior(OUTPUT_FILE)

    try:
        check_sanity(prior, data)
    except ValueError as e:
        print(f"  ERROR: sanity check failed: {e}")
        return 1

    added, removed, sharing_changed, metadata_changed = diff_summary(prior, data)
    summary = data.get("summary") or {}
    print(f"  Portals in new snapshot:  {len(data['portals']):,}")
    if prior is not None:
        print(f"  Portals in prior snapshot: {len(prior.get('portals', [])):,}")
        print(f"    Added:             {added}")
        print(f"    Removed:           {removed}")
        print(f"    Sharing changed:   {sharing_changed}")
        print(f"    Metadata changed:  {metadata_changed}")
    if summary.get("total_cameras") is not None:
        print(f"  Summary: {summary.get('total_cameras', 0):,} cameras across {summary.get('total_portals_found', 0):,} portals")

    write_atomic(OUTPUT_FILE, data)
    print(f"  Wrote {OUTPUT_FILE}")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
