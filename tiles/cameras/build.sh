#!/usr/bin/env bash
set -euo pipefail

# Builds per-country camera PMTiles from GeoJSON in R2 and uploads them.
#
# Modes:
#   build.sh                          — R2: build every country in COUNTRIES,
#                                       isolating failures per country
#   build.sh --country <cc>           — R2: build one country
#   build.sh --local <geojson> [out]  — local preview build, no R2
#
# R2 modes require env:
#   R2_DATA_BUCKET   — bucket holding cameras-<cc>.geojson.gz (read)
#   R2_TILES_BUCKET  — bucket the built PMTiles are uploaded to (write)
#   R2_ENDPOINT      — R2 S3-compatible endpoint URL
#
# Per-country builds run as child processes, NOT functions: bash suppresses
# `set -e` inside any function tested with `if !`, so a mid-build failure in
# a function would fall through to the upload. A child process keeps errexit
# live and the parent only inspects its exit code.
#
# Zoom strategy (heatmap → dots), identical for every archive:
#   z0–z10  geometry-only raw points — heat anchors never move across zooms
#   z11–z14 raw points with all properties — dots, popups, direction cones

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COUNTRIES=(us ca)

# Bump BUILD_CONFIG whenever tippecanoe flags change so the skip check
# doesn't short-circuit a rebuild with unchanged source data.
BUILD_CONFIG="v3-geomonly-heat"

# Floors mirror data/cameras/fetch.mjs; sizes protect prod from truncated
# uploads. CA archive is small (~1K cameras), hence the much lower floor.
country_min_features() {
  case "$1" in
    us) echo 50000 ;;
    ca) echo 300 ;;
    *) return 1 ;;
  esac
}

country_min_bytes() {
  case "$1" in
    us) echo $((10 * 1024 * 1024)) ;;
    ca) echo $((82 * 1024)) ;;
    *) return 1 ;;
  esac
}

HEAT_TMP="$(mktemp -u).heat.pmtiles"
DETAIL_TMP="$(mktemp -u).detail.pmtiles"
trap 'rm -f "${HEAT_TMP}" "${DETAIL_TMP}"' EXIT

# build_tiles <geojson> <output.pmtiles> — two-pass build + merge
build_tiles() {
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
    "$1"

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
    "$1"

  echo "==> Merging zoom ranges with tile-join"
  tile-join -o "$2" --force --no-tile-size-limit \
    "${HEAT_TMP}" "${DETAIL_TMP}"
}

# ── Local mode ──────────────────────────────────────────────────────────
if [ "${1:-}" = "--local" ]; then
  GEOJSON_FILE="${2:?usage: build.sh --local <cameras.geojson> [output.pmtiles]}"
  OUTPUT_FILE="${3:-cameras-local.pmtiles}"

  FEATURE_COUNT=$(jq '.features | length' "${GEOJSON_FILE}")
  echo "    ${FEATURE_COUNT} features found"

  build_tiles "${GEOJSON_FILE}" "${OUTPUT_FILE}"

  echo "==> Verifying tile invariants"
  bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}" "${FEATURE_COUNT}"

  echo "==> Done (local). Built ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
  exit 0
fi

# ── Single-country R2 mode ──────────────────────────────────────────────
if [ "${1:-}" = "--country" ]; then
  CC="${2:?usage: build.sh --country <cc>}"
  if ! MIN_FEATURES="$(country_min_features "${CC}")"; then
    echo "ERROR: unknown country '${CC}' (known: ${COUNTRIES[*]})"
    exit 1
  fi
  MIN_BYTES="$(country_min_bytes "${CC}")"

  OUTPUT_FILE="cameras-${CC}.pmtiles"
  GEOJSON_FILE="cameras-${CC}.geojson"
  HASH_FILE="cameras-${CC}.geojson.sha256"

  echo "==> [${CC}] Fetching GeoJSON from R2"
  aws s3 cp "s3://${R2_DATA_BUCKET}/cameras-${CC}.geojson.gz" "${GEOJSON_FILE}.download" \
    --endpoint-url "${R2_ENDPOINT}"

  # The object may or may not actually be gzip-compressed — check magic bytes.
  if [ "$(head -c 2 "${GEOJSON_FILE}.download" | xxd -p)" = "1f8b" ]; then
    echo "==> [${CC}] Decompressing gzip"
    gunzip -c "${GEOJSON_FILE}.download" > "${GEOJSON_FILE}"
    rm "${GEOJSON_FILE}.download"
  else
    mv "${GEOJSON_FILE}.download" "${GEOJSON_FILE}"
  fi

  echo "==> [${CC}] Checking whether source data changed since last build"
  NEW_HASH="$(shasum -a 256 "${GEOJSON_FILE}" | cut -d' ' -f1)-${BUILD_CONFIG}"
  OLD_HASH=$(aws s3 cp "s3://${R2_TILES_BUCKET}/${HASH_FILE}" - \
    --endpoint-url "${R2_ENDPOINT}" 2>/dev/null || echo "none")
  if [ "${NEW_HASH}" = "${OLD_HASH}" ]; then
    echo "==> [${CC}] Source unchanged (${NEW_HASH:0:12}…) — skipping build"
    exit 0
  fi

  echo "==> [${CC}] Validating GeoJSON"
  FEATURE_COUNT=$(jq '.features | length' "${GEOJSON_FILE}")
  if [ "${FEATURE_COUNT}" -lt "${MIN_FEATURES}" ]; then
    echo "ERROR: [${CC}] only ${FEATURE_COUNT} features — expected ${MIN_FEATURES}+. Aborting to protect prod."
    exit 1
  fi
  echo "    ${FEATURE_COUNT} features found"

  build_tiles "${GEOJSON_FILE}" "${OUTPUT_FILE}"

  echo "==> [${CC}] Verifying tile invariants"
  bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}" "${FEATURE_COUNT}"

  FILE_SIZE=$(stat -f%z "${OUTPUT_FILE}" 2>/dev/null || stat -c%s "${OUTPUT_FILE}")
  if [ "${FILE_SIZE}" -lt "${MIN_BYTES}" ]; then
    echo "ERROR: [${CC}] output is only $(du -h "${OUTPUT_FILE}" | cut -f1) — suspiciously small. Aborting."
    exit 1
  fi

  echo "==> [${CC}] Uploading to Cloudflare R2"
  aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_BUCKET}/${OUTPUT_FILE}" \
    --endpoint-url "${R2_ENDPOINT}"
  echo "${NEW_HASH}" | aws s3 cp - "s3://${R2_TILES_BUCKET}/${HASH_FILE}" \
    --endpoint-url "${R2_ENDPOINT}"

  rm -f "${GEOJSON_FILE}"
  echo "==> [${CC}] Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
  exit 0
fi

# ── Main R2 mode: every country, isolating failures ────────────────────
FAILED=()
for CC in "${COUNTRIES[@]}"; do
  if ! bash "${BASH_SOURCE[0]}" --country "${CC}"; then
    echo "!! [${CC}] build failed — continuing with remaining countries"
    FAILED+=("${CC}")
  fi
done

if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "ERROR: failed countries: ${FAILED[*]}"
  exit 1
fi
echo "==> All countries built or skipped successfully"
