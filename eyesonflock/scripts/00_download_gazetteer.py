#!/usr/bin/env python3
"""Manual one-shot: refresh the Census Bureau gazetteer files in gazetteer/.

NOT part of run_pipeline.py. The gazetteers change at most annually; bump
GAZETTEER_VINTAGE in paths.py, delete the old files, run this script, and
commit the new ones. See METHODOLOGY.md.

Downloads three gazetteer files to gazetteer/:
  - <vintage>_Gaz_place_national.txt    (places / cities)
  - <vintage>_Gaz_counties_national.txt (counties)
  - <vintage>_Gaz_state_national.txt    (states, derived from counties)

The Census Bureau distributes place and county gazetteers as .zip archives.
There is no official state-level gazetteer, so we derive one by computing
area-weighted centroids from the county file.
"""

import csv
import io
import urllib.request
import zipfile
from pathlib import Path

from paths import COUNTY_GAZ, GAZETTEER_DIR, GAZETTEER_VINTAGE, PLACE_GAZ, STATE_GAZ

CENSUS_BASE = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    f"{GAZETTEER_VINTAGE}_Gazetteer"
)

# Census distributes these as zip archives containing a single .txt file
GAZETTEER_ZIPS: dict[Path, str] = {
    PLACE_GAZ: f"{CENSUS_BASE}/{GAZETTEER_VINTAGE}_Gaz_place_national.zip",
    COUNTY_GAZ: f"{CENSUS_BASE}/{GAZETTEER_VINTAGE}_Gaz_counties_national.zip",
}

# State FIPS to name mapping (50 states + DC + PR)
STATE_FIPS = {
    "01": "Alabama", "02": "Alaska", "04": "Arizona", "05": "Arkansas",
    "06": "California", "08": "Colorado", "09": "Connecticut", "10": "Delaware",
    "11": "District of Columbia", "12": "Florida", "13": "Georgia", "15": "Hawaii",
    "16": "Idaho", "17": "Illinois", "18": "Indiana", "19": "Iowa",
    "20": "Kansas", "21": "Kentucky", "22": "Louisiana", "23": "Maine",
    "24": "Maryland", "25": "Massachusetts", "26": "Michigan", "27": "Minnesota",
    "28": "Mississippi", "29": "Missouri", "30": "Montana", "31": "Nebraska",
    "32": "Nevada", "33": "New Hampshire", "34": "New Jersey", "35": "New Mexico",
    "36": "New York", "37": "North Carolina", "38": "North Dakota", "39": "Ohio",
    "40": "Oklahoma", "41": "Oregon", "42": "Pennsylvania", "44": "Rhode Island",
    "45": "South Carolina", "46": "South Dakota", "47": "Tennessee", "48": "Texas",
    "49": "Utah", "50": "Vermont", "51": "Virginia", "53": "Washington",
    "54": "West Virginia", "55": "Wisconsin", "56": "Wyoming",
    "72": "Puerto Rico",
}

# State name → USPS code, for the derived state file
STATE_USPS = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "Puerto Rico": "PR",
}


def download_and_extract(url: str, dest: Path) -> None:
    """Download a zip file and extract the .txt file inside it."""
    print(f"    Fetching {url.split('/')[-1]}...")
    response = urllib.request.urlopen(url)
    zip_data = response.read()

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        txt_files = [n for n in zf.namelist() if n.endswith(".txt")]
        if not txt_files:
            raise RuntimeError(f"No .txt file found in {url}")
        with zf.open(txt_files[0]) as src, open(dest, "wb") as dst:
            dst.write(src.read())


def line_count(path: Path) -> int:
    """Count lines in a file."""
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def derive_state_gazetteer(counties_path: Path, state_path: Path) -> None:
    """Derive a state-level gazetteer from the counties file.

    Uses area-weighted centroids: each county's lat/lng is weighted by its
    land area (ALAND_SQMI) to produce a state-level centroid.
    """
    print("  Deriving state gazetteer from counties...")

    states: dict[str, dict] = {}  # fips -> aggregate

    with open(counties_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            # Strip whitespace from keys (Census files have trailing spaces)
            row = {k.strip(): v.strip() if v else v for k, v in row.items()}

            geoid = row.get("GEOID", "")
            state_fips = geoid[:2] if len(geoid) >= 2 else ""
            if not state_fips or state_fips not in STATE_FIPS:
                continue

            try:
                lat = float(row.get("INTPTLAT", 0))
                lng = float(row.get("INTPTLONG", 0))
                area = float(row.get("ALAND_SQMI", 1))
            except (ValueError, TypeError):
                continue

            if area <= 0:
                area = 1  # Avoid division by zero

            s = states.setdefault(state_fips, {
                "name": STATE_FIPS[state_fips],
                "total_area": 0,
                "weighted_lat": 0,
                "weighted_lng": 0,
                "aland": 0,
                "awater": 0,
                "aland_sqmi": 0,
                "awater_sqmi": 0,
            })
            s["total_area"] += area
            s["weighted_lat"] += lat * area
            s["weighted_lng"] += lng * area
            s["aland"] += int(float(row.get("ALAND", 0)))
            s["awater"] += int(float(row.get("AWATER", 0)))
            s["aland_sqmi"] += float(row.get("ALAND_SQMI", 0))
            s["awater_sqmi"] += float(row.get("AWATER_SQMI", 0))

    with open(state_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow([
            "USPS", "GEOID", "NAME",
            "ALAND", "AWATER", "ALAND_SQMI", "AWATER_SQMI",
            "INTPTLAT", "INTPTLONG",
        ])
        for fips in sorted(states):
            s = states[fips]
            lat = s["weighted_lat"] / s["total_area"]
            lng = s["weighted_lng"] / s["total_area"]
            writer.writerow([
                STATE_USPS.get(s["name"], ""), fips, s["name"],
                s["aland"], s["awater"],
                f"{s['aland_sqmi']:.3f}", f"{s['awater_sqmi']:.3f}",
                f"{lat:+.7f}", f"{lng:+.7f}",
            ])

    print(f"    Saved: {state_path.name} ({line_count(state_path):,} lines)")


def main() -> None:
    print(f"=== Download Census Gazetteer Files ({GAZETTEER_VINTAGE} vintage) ===")
    GAZETTEER_DIR.mkdir(parents=True, exist_ok=True)

    for dest, url in GAZETTEER_ZIPS.items():
        if dest.exists():
            print(f"  Skipping (cached): {dest.name}")
            print(f"    {line_count(dest):,} lines")
            continue
        print(f"  Downloading {dest.name}...")
        download_and_extract(url, dest)
        print(f"    Saved: {dest.name} ({line_count(dest):,} lines)")

    if STATE_GAZ.exists():
        print(f"  Skipping (cached): {STATE_GAZ.name}")
        print(f"    {line_count(STATE_GAZ):,} lines")
    elif not COUNTY_GAZ.exists():
        print("  ERROR: Cannot derive state gazetteer — counties file missing")
        raise SystemExit(1)
    else:
        derive_state_gazetteer(COUNTY_GAZ, STATE_GAZ)

    print("Done.")


if __name__ == "__main__":
    main()
