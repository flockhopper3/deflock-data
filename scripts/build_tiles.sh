#!/usr/bin/env bash
set -euo pipefail

OUTPUT_FILE="cameras.pmtiles"
GEOJSON_FILE="cameras.geojson"

echo "==> Fetching GeoJSON from R2"
aws s3 cp "s3://${R2_DATA_BUCKET}/cameras.geojson.gz" "${GEOJSON_FILE}" \
  --endpoint-url "${R2_ENDPOINT}"

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
  --cluster-distance=60 \
  --cluster-densest-as-needed \
  --accumulate-attribute=cluster_count:sum \
  --drop-rate=1 \
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

rm -f "${GEOJSON_FILE}"

echo "==> Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
