#!/usr/bin/env python3
"""Manual one-shot: refresh the Census Bureau gazetteer files in data/raw/.

NOT part of run_pipeline.py. The gazetteers change at most annually; run this
script manually when bumping to a newer Census vintage. See METHODOLOGY.md.

Downloads three gazetteer files to data/raw/:
  - 2023_Gaz_place_national.txt   (places / cities)
  - 2023_Gaz_counties_national.txt (counties)
  - 2023_Gaz_state_national.txt   (states, derived from counties)

The Census Bureau distributes place and county gazetteers as .zip archives.
There is no official state-level gazetteer, so we derive one by computing
area-weighted centroids from the county file.
"""

import csv
import io
import urllib.request
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

CENSUS_BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer"

# Census distributes these as zip archives containing a single .txt file
GAZETTEER_ZIPS = {
    "2023_Gaz_place_national.txt": f"{CENSUS_BASE}/2023_Gaz_place_national.zip",
    "2023_Gaz_counties_national.txt": f"{CENSUS_BASE}/2023_Gaz_counties_national.zip",
}

# State-level file is derived from counties (no official Census state gazetteer)
STATE_FILE = "2023_Gaz_state_national.txt"

# State FIPS to name mapping (50 states + DC + territories)
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


def download_and_extract(url, dest):
    """Download a zip file and extract the .txt file inside it."""
    print(f"    Fetching {url.split('/')[-1]}...")
    response = urllib.request.urlopen(url)
    zip_data = response.read()

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        # Find the .txt file inside the zip
        txt_files = [n for n in zf.namelist() if n.endswith(".txt")]
        if not txt_files:
            raise RuntimeError(f"No .txt file found in {url}")
        with zf.open(txt_files[0]) as src, open(dest, "wb") as dst:
            dst.write(src.read())


def line_count(path):
    """Count lines in a file."""
    return sum(1 for _ in open(path))


def derive_state_gazetteer(counties_path, state_path):
    """Derive a state-level gazetteer from the counties file.

    Uses area-weighted centroids: each county's lat/lng is weighted by its
    land area (ALAND_SQMI) to produce a state-level centroid.
    """
    print("  Deriving state gazetteer from counties...")

    # Read counties file (tab-separated)
    states = {}  # fips -> {name, total_area, weighted_lat, weighted_lng}

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

            if state_fips not in states:
                states[state_fips] = {
                    "name": STATE_FIPS[state_fips],
                    "total_area": 0,
                    "weighted_lat": 0,
                    "weighted_lng": 0,
                    "aland": 0,
                    "awater": 0,
                    "aland_sqmi": 0,
                    "awater_sqmi": 0,
                }

            s = states[state_fips]
            s["total_area"] += area
            s["weighted_lat"] += lat * area
            s["weighted_lng"] += lng * area
            s["aland"] += int(float(row.get("ALAND", 0)))
            s["awater"] += int(float(row.get("AWATER", 0)))
            s["aland_sqmi"] += float(row.get("ALAND_SQMI", 0))
            s["awater_sqmi"] += float(row.get("AWATER_SQMI", 0))

    # Write state gazetteer
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

            # Reverse-lookup USPS code from name
            usps = {v: k for k, v in {
                "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona",
                "AR": "Arkansas", "CA": "California", "CO": "Colorado",
                "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
                "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
                "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
                "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
                "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
                "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
                "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
                "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
                "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
                "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
                "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
                "RI": "Rhode Island", "SC": "South Carolina",
                "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
                "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
                "WA": "Washington", "WV": "West Virginia",
                "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
            }.items()}.get(s["name"], "")

            writer.writerow([
                usps, fips, s["name"],
                s["aland"], s["awater"],
                f"{s['aland_sqmi']:.3f}", f"{s['awater_sqmi']:.3f}",
                f"{lat:+.7f}", f"{lng:+.7f}",
            ])

    print(f"    Saved: {state_path.name} ({line_count(state_path):,} lines)")


def main():
    print("=== Download Census Gazetteer Files ===")

    # Download place and county gazetteers (from Census zip archives)
    for filename, url in GAZETTEER_ZIPS.items():
        dest = RAW_DIR / filename

        if dest.exists():
            print(f"  Skipping (cached): {filename}")
            print(f"    {line_count(dest):,} lines")
            continue

        print(f"  Downloading {filename}...")
        download_and_extract(url, dest)
        print(f"    Saved: {dest.name} ({line_count(dest):,} lines)")

    # Derive state gazetteer from counties
    state_dest = RAW_DIR / STATE_FILE
    counties_path = RAW_DIR / "2023_Gaz_counties_national.txt"

    if state_dest.exists():
        print(f"  Skipping (cached): {STATE_FILE}")
        print(f"    {line_count(state_dest):,} lines")
    elif not counties_path.exists():
        print(f"  ERROR: Cannot derive state gazetteer — counties file missing")
        raise SystemExit(1)
    else:
        derive_state_gazetteer(counties_path, state_dest)

    print("Done.")


if __name__ == "__main__":
    main()
