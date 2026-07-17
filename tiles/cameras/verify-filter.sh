#!/usr/bin/env bash
set -euo pipefail

# Verifies invariants of a built cameras *filter* PMTiles (companion archive
# built from enrich.mjs output):
#   1. the z0 tile contains every camera exactly once (no clustering, no drops)
#   2. heat-range features carry exactly the four filter codes b/o/z/m,
#      all integers — nothing more, nothing less
#   3. detail-range tiles keep full properties plus codes (osmId AND b at z12)
#
# Usage: verify-filter.sh <file.pmtiles> <expected_feature_count>

FILE="${1:?usage: verify-filter.sh <pmtiles> <expected_count>}"
EXPECTED="${2:?usage: verify-filter.sh <pmtiles> <expected_count>}"

fail() { echo "FAIL: $1"; exit 1; }

Z0_JSON=$(tippecanoe-decode "${FILE}" 0 0 0)

Z0_COUNT=$(jq '[.features[].features | length] | add // 0' <<<"${Z0_JSON}")
[ "${Z0_COUNT}" = "${EXPECTED}" ] \
  || fail "z0 has ${Z0_COUNT} features, expected ${EXPECTED} — clustering or dropping present"

BAD_KEYS=$(jq '[.features[].features[].properties | select((keys | sort) != ["b","m","o","z"])] | length' <<<"${Z0_JSON}")
[ "${BAD_KEYS}" = "0" ] \
  || fail "${BAD_KEYS} z0 features don't carry exactly b/o/z/m — filter heat tiles must hold the four codes and nothing else"

NON_INT=$(jq '[.features[].features[].properties[] | select((type != "number") or (. != floor))] | length' <<<"${Z0_JSON}")
[ "${NON_INT}" = "0" ] \
  || fail "${NON_INT} z0 code values are not integers"

# Detail range: decode the z12 tile containing the first camera and check that
# osmId AND the b code both survive. Same four-corner candidate scan as
# verify.sh — z0 coordinates are quantized to z12-tile granularity, and
# tippecanoe-decode silently substitutes the nearest existing ancestor when the
# requested tile is absent, so each candidate's own `.properties.zoom` is
# checked before trusting its contents.
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

    CAND_FULL=$(jq '[.features[].features[].properties | select(has("osmId") and has("b"))] | length' <<<"${CAND_JSON}")
    if [ "${CAND_FULL}" -gt 0 ]; then
      FOUND=1
      break 2
    fi
  done
done

[ "${FOUND}" = "1" ] \
  || fail "no genuine z12 tile among candidates${CANDIDATES_TRIED} has osmId+b — detail range lost original properties or filter codes"

echo "OK: ${Z0_COUNT} cameras at z0, codes-only heat range, full properties + codes at z12"
