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
#
# The z0 tile's coordinates are quantized to the z0 grid (extent 4096), which is
# exactly z12-tile granularity — every decoded coordinate sits ON a z12 tile
# corner, so the point could belong to any of up to four adjacent z12 tiles.
# Also, tippecanoe-decode silently substitutes the nearest existing ancestor
# tile when the requested tile is absent (a "Warning: using tile A/B/C instead"
# on stderr, exit 0, JSON on stdout for the ancestor it actually returned) — so
# a wrong pick could decode a shallower geometry-only tile and produce a
# spurious FAIL. Check all four corner candidates and require that at least
# one of them is a genuine z12 tile (verified via the decoded output's own
# `.properties.zoom`, which reflects the tile actually returned, not the one
# requested) containing osmId.
read -r LON LAT <<<"$(jq -r '.features[0].features[0].geometry.coordinates | "\(.[0]) \(.[1])"' <<<"${Z0_JSON}")"
read -r TX TY <<<"$(awk -v lon="${LON}" -v lat="${LAT}" 'BEGIN {
  z = 12; n = 2^z; pi = 3.14159265358979; r = lat * pi / 180;
  x = int((lon + 180) / 360 * n);
  y = int((1 - log(sin(r)/cos(r) + 1/cos(r)) / pi) / 2 * n);
  print x, y }')"

TX_LO=$((TX - 1)); [ "${TX_LO}" -lt 0 ] && TX_LO=0
TY_LO=$((TY - 1)); [ "${TY_LO}" -lt 0 ] && TY_LO=0

FOUND=0
CANDIDATES_TRIED=""
for CX in "${TX_LO}" "${TX}"; do
  for CY in "${TY_LO}" "${TY}"; do
    CANDIDATES_TRIED="${CANDIDATES_TRIED} ${CX}/${CY}"

    CAND_JSON=$(tippecanoe-decode "${FILE}" 12 "${CX}" "${CY}" 2>/dev/null) || continue
    CAND_ZOOM=$(jq -r '.properties.zoom // empty' <<<"${CAND_JSON}" || true)
    [ "${CAND_ZOOM}" = "12" ] || continue

    CAND_WITH_ID=$(jq '[.features[].features[].properties | select(has("osmId"))] | length' <<<"${CAND_JSON}")
    if [ "${CAND_WITH_ID}" -gt 0 ]; then
      FOUND=1
      break 2
    fi
  done
done

[ "${FOUND}" = "1" ] \
  || fail "no genuine z12 tile among candidates${CANDIDATES_TRIED} has osmId — detail range lost its properties (or tippecanoe-decode substituted a non-z12 ancestor for every candidate)"

echo "OK: ${Z0_COUNT} cameras at z0, geometry-only heat range, properties intact at z12"
