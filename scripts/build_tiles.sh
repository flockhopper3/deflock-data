#!/usr/bin/env bash
set -euo pipefail

DATA_URL="${DATA_URL:-https://data.dontgetflocked.com}"
OUTPUT_FILE="cameras.pmtiles"
GEOJSON_FILE="cameras.geojson"

echo "==> Fetching GeoJSON from ${DATA_URL}"
curl -sSf "${DATA_URL}" -o "${GEOJSON_FILE}"

echo "==> Validating GeoJSON"
FEATURE_COUNT=$(jq '.features | length' "${GEOJSON_FILE}")
if [ "${FEATURE_COUNT}" -lt 1 ]; then
  echo "ERROR: GeoJSON has 0 features — aborting"
  exit 1
fi
echo "    ${FEATURE_COUNT} features found"

echo "==> Running Tippecanoe"
tippecanoe \
  -o "${OUTPUT_FILE}" \
  --force \
  --no-feature-limit \
  --no-tile-size-limit \
  --minimum-zoom=0 \
  --maximum-zoom=14 \
  --drop-densest-as-needed \
  --extend-zooms-if-still-dropping \
  --no-tile-stats \
  --layer=cameras \
  "${GEOJSON_FILE}"

echo "==> Uploading to Cloudflare R2"
aws s3 cp "${OUTPUT_FILE}" "s3://${R2_BUCKET_NAME}/${OUTPUT_FILE}" \
  --endpoint-url "${R2_ENDPOINT}"

aws s3 cp styles/layers.json "s3://${R2_BUCKET_NAME}/styles/layers.json" \
  --endpoint-url "${R2_ENDPOINT}" \
  --content-type "application/json"

echo "==> Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1)) + styles/layers.json"
