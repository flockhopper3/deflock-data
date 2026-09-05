#!/usr/bin/env python3
"""Single source of truth for every file location the pipeline touches.

Two kinds of paths:

* **Committed inputs** live inside the hub and never move: the Census
  gazetteer files and the Google geocode cache seed.
* **Generated data** (raw snapshot, intermediates, outputs) lives under
  WORK_DIR, which defaults to ``eyesonflock/work`` (gitignored) and can be
  relocated with the ``EYESONFLOCK_WORK_DIR`` environment variable — the
  GitHub Actions workflow points it at /tmp.

Scripts import names from here instead of computing directories from
``__file__`` so the hub can sit anywhere in the repo without colliding with
the other hubs' ``data/`` and ``output/`` directories.
"""

import os
from pathlib import Path

EOF_ROOT = Path(__file__).resolve().parent.parent

# ── Committed inputs ─────────────────────────────────────────────────────────

GAZETTEER_VINTAGE = "2023"
GAZETTEER_DIR = EOF_ROOT / "gazetteer"
PLACE_GAZ = GAZETTEER_DIR / f"{GAZETTEER_VINTAGE}_Gaz_place_national.txt"
COUNTY_GAZ = GAZETTEER_DIR / f"{GAZETTEER_VINTAGE}_Gaz_counties_national.txt"
STATE_GAZ = GAZETTEER_DIR / f"{GAZETTEER_VINTAGE}_Gaz_state_national.txt"

GOOGLE_CACHE_FILE = EOF_ROOT / "google_geocode_cache.json"
ENV_FILE = EOF_ROOT / ".env"

# ── Generated data ───────────────────────────────────────────────────────────

WORK_DIR = Path(os.environ.get("EYESONFLOCK_WORK_DIR") or EOF_ROOT / "work").resolve()
RAW_DIR = WORK_DIR / "raw"
INT_DIR = WORK_DIR / "intermediate"
OUTPUT_DIR = WORK_DIR / "output"

SNAPSHOT_FILE = RAW_DIR / "eyesonflock_full_data.json"
PARSED_ORGS_FILE = INT_DIR / "parsed_orgs.json"
GEOCODED_ORGS_FILE = INT_DIR / "geocoded_orgs.json"
AUDIT_FILE = INT_DIR / "geocode_audit.txt"
GOOGLE_RUN_STATUS_FILE = INT_DIR / "google_geocode_run.json"

NODES_FILE = OUTPUT_DIR / "sharing-network-nodes.geojson"
ADJACENCY_FILE = OUTPUT_DIR / "sharing-network-adjacency.json"
META_FILE = OUTPUT_DIR / "meta.json"
