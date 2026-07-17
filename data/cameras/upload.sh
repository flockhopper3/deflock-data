#!/usr/bin/env bash
set -euo pipefail

# Uploads the GeoJSON files produced by fetch.mjs to the R2 data bucket.
# fetch.mjs emits hourly-suffixed names (cameras-<cc>-hourly.geojson), so
# uploads land on the -hourly keys and can never touch the daily Worker
# cron's keys (cameras.geojson.gz / cameras-ca.geojson.gz).
#
# Required env:
#   R2_DATA_BUCKET — bucket the datasets are uploaded to (write)
#   R2_ENDPOINT    — R2 S3-compatible endpoint URL
#
# Objects keep the .geojson.gz key but hold uncompressed JSON — the data
# worker has always stored them that way (Cloudflare compresses at the edge)
# and its consumers depend on it. tiles/cameras/build.sh sniffs magic bytes,
# so either encoding works downstream.

OUT_DIR="${1:-out}"

for file in "${OUT_DIR}"/cameras-*-hourly.geojson; do
  name="$(basename "${file}" .geojson)"
  feature_count="$(jq -r --arg n "${name}" '.[$n].featureCount' "${OUT_DIR}/meta.json")"
  last_updated="$(jq -r --arg n "${name}" '.[$n].lastUpdated' "${OUT_DIR}/meta.json")"

  echo "==> Uploading ${name}.geojson.gz (${feature_count} features)"
  aws s3 cp "${file}" "s3://${R2_DATA_BUCKET}/${name}.geojson.gz" \
    --endpoint-url "${R2_ENDPOINT}" \
    --content-type "application/geo+json" \
    --cache-control "public, max-age=3600" \
    --metadata "x-last-updated=${last_updated},x-feature-count=${feature_count},x-source=overpass"
done

echo "==> Done"
