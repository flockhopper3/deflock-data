#!/usr/bin/env bash
set -euo pipefail

# Verifies invariants of a built boundaries PMTiles:
#   1. z0 world tile has the states layer with ≥ 40 features (some may be
#      reduced away at z0; 40 catches an empty/truncated layer without
#      being brittle about tiny-polygon reduction)
#   2. z6 tile over Chicago has counties layer containing Cook with the
#      spec's exact attribute set
#   3. z12 tile over Chicago is a real z12 tile (not an ancestor fallback)
#      whose municipalities layer contains Chicago with type/county/state
#
# Tile addresses are fixed constants for Chicago (lon -87.6, lat 41.85):
#   z6 → 6/16/23, z12 → 12/1051/1524
#
# Usage: verify.sh <boundaries.pmtiles>

FILE="${1:?usage: verify.sh <boundaries.pmtiles>}"

fail() { echo "FAIL: $1"; exit 1; }

layer_features() { # <decoded-json> <layer>
  jq --arg L "$2" '[.features[] | select(.properties.layer == $L) | .features[]]' <<<"$1"
}

Z0_JSON=$(tippecanoe-decode "${FILE}" 0 0 0)
STATE_COUNT=$(layer_features "${Z0_JSON}" states | jq 'length')
[ "${STATE_COUNT}" -ge 40 ] \
  || fail "z0 states layer has ${STATE_COUNT} features — expected ≥ 40"

Z6_JSON=$(tippecanoe-decode "${FILE}" 6 16 23)
COOK=$(layer_features "${Z6_JSON}" counties \
  | jq '[.[] | select(.properties.name == "Cook" and .properties.state == "IL")] | length')
[ "${COOK}" -ge 1 ] \
  || fail "z6 Chicago tile has no Cook County in counties layer"
COOK_KEYS=$(layer_features "${Z6_JSON}" counties \
  | jq -r '[.[] | select(.properties.name == "Cook" and .properties.state == "IL")][0].properties | keys | sort | join(",")')
[ "${COOK_KEYS}" = "fips,name,state" ] \
  || fail "county attributes are '${COOK_KEYS}' — expected exactly fips,name,state"

Z12_JSON=$(tippecanoe-decode "${FILE}" 12 1051 1524)
Z12_ZOOM=$(jq -r '.properties.zoom' <<<"${Z12_JSON}")
[ "${Z12_ZOOM}" = "12" ] \
  || fail "requested z12 tile decoded as z${Z12_ZOOM} — max zoom truncated?"
CHI=$(layer_features "${Z12_JSON}" municipalities \
  | jq '[.[] | select(.properties.name == "Chicago" and .properties.type == "city"
        and .properties.state == "IL" and .properties.county == "Cook")] | length')
[ "${CHI}" -ge 1 ] \
  || fail "z12 Chicago tile has no Chicago city with expected attributes"

echo "OK: states z0 (${STATE_COUNT}), Cook County z6, Chicago city z12 all verified"
