#!/usr/bin/env bash
set -euo pipefail

# Builds cameras.pmtiles from the latest camera GeoJSON in R2 and uploads it.
#
# Required env:
#   R2_DATA_BUCKET   — bucket holding cameras.geojson.gz (read)
#   R2_TILES_BUCKET  — bucket the built PMTiles are uploaded to (write)
#   R2_ENDPOINT      — R2 S3-compatible endpoint URL
#
# Zoom strategy (heatmap → dots):
#   z0–z10  clustered points with point_count — feeds the heatmap layer
#   z11–z14 raw points with all properties    — feeds dot + popup layers

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
BUILD_CONFIG="v2-cluster5-keeppos-maxz10"
NEW_HASH="$(shasum -a 256 "${GEOJSON_FILE}" | cut -d' ' -f1)-${BUILD_CONFIG}"
OLD_HASH=$(aws s3 cp "s3://${R2_TILES_BUCKET}/${HASH_FILE}" - \
  --endpoint-url "${R2_ENDPOINT}" 2>/dev/null || echo "none")
if [ "${NEW_HASH}" = "${OLD_HASH}" ]; then
  echo "==> Source unchanged (${NEW_HASH:0:12}…) — skipping build"
  exit 0
fi

echo "==> Validating GeoJSON"
FEATURE_COUNT=$(jq '.features | length' "${GEOJSON_FILE}")
if [ "${FEATURE_COUNT}" -lt 1000 ]; then
  echo "ERROR: Only ${FEATURE_COUNT} features — expected 100K+. Aborting to protect prod."
  exit 1
fi
echo "    ${FEATURE_COUNT} features found"

echo "==> Running Tippecanoe"
tippecanoe \
  -o "${OUTPUT_FILE}" \
  --force \
  --no-feature-limit \
  --no-tile-size-limit \
  --drop-rate=1 \
  --cluster-distance=5 \
  --cluster-maxzoom=10 \
  --keep-point-cluster-position \
  --minimum-zoom=0 \
  --maximum-zoom=14 \
  --no-tile-stats \
  --layer=cameras \
  "${GEOJSON_FILE}"

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
