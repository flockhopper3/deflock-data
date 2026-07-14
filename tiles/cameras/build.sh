#!/usr/bin/env bash
set -euo pipefail

# Builds cameras.pmtiles from camera GeoJSON and (in R2 mode) uploads it.
#
# R2 mode (default — used by CI). Required env:
#   R2_DATA_BUCKET   — bucket holding cameras.geojson.gz (read)
#   R2_TILES_BUCKET  — bucket the built PMTiles are uploaded to (write)
#   R2_ENDPOINT      — R2 S3-compatible endpoint URL
#
# Local mode (preview pipeline/style changes without R2):
#   build.sh --local <cameras.geojson> [output.pmtiles]
#
# Zoom strategy (heatmap → dots):
#   z0–z10  geometry-only raw points — feeds the heatmap layer. Every camera
#           appears at its true location at every zoom, so heat anchors never
#           move between zoom levels. (Tippecanoe clustering re-anchors every
#           zoom independently — that made the heatmap "shift" on zoom.)
#   z11–z14 raw points with all properties — feeds dot + popup layers.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOCAL=0
if [ "${1:-}" = "--local" ]; then
  LOCAL=1
  GEOJSON_FILE="${2:?usage: build.sh --local <cameras.geojson> [output.pmtiles]}"
  OUTPUT_FILE="${3:-cameras-local.pmtiles}"
else
  OUTPUT_FILE="cameras.pmtiles"
  GEOJSON_FILE="cameras.geojson"
  HASH_FILE="cameras.geojson.sha256"

  echo "==> Fetching GeoJSON from R2"
  aws s3 cp "s3://${R2_DATA_BUCKET}/cameras.geojson.gz" "${GEOJSON_FILE}.download" \
    --endpoint-url "${R2_ENDPOINT}"

  # The object may or may not actually be gzip-compressed — check magic bytes.
  if [ "$(head -c 2 "${GEOJSON_FILE}.download" | xxd -p)" = "1f8b" ]; then
    echo "==> Decompressing gzip"
    gunzip -c "${GEOJSON_FILE}.download" > "${GEOJSON_FILE}"
    rm "${GEOJSON_FILE}.download"
  else
    mv "${GEOJSON_FILE}.download" "${GEOJSON_FILE}"
  fi

  echo "==> Checking whether source data changed since last build"
  # Bump BUILD_CONFIG whenever tippecanoe flags change so the skip check
  # doesn't short-circuit a rebuild with unchanged source data.
  BUILD_CONFIG="v3-geomonly-heat"
  NEW_HASH="$(shasum -a 256 "${GEOJSON_FILE}" | cut -d' ' -f1)-${BUILD_CONFIG}"
  OLD_HASH=$(aws s3 cp "s3://${R2_TILES_BUCKET}/${HASH_FILE}" - \
    --endpoint-url "${R2_ENDPOINT}" 2>/dev/null || echo "none")
  if [ "${NEW_HASH}" = "${OLD_HASH}" ]; then
    echo "==> Source unchanged (${NEW_HASH:0:12}…) — skipping build"
    exit 0
  fi
fi

echo "==> Validating GeoJSON"
FEATURE_COUNT=$(jq '.features | length' "${GEOJSON_FILE}")
if [ "${LOCAL}" = "0" ] && [ "${FEATURE_COUNT}" -lt 1000 ]; then
  echo "ERROR: Only ${FEATURE_COUNT} features — expected 100K+. Aborting to protect prod."
  exit 1
fi
echo "    ${FEATURE_COUNT} features found"

HEAT_TMP="$(mktemp -u).heat.pmtiles"
DETAIL_TMP="$(mktemp -u).detail.pmtiles"
trap 'rm -f "${HEAT_TMP}" "${DETAIL_TMP}"' EXIT

echo "==> Tippecanoe pass 1/2: heat range (z0–10, geometry-only, unclustered)"
tippecanoe \
  -o "${HEAT_TMP}" \
  --force \
  --exclude-all \
  --no-feature-limit \
  --no-tile-size-limit \
  --drop-rate=1 \
  --minimum-zoom=0 \
  --maximum-zoom=10 \
  --no-tile-stats \
  --layer=cameras \
  "${GEOJSON_FILE}"

echo "==> Tippecanoe pass 2/2: detail range (z11–14, all properties)"
tippecanoe \
  -o "${DETAIL_TMP}" \
  --force \
  --no-feature-limit \
  --no-tile-size-limit \
  --drop-rate=1 \
  --minimum-zoom=11 \
  --maximum-zoom=14 \
  --no-tile-stats \
  --layer=cameras \
  "${GEOJSON_FILE}"

echo "==> Merging zoom ranges with tile-join"
tile-join -o "${OUTPUT_FILE}" --force --no-tile-size-limit \
  "${HEAT_TMP}" "${DETAIL_TMP}"

echo "==> Verifying tile invariants"
bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}" "${FEATURE_COUNT}"

if [ "${LOCAL}" = "1" ]; then
  echo "==> Done (local). Built ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
  exit 0
fi

FILE_SIZE=$(stat -f%z "${OUTPUT_FILE}" 2>/dev/null || stat -c%s "${OUTPUT_FILE}")
MIN_SIZE=$((10 * 1024 * 1024))
if [ "${FILE_SIZE}" -lt "${MIN_SIZE}" ]; then
  echo "ERROR: Output is only $(du -h "${OUTPUT_FILE}" | cut -f1) — suspiciously small. Aborting."
  exit 1
fi

echo "==> Uploading to Cloudflare R2"
aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_BUCKET}/${OUTPUT_FILE}" \
  --endpoint-url "${R2_ENDPOINT}"
echo "${NEW_HASH}" | aws s3 cp - "s3://${R2_TILES_BUCKET}/${HASH_FILE}" \
  --endpoint-url "${R2_ENDPOINT}"

rm -f "${GEOJSON_FILE}"

echo "==> Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
