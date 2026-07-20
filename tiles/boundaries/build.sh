#!/usr/bin/env bash
set -euo pipefail

# Builds boundaries-us.pmtiles — states/counties/municipalities polygon
# layers from Census cartographic boundary files — and uploads it to R2.
#
# Modes:
#   build.sh                  — build + upload (needs R2 env below)
#   build.sh --local [out]    — build only, no R2 (default boundaries-us-local.pmtiles)
#
# R2 mode env:
#   R2_TILES_BUCKET        — destination bucket (write)
#   R2_ENDPOINT            — R2 S3-compatible endpoint URL
# Optional:
#   R2_TILES_MIRROR_BUCKET — second bucket receiving a copy
#   BOUNDARIES_WORK_DIR    — reuse a work dir (downloads are cached there);
#                            defaults to a fresh mktemp dir
#
# Source: Census cartographic boundary files (1:500k, pre-generalized).
# Boundaries change ~annually — bump VINTAGE when the Census publishes a
# new year and dispatch the workflow.
VINTAGE=2024
CB_BASE="https://www2.census.gov/geo/tiger/GENZ${VINTAGE}/shp"
LAYERS_SRC=(state county place cousub)

# Sanity floors — protect prod from a truncated download or a prep bug.
MIN_COUNTIES=3000
MIN_MUNICIPALITIES=30000
MIN_BYTES=$((5 * 1024 * 1024))

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OUTPUT_FILE="boundaries-us.pmtiles"
UPLOAD=1
if [ "${1:-}" = "--local" ]; then
  UPLOAD=0
  OUTPUT_FILE="${2:-boundaries-us-local.pmtiles}"
elif [ -n "${1:-}" ]; then
  echo "ERROR: unknown argument '$1' (expected --local or no arguments)"
  exit 1
fi

WORK_DIR="${BOUNDARIES_WORK_DIR:-$(mktemp -d)}"
mkdir -p "${WORK_DIR}"
STATES_TMP="${WORK_DIR}/states.layer.pmtiles"
COUNTIES_TMP="${WORK_DIR}/counties.layer.pmtiles"
MUNIS_TMP="${WORK_DIR}/munis.layer.pmtiles"

echo "==> Work dir: ${WORK_DIR} (vintage ${VINTAGE})"

for SRC in "${LAYERS_SRC[@]}"; do
  ZIP="cb_${VINTAGE}_us_${SRC}_500k.zip"
  if [ ! -f "${WORK_DIR}/${ZIP}" ]; then
    echo "==> Downloading ${ZIP}"
    curl -fL --retry 3 -o "${WORK_DIR}/${ZIP}.download" "${CB_BASE}/${ZIP}"
    mv "${WORK_DIR}/${ZIP}.download" "${WORK_DIR}/${ZIP}"
  fi
  unzip -o -q "${WORK_DIR}/${ZIP}" -d "${WORK_DIR}/${SRC}"
done

# Installed by `npm ci --prefix tiles/boundaries` — invoke the binary
# directly; npx prefix resolution is unreliable across npm versions.
MAPSHAPER="${SCRIPT_DIR}/node_modules/.bin/mapshaper"

echo "==> Converting shapefiles to GeoJSON"
${MAPSHAPER} "${WORK_DIR}/state/cb_${VINTAGE}_us_state_500k.shp" \
  -o format=geojson "${WORK_DIR}/states.json"
${MAPSHAPER} "${WORK_DIR}/county/cb_${VINTAGE}_us_county_500k.shp" \
  -o format=geojson "${WORK_DIR}/counties.json"
${MAPSHAPER} "${WORK_DIR}/cousub/cb_${VINTAGE}_us_cousub_500k.shp" \
  -o format=geojson "${WORK_DIR}/cousubs.json"

# Places carry no county — assign the county containing the largest share of
# each place's area. County fields are renamed first so the join can't
# collide with the place's own NAME/GEOID.
echo "==> Largest-overlap county join onto places"
${MAPSHAPER} "${WORK_DIR}/county/cb_${VINTAGE}_us_county_500k.shp" \
  -rename-fields co_name=NAME,co_geoid=GEOID \
  -filter-fields co_name,co_geoid \
  -o format=geojson "${WORK_DIR}/counties-join.json"
${MAPSHAPER} "${WORK_DIR}/place/cb_${VINTAGE}_us_place_500k.shp" \
  -join "${WORK_DIR}/counties-join.json" largest-overlap fields=co_name,co_geoid \
  -o format=geojson "${WORK_DIR}/places.json"

echo "==> Shaping tile layers"
node "${SCRIPT_DIR}/prep.mjs" \
  "${WORK_DIR}/states.json" "${WORK_DIR}/counties.json" \
  "${WORK_DIR}/places.json" "${WORK_DIR}/cousubs.json" \
  "${WORK_DIR}/layers"

STATES_N=$(jq '.features | length' "${WORK_DIR}/layers/states.geojson")
COUNTIES_N=$(jq '.features | length' "${WORK_DIR}/layers/counties.geojson")
MUNIS_N=$(jq '.features | length' "${WORK_DIR}/layers/municipalities.geojson")
echo "    states=${STATES_N} counties=${COUNTIES_N} municipalities=${MUNIS_N}"
[ "${STATES_N}" -eq 56 ] || { echo "ERROR: ${STATES_N} states, expected exactly 56. Aborting."; exit 1; }
[ "${COUNTIES_N}" -ge "${MIN_COUNTIES}" ] || { echo "ERROR: only ${COUNTIES_N} counties (< ${MIN_COUNTIES}). Aborting."; exit 1; }
[ "${MUNIS_N}" -ge "${MIN_MUNICIPALITIES}" ] || { echo "ERROR: only ${MUNIS_N} municipalities (< ${MIN_MUNICIPALITIES}). Aborting."; exit 1; }

# One tippecanoe pass per layer (different zoom floors), merged by tile-join.
# Polygons keep the default simplification (the source is pre-generalized);
# --detect-shared-borders keeps adjacent boundaries from separating when
# simplified. --buffer=4 because polygons need edge overlap, unlike the
# camera points' --buffer=0.
tippecanoe_layer() { # <geojson> <out.pmtiles> <layer> <minzoom>
  tippecanoe \
    -o "$2" \
    --force \
    --no-feature-limit \
    --no-tile-size-limit \
    --buffer=4 \
    --detect-shared-borders \
    --minimum-zoom="$4" \
    --maximum-zoom=12 \
    --no-tile-stats \
    --layer="$3" \
    "$1"
}

echo "==> Tippecanoe 1/3: states (z0–12)"
tippecanoe_layer "${WORK_DIR}/layers/states.geojson" "${STATES_TMP}" states 0
echo "==> Tippecanoe 2/3: counties (z2–12)"
tippecanoe_layer "${WORK_DIR}/layers/counties.geojson" "${COUNTIES_TMP}" counties 2
echo "==> Tippecanoe 3/3: municipalities (z5–12)"
tippecanoe_layer "${WORK_DIR}/layers/municipalities.geojson" "${MUNIS_TMP}" municipalities 5

echo "==> Merging layers with tile-join"
tile-join -o "${OUTPUT_FILE}" --force --no-tile-size-limit \
  -n "boundaries-us ${VINTAGE}" \
  "${STATES_TMP}" "${COUNTIES_TMP}" "${MUNIS_TMP}"

echo "==> Verifying tile invariants"
bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}"

FILE_SIZE=$(stat -f%z "${OUTPUT_FILE}" 2>/dev/null || stat -c%s "${OUTPUT_FILE}")
[ "${FILE_SIZE}" -ge "${MIN_BYTES}" ] \
  || { echo "ERROR: output is only $(du -h "${OUTPUT_FILE}" | cut -f1) — suspiciously small. Aborting."; exit 1; }

if [ "${UPLOAD}" = "0" ]; then
  echo "==> Done (local). Built ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
  exit 0
fi

: "${R2_TILES_BUCKET:?R2_TILES_BUCKET required}"
: "${R2_ENDPOINT:?R2_ENDPOINT required}"

echo "==> Uploading to Cloudflare R2"
aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_BUCKET}/${OUTPUT_FILE}" \
  --cache-control "public, max-age=86400" \
  --endpoint-url "${R2_ENDPOINT}"
if [ -n "${R2_TILES_MIRROR_BUCKET:-}" ]; then
  echo "==> Mirroring to ${R2_TILES_MIRROR_BUCKET}"
  aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_MIRROR_BUCKET}/${OUTPUT_FILE}" \
    --cache-control "public, max-age=86400" \
    --endpoint-url "${R2_ENDPOINT}"
fi

echo "==> Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
