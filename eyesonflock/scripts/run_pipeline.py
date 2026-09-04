#!/usr/bin/env python3
"""Run the full sharing network data pipeline.

Executes all pipeline steps in order:
  00  - Fetch fresh EyesOnFlock snapshot from the public API
  01  - Parse + canonicalize organization names (dedupes aliases, flags junk)
  02  - Geocode organizations via Census gazetteers
  02a - (optional) Upgrade state/default fallbacks via Google Maps API
  03  - Build GeoJSON nodes (with isPortal/isJunk/isInactive/isLikelyAggregator flags)
  04  - Build adjacency JSON (bidirectional)
  05  - (optional) Geocoding quality audit report

Step 00 replaces the raw snapshot at data/raw/eyesonflock_full_data.json on
every run, so steps 01–04 always rebuild from the freshest available data.
The pipeline is safe to run on a schedule (e.g. biweekly cron). Step 02a
retains its per-query Google Maps cache (data/intermediate/google_geocode_cache.json)
so no paid API calls are repeated for (city, state) pairs already looked up.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

SCRIPTS = [
    "00_fetch_eyesonflock.py",
    "01_parse_orgs.py",
    "02_geocode_orgs.py",
    "02a_google_geocode.py",
    "03_build_nodes_geojson.py",
    "04_build_adjacency.py",
    "05_audit_geocoding.py",
]


def main():
    print("=" * 60)
    print("SHARING NETWORK DATA PIPELINE")
    print("=" * 60)

    for script_name in SCRIPTS:
        script_path = SCRIPT_DIR / script_name
        print(f"\n--- Running {script_name} ---")

        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(SCRIPT_DIR),
        )
        if result.returncode != 0:
            print(f"\nERROR: {script_name} failed (exit code {result.returncode})")
            sys.exit(1)

    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print("\nOutputs:")
    print("  output/sharing-network-nodes.geojson")
    print("  output/sharing-network-adjacency.json")


if __name__ == "__main__":
    main()
