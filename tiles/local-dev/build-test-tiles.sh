#!/usr/bin/env bash
set -euo pipefail

GEOJSON="../cameras.geojson"
OUTDIR="."

CONFIGS=(
  # name | tippecanoe flags
  # --- Baseline: all features, no limits ---
  "full|-r1 --no-feature-limit --no-tile-size-limit"

  # --- Clustering sweeps: vary distance to find density sweet spot ---
  "clust-d5|--cluster-distance=5 -r1 --no-feature-limit --no-tile-size-limit"
  "clust-d10|--cluster-distance=10 -r1 --no-feature-limit --no-tile-size-limit"
  "clust-d15|--cluster-distance=15 -r1 --no-feature-limit --no-tile-size-limit"
  "clust-d20|--cluster-distance=20 -r1 --no-feature-limit --no-tile-size-limit"
  "clust-d30|--cluster-distance=30 -r1 --no-feature-limit --no-tile-size-limit"

  # --- Drop rate sweeps: gradual thinning at low zooms ---
  "drop-r1.5|--drop-rate=1.5 --no-feature-limit --no-tile-size-limit"
  "drop-r2|--drop-rate=2 --no-feature-limit --no-tile-size-limit"
  "drop-r2.5|--drop-rate=2.5 --no-feature-limit --no-tile-size-limit"

  # --- Combos: cluster + drop rate ---
  "clust-d10-r1.5|--cluster-distance=10 --drop-rate=1.5 --no-feature-limit --no-tile-size-limit"
  "clust-d10-r2|--cluster-distance=10 --drop-rate=2 --no-feature-limit --no-tile-size-limit"
  "clust-d15-r1.5|--cluster-distance=15 --drop-rate=1.5 --no-feature-limit --no-tile-size-limit"
  "clust-d20-r1.5|--cluster-distance=20 --drop-rate=1.5 --no-feature-limit --no-tile-size-limit"

  # --- Tile size cap: let tippecanoe decide what to drop ---
  "cap-500k|--maximum-tile-bytes=500000 -r1"
  "cap-250k|--maximum-tile-bytes=250000 -r1"

  # --- Cluster + tile size cap ---
  "clust-d10-cap500k|--cluster-distance=10 --maximum-tile-bytes=500000 -r1"
)

echo "==> Building ${#CONFIGS[@]} tile configs from $GEOJSON"
echo ""

for entry in "${CONFIGS[@]}"; do
  IFS='|' read -r name flags <<< "$entry"
  outfile="${OUTDIR}/cameras-${name}.pmtiles"
  echo "--- ${name} ---"
  echo "    flags: ${flags}"

  tippecanoe \
    -o "$outfile" \
    --force \
    --minimum-zoom=0 \
    --maximum-zoom=14 \
    --no-tile-stats \
    --layer=cameras \
    $flags \
    "$GEOJSON" 2>&1 | tail -3

  size=$(du -h "$outfile" | cut -f1)
  echo "    output: ${outfile} (${size})"
  echo ""
done

echo "==> Done. Restart server and compare at http://localhost:3000"
