#!/usr/bin/env python3
"""Run the full sharing network data pipeline.

Executes all pipeline steps in order:
  00  - Fetch fresh EyesOnFlock snapshot from the public API
  01  - Parse + canonicalize organization names (dedupes aliases, flags junk)
  02  - Geocode organizations via Census gazetteers
  02a - Upgrade state/default fallbacks via the Google geocode cache (+ API if keyed)
  03  - Build GeoJSON nodes (with isPortal/isJunk/isInactive/isLikelyAggregator flags)
  04  - Build adjacency JSON (outbound-only, directional)
  05  - Geocoding quality audit report
  06  - Verify output invariants and write meta.json (fails the run on violation)

Step 00 replaces the raw snapshot under the work dir on every run, so steps
01–04 always rebuild from the freshest available data. The pipeline is safe to
run on a schedule. Step 02a retains its per-query Google Maps cache
(eyesonflock/google_geocode_cache.json) so no paid API calls are repeated.

Usage:
  python run_pipeline.py                # full run, fetches a fresh snapshot
  python run_pipeline.py --skip-fetch   # rebuild from the snapshot already in the work dir

The work dir defaults to eyesonflock/work; set EYESONFLOCK_WORK_DIR to move it.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from paths import ADJACENCY_FILE, META_FILE, NODES_FILE, SNAPSHOT_FILE, WORK_DIR

SCRIPT_DIR = Path(__file__).resolve().parent

FETCH_SCRIPT = "00_fetch_eyesonflock.py"

SCRIPTS = [
    FETCH_SCRIPT,
    "01_parse_orgs.py",
    "02_geocode_orgs.py",
    "02a_google_geocode.py",
    "03_build_nodes_geojson.py",
    "04_build_adjacency.py",
    "05_audit_geocoding.py",
    "06_verify_outputs.py",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="skip step 00 and rebuild from the snapshot already in the work dir",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    scripts = [s for s in SCRIPTS if not (args.skip_fetch and s == FETCH_SCRIPT)]

    print("=" * 60)
    print("SHARING NETWORK DATA PIPELINE")
    print("=" * 60)
    print(f"Work dir: {WORK_DIR}")
    if args.skip_fetch:
        if not SNAPSHOT_FILE.exists():
            print(f"\nERROR: --skip-fetch given but no snapshot at {SNAPSHOT_FILE}")
            return 1
        print(f"Skipping fetch; using existing snapshot {SNAPSHOT_FILE}")

    for script_name in scripts:
        script_path = SCRIPT_DIR / script_name
        print(f"\n--- Running {script_name} ---", flush=True)

        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(SCRIPT_DIR),
        )
        if result.returncode != 0:
            print(f"\nERROR: {script_name} failed (exit code {result.returncode})")
            return 1

    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print("\nOutputs:")
    print(f"  {NODES_FILE}")
    print(f"  {ADJACENCY_FILE}")
    print(f"  {META_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
