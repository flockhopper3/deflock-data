#!/usr/bin/env bash
set -euo pipefail

# Verifies invariants of a built cameras PMTiles:
#   1. the z0 tile contains every camera exactly once (no clustering, no drops)
#   2. heat-range tiles are geometry-only (no attributes, no point_count)
#   3. detail-range tiles keep full properties (osmId present at z12)
#
# Usage: verify.sh <file.pmtiles> <expected_feature_count>

FILE="${1:?usage: verify.sh <pmtiles> <expected_count>}"
EXPECTED="${2:?usage: verify.sh <pmtiles> <expected_count>}"

fail() { echo "FAIL: $1"; exit 1; }

Z0_JSON=$(tippecanoe-decode "${FILE}" 0 0 0)

Z0_COUNT=$(jq '[.features[].features | length] | add // 0' <<<"${Z0_JSON}")
[ "${Z0_COUNT}" = "${EXPECTED}" ] \
  || fail "z0 has ${Z0_COUNT} features, expected ${EXPECTED} — clustering or dropping present"

CLUSTERED=$(jq '[.features[].features[].properties | select(has("point_count"))] | length' <<<"${Z0_JSON}")
[ "${CLUSTERED}" = "0" ] \
  || fail "${CLUSTERED} z0 features carry point_count — clustered tiles detected"

ATTRS=$(jq '[.features[].features[].properties | length] | add // 0' <<<"${Z0_JSON}")
[ "${ATTRS}" = "0" ] \
  || fail "z0 features carry ${ATTRS} attributes — heat tiles should be geometry-only"

# Detail range: decode the z12 tile containing the first camera and check osmId survives.
read -r LON LAT <<<"$(jq -r '.features[0].features[0].geometry.coordinates | "\(.[0]) \(.[1])"' <<<"${Z0_JSON}")"
read -r TX TY <<<"$(awk -v lon="${LON}" -v lat="${LAT}" 'BEGIN {
  z = 12; n = 2^z; pi = 3.14159265358979; r = lat * pi / 180;
  x = int((lon + 180) / 360 * n);
  y = int((1 - log(sin(r)/cos(r) + 1/cos(r)) / pi) / 2 * n);
  print x, y }')"
WITH_ID=$(tippecanoe-decode "${FILE}" 12 "${TX}" "${TY}" \
  | jq '[.features[].features[].properties | select(has("osmId"))] | length')
[ "${WITH_ID}" -gt 0 ] \
  || fail "z12 tile ${TX}/${TY} has no features with osmId — detail range lost its properties"

echo "OK: ${Z0_COUNT} cameras at z0, geometry-only heat range, properties intact at z12"
