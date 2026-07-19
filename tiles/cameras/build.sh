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
#   R2_DATA_BUCKET   — bucket holding cameras-<cc>-hourly.geojson.gz (read)
#   R2_TILES_BUCKET  — bucket the built PMTiles are uploaded to (write)
#   R2_ENDPOINT      — R2 S3-compatible endpoint URL
# Optional:
#   R2_TILES_MIRROR_BUCKET — second bucket that receives a copy of each
#                            archive (no hash file; skip-check state lives
#                            only in R2_TILES_BUCKET)
#
# Per-country builds run as child processes, NOT functions: bash suppresses
# `set -e` inside any function tested with `if !`, so a mid-build failure in
# a function would fall through to the upload. A child process keeps errexit
# live and the parent only inspects its exit code.
#
# Zoom strategy (heatmap → dots), identical for every archive:
#   z0–z8   geometry-only raw points — heat anchors never move across zooms
#   z9–z14  raw points with all properties — dots, popups, direction cones
#
# Each country also gets a filter companion set, built from the same GeoJSON
# in the same run (ids in the manifest are build-scoped, so the three
# artifacts must always ship together):
#   cameras-<cc>-hourly-filter.pmtiles — same points, integer filter codes
#     b/o/z/m at ALL zooms (heat range carries only the codes; detail range
#     carries full properties plus codes) — attached by the app only when a
#     filter is active
#   cameras-<cc>-hourly-manifest.json  — code→label dictionary powering the
#     filter UI, uploaded gzipped

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COUNTRIES=(us ca)

# Bump BUILD_CONFIG whenever tippecanoe flags, upload destinations, or the set
# of emitted artifacts change so the skip check doesn't short-circuit a rebuild
# with unchanged source data. (v8 adds the per-country positions index.)
BUILD_CONFIG="v8-positions-index"

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
    ca) echo $((80 * 1024)) ;;
    *) return 1 ;;
  esac
}

# Source keys in R2_DATA_BUCKET — the hourly GitHub Actions ingestion's
# outputs. The daily Worker cron's keys (cameras.geojson.gz /
# cameras-ca.geojson.gz) are a separate dataset this pipeline never reads
# or writes.
country_source_key() {
  case "$1" in
    us) echo "cameras-us-hourly.geojson.gz" ;;
    ca) echo "cameras-ca-hourly.geojson.gz" ;;
    *) return 1 ;;
  esac
}

HEAT_TMP="$(mktemp -u).heat.pmtiles"
DETAIL_TMP="$(mktemp -u).detail.pmtiles"
FILTER_HEAT_TMP="$(mktemp -u).filter-heat.pmtiles"
FILTER_DETAIL_TMP="$(mktemp -u).filter-detail.pmtiles"
trap 'rm -f "${HEAT_TMP}" "${DETAIL_TMP}" "${FILTER_HEAT_TMP}" "${FILTER_DETAIL_TMP}"' EXIT

# build_tiles <geojson> <output.pmtiles> — two-pass build + merge
build_tiles() {
  echo "==> Tippecanoe pass 1/2: heat range (z0–8, geometry-only, unclustered)"
  tippecanoe \
    -o "${HEAT_TMP}" \
    --force \
    --exclude-all \
    --no-feature-limit \
    --no-tile-size-limit \
    --drop-rate=1 \
    --buffer=0 \
    --minimum-zoom=0 \
    --maximum-zoom=8 \
    --no-tile-stats \
    --layer=cameras \
    "$1"

  echo "==> Tippecanoe pass 2/2: detail range (z9–14, all properties)"
  tippecanoe \
    -o "${DETAIL_TMP}" \
    --force \
    --no-feature-limit \
    --no-tile-size-limit \
    --drop-rate=1 \
    --buffer=0 \
    --minimum-zoom=9 \
    --maximum-zoom=14 \
    --no-tile-stats \
    --layer=cameras \
    "$1"

  echo "==> Merging zoom ranges with tile-join"
  tile-join -o "$2" --force --no-tile-size-limit \
    "${HEAT_TMP}" "${DETAIL_TMP}"
}

# build_filter_tiles <enriched-geojson> <output.pmtiles> — filter companion.
# Same two-pass structure and drop settings as build_tiles so per-tile feature
# counts match the main archive, but the heat range keeps the four integer
# filter codes instead of stripping all attributes.
build_filter_tiles() {
  echo "==> Tippecanoe filter 1/2: heat range (z0–8, b/o/z/m codes only)"
  tippecanoe \
    -o "${FILTER_HEAT_TMP}" \
    --force \
    --no-feature-limit \
    --no-tile-size-limit \
    --drop-rate=1 \
    --buffer=0 \
    --minimum-zoom=0 \
    --maximum-zoom=8 \
    --no-tile-stats \
    --include=b --include=o --include=z --include=m \
    --layer=cameras \
    "$1"

  echo "==> Tippecanoe filter 2/2: detail range (z9–14, all properties + codes)"
  tippecanoe \
    -o "${FILTER_DETAIL_TMP}" \
    --force \
    --no-feature-limit \
    --no-tile-size-limit \
    --drop-rate=1 \
    --buffer=0 \
    --minimum-zoom=9 \
    --maximum-zoom=14 \
    --no-tile-stats \
    --layer=cameras \
    "$1"

  local FILTER_NAME="${3:-cameras-filter}"
  echo "==> Merging filter zoom ranges with tile-join"
  tile-join -o "$2" --force --no-tile-size-limit \
    -n "${FILTER_NAME}" \
    "${FILTER_HEAT_TMP}" "${FILTER_DETAIL_TMP}"
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

  FILTER_OUTPUT_FILE="${OUTPUT_FILE%.pmtiles}-filter.pmtiles"
  MANIFEST_FILE="${OUTPUT_FILE%.pmtiles}-manifest.json"
  ENRICHED_FILE="${OUTPUT_FILE%.pmtiles}-enriched.geojson.tmp"

  echo "==> Enriching with filter codes + building manifest"
  node "${SCRIPT_DIR}/enrich.mjs" "${GEOJSON_FILE}" "${ENRICHED_FILE}" "${MANIFEST_FILE}"

  FILTER_VERSION=$(jq -r '.version' "${MANIFEST_FILE}")
  build_filter_tiles "${ENRICHED_FILE}" "${FILTER_OUTPUT_FILE}" "cameras-filter ${FILTER_VERSION}"
  rm -f "${ENRICHED_FILE}"

  echo "==> Verifying filter tile invariants"
  bash "${SCRIPT_DIR}/verify-filter.sh" "${FILTER_OUTPUT_FILE}" "${FEATURE_COUNT}"

  INDEX_BIN_FILE="${OUTPUT_FILE%.pmtiles}-index.bin"
  INDEX_JSON_FILE="${OUTPUT_FILE%.pmtiles}-index.json"
  echo "==> Building positions index"
  node "${SCRIPT_DIR}/positions-index.mjs" \
    "${GEOJSON_FILE}" "${INDEX_BIN_FILE}" "${INDEX_JSON_FILE}" "${FEATURE_COUNT}"

  echo "==> Done (local). Built ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1)), ${FILTER_OUTPUT_FILE} ($(du -h "${FILTER_OUTPUT_FILE}" | cut -f1)), ${MANIFEST_FILE}, ${INDEX_BIN_FILE}, ${INDEX_JSON_FILE}"
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
  SOURCE_KEY="$(country_source_key "${CC}")"

  OUTPUT_FILE="cameras-${CC}-hourly.pmtiles"
  GEOJSON_FILE="cameras-${CC}-hourly.geojson"
  HASH_FILE="cameras-${CC}-hourly.geojson.sha256"
  FILTER_OUTPUT_FILE="cameras-${CC}-hourly-filter.pmtiles"
  MANIFEST_FILE="cameras-${CC}-hourly-manifest.json"
  ENRICHED_FILE="cameras-${CC}-hourly-enriched.geojson.tmp"
  INDEX_BIN_FILE="cameras-${CC}-hourly-index.bin"
  INDEX_JSON_FILE="cameras-${CC}-hourly-index.json"

  echo "==> [${CC}] Fetching ${SOURCE_KEY} from R2"
  aws s3 cp "s3://${R2_DATA_BUCKET}/${SOURCE_KEY}" "${GEOJSON_FILE}.download" \
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

  echo "==> [${CC}] Enriching with filter codes + building manifest"
  node "${SCRIPT_DIR}/enrich.mjs" "${GEOJSON_FILE}" "${ENRICHED_FILE}" "${MANIFEST_FILE}"

  FILTER_VERSION=$(jq -r '.version' "${MANIFEST_FILE}")
  build_filter_tiles "${ENRICHED_FILE}" "${FILTER_OUTPUT_FILE}" "cameras-filter ${FILTER_VERSION}"
  rm -f "${ENRICHED_FILE}"

  echo "==> [${CC}] Verifying filter tile invariants"
  bash "${SCRIPT_DIR}/verify-filter.sh" "${FILTER_OUTPUT_FILE}" "${FEATURE_COUNT}"

  # The filter archive is a superset of the main one attribute-wise; anything
  # below the main archive's floor means a truncated build.
  FILTER_SIZE=$(stat -f%z "${FILTER_OUTPUT_FILE}" 2>/dev/null || stat -c%s "${FILTER_OUTPUT_FILE}")
  if [ "${FILTER_SIZE}" -lt "${MIN_BYTES}" ]; then
    echo "ERROR: [${CC}] filter output is only $(du -h "${FILTER_OUTPUT_FILE}" | cut -f1) — suspiciously small. Aborting."
    exit 1
  fi

  echo "==> [${CC}] Building positions index"
  node "${SCRIPT_DIR}/positions-index.mjs" \
    "${GEOJSON_FILE}" "${INDEX_BIN_FILE}" "${INDEX_JSON_FILE}" "${FEATURE_COUNT}"

  # positions-index.mjs already asserts header count == FEATURE_COUNT; re-check the
  # header at the shell layer so a mismatch aborts before any upload.
  INDEX_COUNT=$(node -e 'process.stdout.write(String(require("fs").readFileSync(process.argv[1]).readUInt32LE(8)))' "${INDEX_BIN_FILE}")
  if [ "${INDEX_COUNT}" != "${FEATURE_COUNT}" ]; then
    echo "ERROR: [${CC}] index count ${INDEX_COUNT} != ${FEATURE_COUNT} features — aborting."
    exit 1
  fi

  # Served gzipped with content-encoding, same as the manifest.
  gzip -9 -c "${INDEX_BIN_FILE}" > "${INDEX_BIN_FILE}.gz"
  gzip -9 -c "${INDEX_JSON_FILE}" > "${INDEX_JSON_FILE}.gz"
  echo "    index: $(du -h "${INDEX_BIN_FILE}" | cut -f1) raw, $(du -h "${INDEX_BIN_FILE}.gz" | cut -f1) gzipped"

  # Manifest is served gzipped; TTL policy lives in the serving worker, same
  # as the pmtiles objects (no cache-control metadata set at upload).
  gzip -9 -c "${MANIFEST_FILE}" > "${MANIFEST_FILE}.gz"

  echo "==> [${CC}] Uploading to Cloudflare R2"
  aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_BUCKET}/${OUTPUT_FILE}" \
    --endpoint-url "${R2_ENDPOINT}"
  aws s3 cp "${FILTER_OUTPUT_FILE}" "s3://${R2_TILES_BUCKET}/${FILTER_OUTPUT_FILE}" \
    --endpoint-url "${R2_ENDPOINT}"
  aws s3 cp "${MANIFEST_FILE}.gz" "s3://${R2_TILES_BUCKET}/${MANIFEST_FILE}" \
    --content-encoding gzip --content-type application/json \
    --endpoint-url "${R2_ENDPOINT}"
  aws s3 cp "${INDEX_BIN_FILE}.gz" "s3://${R2_TILES_BUCKET}/${INDEX_BIN_FILE}" \
    --content-encoding gzip --content-type application/octet-stream \
    --endpoint-url "${R2_ENDPOINT}"
  aws s3 cp "${INDEX_JSON_FILE}.gz" "s3://${R2_TILES_BUCKET}/${INDEX_JSON_FILE}" \
    --content-encoding gzip --content-type application/json \
    --endpoint-url "${R2_ENDPOINT}"
  if [ "${CC}" = "us" ]; then
    echo "==> [${CC}] Publishing manifest to data bucket"
    aws s3 cp "${MANIFEST_FILE}" "s3://${R2_DATA_BUCKET}/cameras-manifest.json" \
      --content-type application/json \
      --endpoint-url "${R2_ENDPOINT}"
  fi
  if [ -n "${R2_TILES_MIRROR_BUCKET:-}" ]; then
    echo "==> [${CC}] Mirroring to ${R2_TILES_MIRROR_BUCKET}"
    aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_MIRROR_BUCKET}/${OUTPUT_FILE}" \
      --endpoint-url "${R2_ENDPOINT}"
    aws s3 cp "${FILTER_OUTPUT_FILE}" "s3://${R2_TILES_MIRROR_BUCKET}/${FILTER_OUTPUT_FILE}" \
      --endpoint-url "${R2_ENDPOINT}"
    aws s3 cp "${MANIFEST_FILE}.gz" "s3://${R2_TILES_MIRROR_BUCKET}/${MANIFEST_FILE}" \
      --content-encoding gzip --content-type application/json \
      --endpoint-url "${R2_ENDPOINT}"
    aws s3 cp "${INDEX_BIN_FILE}.gz" "s3://${R2_TILES_MIRROR_BUCKET}/${INDEX_BIN_FILE}" \
      --content-encoding gzip --content-type application/octet-stream \
      --endpoint-url "${R2_ENDPOINT}"
    aws s3 cp "${INDEX_JSON_FILE}.gz" "s3://${R2_TILES_MIRROR_BUCKET}/${INDEX_JSON_FILE}" \
      --content-encoding gzip --content-type application/json \
      --endpoint-url "${R2_ENDPOINT}"
  fi
  echo "${NEW_HASH}" | aws s3 cp - "s3://${R2_TILES_BUCKET}/${HASH_FILE}" \
    --endpoint-url "${R2_ENDPOINT}"

  rm -f "${GEOJSON_FILE}" "${MANIFEST_FILE}.gz" \
    "${INDEX_BIN_FILE}" "${INDEX_BIN_FILE}.gz" "${INDEX_JSON_FILE}" "${INDEX_JSON_FILE}.gz"
  echo "==> [${CC}] Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1)), ${FILTER_OUTPUT_FILE} ($(du -h "${FILTER_OUTPUT_FILE}" | cut -f1)), ${MANIFEST_FILE}, ${INDEX_BIN_FILE}, ${INDEX_JSON_FILE}"
  exit 0
fi

# Guard against typos like --countr that would silently start a full R2 build
if [ "$#" -gt 0 ]; then
  echo "ERROR: unknown argument '$1' (expected --local, --country, or no args)"
  exit 1
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
