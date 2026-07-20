# US Boundaries Tileset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `boundaries-us.pmtiles` — states/counties/municipalities polygon layers with filterable metadata — and a manual GitHub Actions workflow that uploads it to R2.

**Architecture:** A new `tiles/boundaries/` pipeline mirroring `tiles/cameras/`: `build.sh` downloads Census 2024 cartographic boundary shapefiles, mapshaper converts them to GeoJSON and does a largest-overlap county join onto places, `prep.mjs` (pure Node, unit-tested) shapes the three tile layers, tippecanoe builds one archive per layer merged by `tile-join`, `verify.sh` decodes known tiles before upload.

**Tech Stack:** bash, Node 22 (`node --test`), mapshaper (npm, pinned), felt/tippecanoe 2.79.0 (`tippecanoe`, `tile-join`, `tippecanoe-decode`), AWS CLI → Cloudflare R2.

**Spec:** `docs/superpowers/specs/2026-07-19-us-boundaries-tileset-design.md`

## Global Constraints

- Data source: Census cartographic boundary files, vintage **2024**, 1:500k: `cb_2024_us_state_500k`, `cb_2024_us_county_500k`, `cb_2024_us_place_500k`, `cb_2024_us_cousub_500k` from `https://www2.census.gov/geo/tiger/GENZ2024/shp/`.
- Archive: `boundaries-us.pmtiles`, layers `states` (z0–12), `counties` (z2–12), `municipalities` (z5–12).
- Attributes exactly: states `name, abbrev, fips`; counties `name, state, fips`; municipalities `name, type, state, county, fips`. Nothing else survives into tiles.
- Strong-MCD states (cousubs kept): CT ME MA NH RI VT NY NJ PA MI WI MN → STATEFP `09 23 25 33 44 50 36 34 42 26 55 27`.
- Cousub keep rule: strong-MCD state AND `ALAND > 0` AND LSAD NOT in {00, 46, 86} (CB files carry no `FUNCSTAT`; this excludes undefined/consolidated, unorganized territory, and reservation subdivisions) AND derived type NOT in {city, village, borough}.
- Sanity floors before upload: states == 56, counties ≥ 3000, municipalities ≥ 30000, archive ≥ 5 MB.
- Uploads: `R2_TILES_BUCKET` + optional `R2_TILES_MIRROR_BUCKET`, `aws s3 cp --endpoint-url "${R2_ENDPOINT}"`. No skip-hash machinery. **Never** deploy Cloudflare Workers from this repo.
- Workflow: `workflow_dispatch` only. Reuse the tippecanoe cache pattern from `.github/workflows/build-tiles.yml` (cache key `tippecanoe-2.79.0-r2`).
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: prep.mjs pure functions (type derivation, cousub filter, feature shaping)

**Files:**
- Create: `tiles/boundaries/package.json`
- Create: `tiles/boundaries/package-lock.json` (via npm)
- Create: `tiles/boundaries/.gitignore`
- Create: `tiles/boundaries/prep.mjs`
- Test: `tiles/boundaries/prep.test.mjs`

**Interfaces:**
- Consumes: nothing (first task).
- Produces (exported from `prep.mjs`, used by Task 2 and its tests):
  - `STRONG_MCD_STATEFPS: Set<string>`
  - `deriveType(name: string, namelsad: string) => string`
  - `keepCousub(props: object) => boolean`
  - `shapeState(feature) => feature`, `shapeCounty(feature) => feature`, `shapePlace(feature) => feature`, `shapeCousub(feature, countyNames: Map<string,string>) => feature`

- [ ] **Step 1: Scaffold the package with pinned mapshaper**

```bash
cd tiles/boundaries
npm init -y >/dev/null
npm install --save-exact mapshaper
```

Then edit `tiles/boundaries/package.json` to exactly:

```json
{
  "name": "boundaries-tiles",
  "private": true,
  "type": "module",
  "description": "US boundaries tileset prep — see build.sh",
  "dependencies": {
    "mapshaper": "<the exact version npm installed>"
  }
}
```

(Keep the exact mapshaper version npm wrote; just strip the npm-init noise fields.)

Create `tiles/boundaries/.gitignore`:

```
node_modules/
work/
*.pmtiles
```

- [ ] **Step 2: Write failing tests for the pure functions**

Create `tiles/boundaries/prep.test.mjs`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  deriveType,
  keepCousub,
  shapeState,
  shapeCounty,
  shapePlace,
  shapeCousub,
} from './prep.mjs';

// ── deriveType ──────────────────────────────────────────────────────────

test('type: suffix descriptors', () => {
  assert.equal(deriveType('Chicago', 'Chicago city'), 'city');
  assert.equal(deriveType('Scarsdale', 'Scarsdale village'), 'village');
  assert.equal(deriveType('Amherst', 'Amherst town'), 'town');
  assert.equal(deriveType('Radnor', 'Radnor township'), 'township');
  assert.equal(deriveType('Juneau', 'Juneau city and borough'), 'city and borough');
});

test('type: CDP keeps its capitalization', () => {
  assert.equal(deriveType('Bethesda', 'Bethesda CDP'), 'CDP');
});

test('type: prefix descriptors', () => {
  assert.equal(deriveType('Ste. Genevieve', 'Town of Ste. Genevieve'), 'town of');
});

test('type: no descriptor falls back to municipality', () => {
  assert.equal(deriveType('Anchorage', 'Anchorage'), 'municipality');
  assert.equal(deriveType('Anchorage', ''), 'municipality');
  assert.equal(deriveType('Anchorage', undefined), 'municipality');
});

// ── keepCousub ──────────────────────────────────────────────────────────

const cousubProps = (over = {}) => ({
  NAME: 'Radnor',
  NAMELSAD: 'Radnor township',
  STATEFP: '42',
  COUNTYFP: '045',
  FUNCSTAT: 'A',
  ALAND: 35786405,
  ...over,
});

test('cousub: active township in strong-MCD state is kept', () => {
  assert.equal(keepCousub(cousubProps()), true);
});

test('cousub: non-MCD state dropped (VA county subdivisions are statistical)', () => {
  assert.equal(keepCousub(cousubProps({ STATEFP: '51' })), false);
});

test('cousub: nonfunctioning government dropped', () => {
  assert.equal(keepCousub(cousubProps({ FUNCSTAT: 'S' })), false);
});

test('cousub: water-only subdivision dropped', () => {
  assert.equal(keepCousub(cousubProps({ ALAND: 0 })), false);
  assert.equal(keepCousub(cousubProps({ ALAND: undefined })), false);
});

test('cousub: place-covered types dropped (coextensive MI/WI cities, NY villages)', () => {
  assert.equal(keepCousub(cousubProps({ STATEFP: '26', NAME: 'Warren', NAMELSAD: 'Warren city' })), false);
  assert.equal(keepCousub(cousubProps({ STATEFP: '36', NAME: 'Massena', NAMELSAD: 'Massena village' })), false);
  // ...but the same-named NY town survives
  assert.equal(keepCousub(cousubProps({ STATEFP: '36', NAME: 'Massena', NAMELSAD: 'Massena town' })), true);
});

// ── feature shaping ─────────────────────────────────────────────────────

const geom = { type: 'Polygon', coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] };

test('shapeState keeps only name/abbrev/fips', () => {
  const f = shapeState({
    type: 'Feature',
    properties: { NAME: 'Illinois', STUSPS: 'IL', GEOID: '17', STATEFP: '17', ALAND: 1 },
    geometry: geom,
  });
  assert.deepEqual(f.properties, { name: 'Illinois', abbrev: 'IL', fips: '17' });
  assert.equal(f.geometry, geom);
});

test('shapeCounty keeps only name/state/fips', () => {
  const f = shapeCounty({
    type: 'Feature',
    properties: { NAME: 'Cook', NAMELSAD: 'Cook County', STUSPS: 'IL', GEOID: '17031', ALAND: 1 },
    geometry: geom,
  });
  assert.deepEqual(f.properties, { name: 'Cook', state: 'IL', fips: '17031' });
});

test('shapePlace uses the mapshaper-joined county name', () => {
  const f = shapePlace({
    type: 'Feature',
    properties: {
      NAME: 'Chicago', NAMELSAD: 'Chicago city', STUSPS: 'IL', GEOID: '1714000',
      co_name: 'Cook', co_geoid: '17031',
    },
    geometry: geom,
  });
  assert.deepEqual(f.properties, {
    name: 'Chicago', type: 'city', state: 'IL', county: 'Cook', fips: '1714000',
  });
});

test('shapePlace with no county overlap gets county null', () => {
  const f = shapePlace({
    type: 'Feature',
    properties: { NAME: 'Nowhere', NAMELSAD: 'Nowhere CDP', STUSPS: 'AK', GEOID: '0200001' },
    geometry: geom,
  });
  assert.equal(f.properties.county, null);
  assert.equal(f.properties.type, 'CDP');
});

test('shapeCousub resolves county name from STATEFP+COUNTYFP', () => {
  const countyNames = new Map([['42045', 'Delaware']]);
  const f = shapeCousub(
    {
      type: 'Feature',
      properties: {
        NAME: 'Radnor', NAMELSAD: 'Radnor township', STUSPS: 'PA',
        STATEFP: '42', COUNTYFP: '045', GEOID: '4204563624',
      },
      geometry: geom,
    },
    countyNames
  );
  assert.deepEqual(f.properties, {
    name: 'Radnor', type: 'township', state: 'PA', county: 'Delaware', fips: '4204563624',
  });
});

test('shapeCousub with unknown county gets county null', () => {
  const f = shapeCousub(
    {
      type: 'Feature',
      properties: {
        NAME: 'Radnor', NAMELSAD: 'Radnor township', STUSPS: 'PA',
        STATEFP: '42', COUNTYFP: '999', GEOID: '4299963624',
      },
      geometry: geom,
    },
    new Map()
  );
  assert.equal(f.properties.county, null);
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `node --test tiles/boundaries/prep.test.mjs`
Expected: FAIL — `Cannot find module ... prep.mjs` (or named exports missing).

- [ ] **Step 4: Implement the pure functions**

Create `tiles/boundaries/prep.mjs`:

```js
// Shapes Census cartographic-boundary GeoJSON into the three boundary tile
// layers (states, counties, municipalities) with exactly the attributes the
// app filters on. Consumes GeoJSON produced by mapshaper in build.sh:
//   states.json    — cb_<vintage>_us_state_500k, as-is
//   counties.json  — cb_<vintage>_us_county_500k, as-is
//   places.json    — cb_<vintage>_us_place_500k with co_name/co_geoid
//                    largest-overlap county join applied by mapshaper
//   cousubs.json   — cb_<vintage>_us_cousub_500k, as-is
//
// CLI: node prep.mjs <states.json> <counties.json> <places.json> <cousubs.json> <outdir>

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

// States where county subdivisions (MCDs) are functioning municipal
// governments — New England towns plus mid-Atlantic/Midwest townships.
export const STRONG_MCD_STATEFPS = new Set([
  '09', '23', '25', '33', '44', '50', // CT ME MA NH RI VT
  '36', '34', '42', // NY NJ PA
  '26', '55', '27', // MI WI MN
]);

// Municipality kinds incorporated places already cover — a cousub of one of
// these types is the same government appearing in both source files.
const PLACE_COVERED_TYPES = new Set(['city', 'village', 'borough']);

// NAMELSAD is NAME plus the LSAD descriptor ("Springfield city",
// "Bethesda CDP", "Town of X"). The descriptor may prefix or suffix the
// name; whatever remains after removing NAME is the type.
export function deriveType(name, namelsad) {
  if (!namelsad || namelsad === name) return 'municipality';
  let rest = null;
  if (namelsad.startsWith(name)) rest = namelsad.slice(name.length).trim();
  else if (namelsad.endsWith(name)) rest = namelsad.slice(0, namelsad.length - name.length).trim();
  if (!rest) return 'municipality';
  return rest === 'CDP' ? 'CDP' : rest.toLowerCase();
}

export function keepCousub(props) {
  return (
    props.FUNCSTAT === 'A' &&
    STRONG_MCD_STATEFPS.has(props.STATEFP) &&
    (props.ALAND ?? 0) > 0 &&
    !PLACE_COVERED_TYPES.has(deriveType(props.NAME, props.NAMELSAD))
  );
}

const feature = (src, properties) => ({ type: 'Feature', properties, geometry: src.geometry });

export function shapeState(f) {
  const p = f.properties;
  return feature(f, { name: p.NAME, abbrev: p.STUSPS, fips: p.GEOID });
}

export function shapeCounty(f) {
  const p = f.properties;
  return feature(f, { name: p.NAME, state: p.STUSPS, fips: p.GEOID });
}

export function shapePlace(f) {
  const p = f.properties;
  return feature(f, {
    name: p.NAME,
    type: deriveType(p.NAME, p.NAMELSAD),
    state: p.STUSPS,
    county: p.co_name ?? null,
    fips: p.GEOID,
  });
}

export function shapeCousub(f, countyNames) {
  const p = f.properties;
  return feature(f, {
    name: p.NAME,
    type: deriveType(p.NAME, p.NAMELSAD),
    state: p.STUSPS,
    county: countyNames.get(p.STATEFP + p.COUNTYFP) ?? null,
    fips: p.GEOID,
  });
}
```

(`readFileSync`/`writeFileSync`/`mkdirSync`/`join`/`pathToFileURL` are imported now; Task 2 uses them.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `node --test tiles/boundaries/prep.test.mjs`
Expected: PASS, all tests. (Node may warn about unused imports — it won't; unused imports are fine.)

- [ ] **Step 6: Commit**

```bash
git add tiles/boundaries/package.json tiles/boundaries/package-lock.json \
  tiles/boundaries/.gitignore tiles/boundaries/prep.mjs tiles/boundaries/prep.test.mjs
git commit -m "$(cat <<'EOF'
Add boundaries prep: type derivation, cousub filter, feature shaping

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: buildLayers + prep.mjs CLI

**Files:**
- Modify: `tiles/boundaries/prep.mjs` (append)
- Test: `tiles/boundaries/prep.test.mjs` (append)

**Interfaces:**
- Consumes: Task 1 exports (`keepCousub`, `shapeState`, `shapeCounty`, `shapePlace`, `shapeCousub`).
- Produces:
  - `buildLayers({states, counties, places, cousubs}) => {states, counties, municipalities}` — each value a GeoJSON FeatureCollection.
  - CLI `node prep.mjs <states.json> <counties.json> <places.json> <cousubs.json> <outdir>` writing `<outdir>/states.geojson`, `<outdir>/counties.geojson`, `<outdir>/municipalities.geojson` and printing `states=<n> counties=<n> municipalities=<n>` on stdout (Task 4's build.sh parses nothing — it re-counts with jq — but the line is the human log).

- [ ] **Step 1: Write failing test for buildLayers**

Append to `tiles/boundaries/prep.test.mjs`:

```js
// ── buildLayers ─────────────────────────────────────────────────────────

test('buildLayers shapes all three layers and filters cousubs', async () => {
  const { buildLayers } = await import('./prep.mjs');
  const fc = (features) => ({ type: 'FeatureCollection', features });
  const f = (properties) => ({ type: 'Feature', properties, geometry: geom });

  const out = buildLayers({
    states: fc([f({ NAME: 'Pennsylvania', STUSPS: 'PA', GEOID: '42' })]),
    counties: fc([f({ NAME: 'Delaware', STUSPS: 'PA', GEOID: '42045', STATEFP: '42' })]),
    places: fc([
      f({ NAME: 'Chester', NAMELSAD: 'Chester city', STUSPS: 'PA', GEOID: '4213208', co_name: 'Delaware', co_geoid: '42045' }),
    ]),
    cousubs: fc([
      // kept: active township in strong-MCD state
      f({ NAME: 'Radnor', NAMELSAD: 'Radnor township', STUSPS: 'PA', STATEFP: '42', COUNTYFP: '045', GEOID: '4204563624', FUNCSTAT: 'A', ALAND: 1 }),
      // dropped: coextensive city already in places
      f({ NAME: 'Chester', NAMELSAD: 'Chester city', STUSPS: 'PA', STATEFP: '42', COUNTYFP: '045', GEOID: '4204513208', FUNCSTAT: 'A', ALAND: 1 }),
      // dropped: inactive
      f({ NAME: 'Ghost', NAMELSAD: 'Ghost township', STUSPS: 'PA', STATEFP: '42', COUNTYFP: '045', GEOID: '4204500001', FUNCSTAT: 'S', ALAND: 1 }),
    ]),
  });

  assert.equal(out.states.features.length, 1);
  assert.equal(out.counties.features.length, 1);
  assert.deepEqual(
    out.municipalities.features.map((x) => [x.properties.name, x.properties.type, x.properties.county]),
    [['Chester', 'city', 'Delaware'], ['Radnor', 'township', 'Delaware']]
  );
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tiles/boundaries/prep.test.mjs`
Expected: FAIL — `buildLayers` is not exported.

- [ ] **Step 3: Implement buildLayers and the CLI**

Append to `tiles/boundaries/prep.mjs`:

```js
const collection = (features) => ({ type: 'FeatureCollection', features });

export function buildLayers({ states, counties, places, cousubs }) {
  const countyNames = new Map(counties.features.map((f) => [f.properties.GEOID, f.properties.NAME]));
  return {
    states: collection(states.features.map(shapeState)),
    counties: collection(counties.features.map(shapeCounty)),
    municipalities: collection([
      ...places.features.map(shapePlace),
      ...cousubs.features.filter((f) => keepCousub(f.properties)).map((f) => shapeCousub(f, countyNames)),
    ]),
  };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  const [statesIn, countiesIn, placesIn, cousubsIn, outDir] = process.argv.slice(2);
  if (!statesIn || !countiesIn || !placesIn || !cousubsIn || !outDir) {
    console.error('usage: node prep.mjs <states.json> <counties.json> <places.json> <cousubs.json> <outdir>');
    process.exit(1);
  }
  const load = (p) => JSON.parse(readFileSync(p, 'utf8'));
  const layers = buildLayers({
    states: load(statesIn),
    counties: load(countiesIn),
    places: load(placesIn),
    cousubs: load(cousubsIn),
  });
  mkdirSync(outDir, { recursive: true });
  for (const [name, fc] of Object.entries(layers)) {
    writeFileSync(join(outDir, `${name}.geojson`), JSON.stringify(fc));
  }
  console.log(
    `states=${layers.states.features.length} counties=${layers.counties.features.length} municipalities=${layers.municipalities.features.length}`
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tiles/boundaries/prep.test.mjs`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add tiles/boundaries/prep.mjs tiles/boundaries/prep.test.mjs
git commit -m "$(cat <<'EOF'
Add boundaries buildLayers + prep CLI emitting per-layer GeoJSON

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: verify.sh — decoded-tile invariants

**Files:**
- Create: `tiles/boundaries/verify.sh`

**Interfaces:**
- Consumes: a built `boundaries-us.pmtiles` (argument 1). Requires `tippecanoe-decode` + `jq` on PATH.
- Produces: exit 0 on pass, exit 1 with `FAIL: <reason>` on violation. Called by Task 4's build.sh after `tile-join`, before floors/upload.

There is no unit-test harness for this script — it is exercised by the real archive in Task 7 (local build) and in CI. Model it on `tiles/cameras/verify.sh`: `tippecanoe-decode` substitutes the nearest ancestor when a tile is absent, so every deep-tile check asserts the decoded tile's own `.properties.zoom`.

- [ ] **Step 1: Write verify.sh**

Create `tiles/boundaries/verify.sh` (mode 755):

```bash
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
```

```bash
chmod +x tiles/boundaries/verify.sh
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n tiles/boundaries/verify.sh`
Expected: no output, exit 0. (Behavioral verification happens in Task 7 against a real archive.)

- [ ] **Step 3: Commit**

```bash
git add tiles/boundaries/verify.sh
git commit -m "$(cat <<'EOF'
Add boundaries verify.sh: state/county/municipality tile invariants

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: build.sh — download, prep, tippecanoe, floors, upload

**Files:**
- Create: `tiles/boundaries/build.sh`

**Interfaces:**
- Consumes: `prep.mjs` CLI (Task 2), `verify.sh` (Task 3), `npx mapshaper` (Task 1's dependency), `tippecanoe`/`tile-join`, `jq`, `curl`, `unzip`, `aws`.
- Produces: `boundaries-us.pmtiles` uploaded to `s3://${R2_TILES_BUCKET}/boundaries-us.pmtiles` (+ mirror). Modes:
  - `build.sh` — full R2 build+upload (needs `R2_TILES_BUCKET`, `R2_ENDPOINT`; optional `R2_TILES_MIRROR_BUCKET`)
  - `build.sh --local [out.pmtiles]` — build only, no R2, default output `boundaries-us-local.pmtiles`

- [ ] **Step 1: Write build.sh**

Create `tiles/boundaries/build.sh` (mode 755):

```bash
#!/usr/bin/env bash
set -euo pipefail

# Builds boundaries-us.pmtiles — states/counties/municipalities polygon
# layers from Census cartographic boundary files — and uploads it to R2.
#
# Modes:
#   build.sh                  — build + upload (needs R2 env below)
#   build.sh --local [out]    — build only, no R2 (default boundaries-us-local.pmtiles)
#
# R2 mode env:
#   R2_TILES_BUCKET        — destination bucket (write)
#   R2_ENDPOINT            — R2 S3-compatible endpoint URL
# Optional:
#   R2_TILES_MIRROR_BUCKET — second bucket receiving a copy
#   BOUNDARIES_WORK_DIR    — reuse a work dir (downloads are cached there);
#                            defaults to a fresh mktemp dir
#
# Source: Census cartographic boundary files (1:500k, pre-generalized).
# Boundaries change ~annually — bump VINTAGE when the Census publishes a
# new year and dispatch the workflow.
VINTAGE=2024
CB_BASE="https://www2.census.gov/geo/tiger/GENZ${VINTAGE}/shp"
LAYERS_SRC=(state county place cousub)

# Sanity floors — protect prod from a truncated download or a prep bug.
MIN_COUNTIES=3000
MIN_MUNICIPALITIES=30000
MIN_BYTES=$((5 * 1024 * 1024))

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OUTPUT_FILE="boundaries-us.pmtiles"
UPLOAD=1
if [ "${1:-}" = "--local" ]; then
  UPLOAD=0
  OUTPUT_FILE="${2:-boundaries-us-local.pmtiles}"
fi

WORK_DIR="${BOUNDARIES_WORK_DIR:-$(mktemp -d)}"
mkdir -p "${WORK_DIR}"
STATES_TMP="${WORK_DIR}/states.layer.pmtiles"
COUNTIES_TMP="${WORK_DIR}/counties.layer.pmtiles"
MUNIS_TMP="${WORK_DIR}/munis.layer.pmtiles"

echo "==> Work dir: ${WORK_DIR} (vintage ${VINTAGE})"

for SRC in "${LAYERS_SRC[@]}"; do
  ZIP="cb_${VINTAGE}_us_${SRC}_500k.zip"
  if [ ! -f "${WORK_DIR}/${ZIP}" ]; then
    echo "==> Downloading ${ZIP}"
    curl -fL --retry 3 -o "${WORK_DIR}/${ZIP}.download" "${CB_BASE}/${ZIP}"
    mv "${WORK_DIR}/${ZIP}.download" "${WORK_DIR}/${ZIP}"
  fi
  unzip -o -q "${WORK_DIR}/${ZIP}" -d "${WORK_DIR}/${SRC}"
done

# Installed by `npm ci --prefix tiles/boundaries` — invoke the binary
# directly; npx prefix resolution is unreliable across npm versions.
MAPSHAPER="${SCRIPT_DIR}/node_modules/.bin/mapshaper"

echo "==> Converting shapefiles to GeoJSON"
${MAPSHAPER} "${WORK_DIR}/state/cb_${VINTAGE}_us_state_500k.shp" \
  -o format=geojson "${WORK_DIR}/states.json"
${MAPSHAPER} "${WORK_DIR}/county/cb_${VINTAGE}_us_county_500k.shp" \
  -o format=geojson "${WORK_DIR}/counties.json"
${MAPSHAPER} "${WORK_DIR}/cousub/cb_${VINTAGE}_us_cousub_500k.shp" \
  -o format=geojson "${WORK_DIR}/cousubs.json"

# Places carry no county — assign the county containing the largest share of
# each place's area. County fields are renamed first so the join can't
# collide with the place's own NAME/GEOID.
echo "==> Largest-overlap county join onto places"
${MAPSHAPER} "${WORK_DIR}/county/cb_${VINTAGE}_us_county_500k.shp" \
  -rename-fields co_name=NAME,co_geoid=GEOID \
  -filter-fields co_name,co_geoid \
  -o format=geojson "${WORK_DIR}/counties-join.json"
${MAPSHAPER} "${WORK_DIR}/place/cb_${VINTAGE}_us_place_500k.shp" \
  -join "${WORK_DIR}/counties-join.json" largest-overlap fields=co_name,co_geoid \
  -o format=geojson "${WORK_DIR}/places.json"

echo "==> Shaping tile layers"
node "${SCRIPT_DIR}/prep.mjs" \
  "${WORK_DIR}/states.json" "${WORK_DIR}/counties.json" \
  "${WORK_DIR}/places.json" "${WORK_DIR}/cousubs.json" \
  "${WORK_DIR}/layers"

STATES_N=$(jq '.features | length' "${WORK_DIR}/layers/states.geojson")
COUNTIES_N=$(jq '.features | length' "${WORK_DIR}/layers/counties.geojson")
MUNIS_N=$(jq '.features | length' "${WORK_DIR}/layers/municipalities.geojson")
echo "    states=${STATES_N} counties=${COUNTIES_N} municipalities=${MUNIS_N}"
[ "${STATES_N}" -eq 56 ] || { echo "ERROR: ${STATES_N} states, expected exactly 56. Aborting."; exit 1; }
[ "${COUNTIES_N}" -ge "${MIN_COUNTIES}" ] || { echo "ERROR: only ${COUNTIES_N} counties (< ${MIN_COUNTIES}). Aborting."; exit 1; }
[ "${MUNIS_N}" -ge "${MIN_MUNICIPALITIES}" ] || { echo "ERROR: only ${MUNIS_N} municipalities (< ${MIN_MUNICIPALITIES}). Aborting."; exit 1; }

# One tippecanoe pass per layer (different zoom floors), merged by tile-join.
# Polygons keep the default simplification (the source is pre-generalized);
# --detect-shared-borders keeps adjacent boundaries from separating when
# simplified. --buffer=4 because polygons need edge overlap, unlike the
# camera points' --buffer=0.
tippecanoe_layer() { # <geojson> <out.pmtiles> <layer> <minzoom>
  tippecanoe \
    -o "$2" \
    --force \
    --no-feature-limit \
    --no-tile-size-limit \
    --buffer=4 \
    --detect-shared-borders \
    --minimum-zoom="$4" \
    --maximum-zoom=12 \
    --no-tile-stats \
    --layer="$3" \
    "$1"
}

echo "==> Tippecanoe 1/3: states (z0–12)"
tippecanoe_layer "${WORK_DIR}/layers/states.geojson" "${STATES_TMP}" states 0
echo "==> Tippecanoe 2/3: counties (z2–12)"
tippecanoe_layer "${WORK_DIR}/layers/counties.geojson" "${COUNTIES_TMP}" counties 2
echo "==> Tippecanoe 3/3: municipalities (z5–12)"
tippecanoe_layer "${WORK_DIR}/layers/municipalities.geojson" "${MUNIS_TMP}" municipalities 5

echo "==> Merging layers with tile-join"
tile-join -o "${OUTPUT_FILE}" --force --no-tile-size-limit \
  -n "boundaries-us ${VINTAGE}" \
  "${STATES_TMP}" "${COUNTIES_TMP}" "${MUNIS_TMP}"

echo "==> Verifying tile invariants"
bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}"

FILE_SIZE=$(stat -f%z "${OUTPUT_FILE}" 2>/dev/null || stat -c%s "${OUTPUT_FILE}")
[ "${FILE_SIZE}" -ge "${MIN_BYTES}" ] \
  || { echo "ERROR: output is only $(du -h "${OUTPUT_FILE}" | cut -f1) — suspiciously small. Aborting."; exit 1; }

if [ "${UPLOAD}" = "0" ]; then
  echo "==> Done (local). Built ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
  exit 0
fi

: "${R2_TILES_BUCKET:?R2_TILES_BUCKET required}"
: "${R2_ENDPOINT:?R2_ENDPOINT required}"

echo "==> Uploading to Cloudflare R2"
aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_BUCKET}/${OUTPUT_FILE}" \
  --endpoint-url "${R2_ENDPOINT}"
if [ -n "${R2_TILES_MIRROR_BUCKET:-}" ]; then
  echo "==> Mirroring to ${R2_TILES_MIRROR_BUCKET}"
  aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_MIRROR_BUCKET}/${OUTPUT_FILE}" \
    --endpoint-url "${R2_ENDPOINT}"
fi

echo "==> Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
```

```bash
chmod +x tiles/boundaries/build.sh
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n tiles/boundaries/build.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add tiles/boundaries/build.sh
git commit -m "$(cat <<'EOF'
Add boundaries build.sh: Census download → prep → tippecanoe → R2

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: build-boundaries.yml workflow

**Files:**
- Create: `.github/workflows/build-boundaries.yml`

**Interfaces:**
- Consumes: `tiles/boundaries/build.sh` (Task 4), repo secrets `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT` (already exist — used by `build-tiles.yml`).
- Produces: manual-dispatch workflow uploading `boundaries-us.pmtiles`.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/build-boundaries.yml` (tippecanoe steps copied verbatim from `build-tiles.yml`, same cache key so both workflows share the cached binaries):

```yaml
name: Build Boundaries Tiles

# Manual only — Census cartographic boundary files change roughly annually
# (bump VINTAGE in tiles/boundaries/build.sh, then dispatch this).
on:
  workflow_dispatch:

concurrency:
  group: boundaries-build
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: 22

      - name: Install prep dependencies
        run: npm ci --prefix tiles/boundaries

      - name: Run boundaries unit tests
        run: node --test tiles/boundaries/*.test.mjs

      # The apt-packaged tippecanoe is the old Mapbox fork and can't write
      # PMTiles directly, so build felt/tippecanoe once and cache the
      # binaries. Same cache key as build-tiles.yml — the workflows share
      # the cached binaries.
      - name: Cache tippecanoe
        id: cache-tippecanoe
        uses: actions/cache@v4
        with:
          path: ~/tippecanoe-bin
          key: tippecanoe-2.79.0-r2

      - name: Build tippecanoe
        if: steps.cache-tippecanoe.outputs.cache-hit != 'true'
        run: |
          sudo apt-get update
          sudo apt-get install -y build-essential libsqlite3-dev zlib1g-dev
          git clone --depth 1 --branch 2.79.0 https://github.com/felt/tippecanoe.git /tmp/tippecanoe
          make -C /tmp/tippecanoe -j"$(nproc)"
          mkdir -p ~/tippecanoe-bin
          cp /tmp/tippecanoe/tippecanoe /tmp/tippecanoe/tippecanoe-decode /tmp/tippecanoe/tile-join ~/tippecanoe-bin/

      - name: Add tippecanoe to PATH
        run: echo "$HOME/tippecanoe-bin" >> "$GITHUB_PATH"

      - name: Configure AWS CLI for R2
        run: |
          aws configure set aws_access_key_id "${{ secrets.R2_ACCESS_KEY_ID }}"
          aws configure set aws_secret_access_key "${{ secrets.R2_SECRET_ACCESS_KEY }}"
          aws configure set default.region auto

      - name: Build and upload boundaries tiles
        env:
          R2_TILES_BUCKET: flockhopper-tiles
          R2_TILES_MIRROR_BUCKET: deflock-data
          R2_ENDPOINT: ${{ secrets.R2_ENDPOINT }}
        run: bash tiles/boundaries/build.sh
```

- [ ] **Step 2: Validate YAML**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/build-boundaries.yml'))"`
Expected: exit 0, no output. (If PyYAML isn't installed locally, visually diff the tippecanoe steps against `build-tiles.yml` and proceed — GitHub validates on push.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/build-boundaries.yml
git commit -m "$(cat <<'EOF'
Add manual build-boundaries workflow (dispatch-only, annual vintage bumps)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Documentation

**Files:**
- Create: `tiles/boundaries/README.md`
- Modify: `README.md` (root — add a boundaries section after the camera tile-design section)

- [ ] **Step 1: Write tiles/boundaries/README.md**

```markdown
# US Boundaries Tileset

`boundaries-us.pmtiles` — states, counties, and municipalities as polygons
with enough metadata to filter client-side. Built from Census Bureau
cartographic boundary files (1:500k, vintage pinned in `build.sh`),
uploaded to R2 by the manual **Build Boundaries Tiles** workflow, and
served like every other archive at
`https://tiles.dontgetflocked.com/boundaries-us.json` (TileJSON) /
`…/boundaries-us/{z}/{x}/{y}` (tiles).

## Layers & attributes (the filtering contract)

| Layer | Zooms | Attributes |
|-------|-------|------------|
| `states` | z0–12 | `name` ("Illinois"), `abbrev` ("IL"), `fips` ("17") |
| `counties` | z2–12 | `name` ("Cook"), `state` ("IL"), `fips` ("17031") |
| `municipalities` | z5–12 | `name` ("Chicago"), `type` ("city"), `state` ("IL"), `county` ("Cook"), `fips` (place or cousub GEOID) |

- Attributes exist at **all** zooms in each layer's range — filter
  expressions work everywhere. MapLibre overzooms past z12.
- `type` is derived from the Census LSAD descriptor: `city`, `town`,
  `village`, `borough`, `township`, `CDP`, … CDPs are unincorporated
  census-designated places — filter `['!=', ['get', 'type'], 'CDP']` to
  show only real municipal governments.
- `county` on a municipality is the county containing the largest share of
  its area (places can straddle counties); `null` if none.
- The municipalities layer is places nationwide **plus** functioning
  townships/towns in the 12 strong-MCD states (New England, NY, NJ, PA,
  MI, WI, MN). NY villages overlap their surrounding towns by design —
  filter by `type` if you want one level.

## Rebuilding

Boundaries change ~annually. Bump `VINTAGE` in `build.sh` when the Census
publishes a new vintage, then dispatch the **Build Boundaries Tiles**
workflow. Local preview: `bash build.sh --local` (needs tippecanoe + jq;
downloads ~120 MB of shapefiles, cache them with `BOUNDARIES_WORK_DIR`).
```

- [ ] **Step 2: Add the root README section**

In root `README.md`, directly after the "Minimal client example" section's closing code fence (end of the camera docs), add:

```markdown
## Boundaries tileset

`https://tiles.dontgetflocked.com/boundaries-us.json` — US states,
counties, and municipalities as polygon layers with name/state/county/FIPS
attributes at every zoom, for client-side filtering (e.g. outline one
county, or every municipality in a state). Rebuilt manually from Census
cartographic boundary files roughly once a year — see
[`tiles/boundaries/`](tiles/boundaries/).
```

(Adjust placement if that section moved; it belongs with the dataset docs, not inside the camera zoom-strategy details.)

- [ ] **Step 3: Commit**

```bash
git add tiles/boundaries/README.md README.md
git commit -m "$(cat <<'EOF'
Document boundaries tileset: layer contract, rebuild cadence

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: End-to-end local build + real-data spot checks

**Files:** none created (verification only; fixes discovered here get their own commits).

**Interfaces:**
- Consumes: everything above. Requires local `tippecanoe`, `tile-join`, `tippecanoe-decode`, `jq`, `unzip` (already used for the camera local builds).

- [ ] **Step 1: Run unit tests**

Run: `node --test tiles/boundaries/*.test.mjs`
Expected: PASS.

- [ ] **Step 2: Full local build**

```bash
mkdir -p /tmp/boundaries-work
BOUNDARIES_WORK_DIR=/tmp/boundaries-work bash tiles/boundaries/build.sh --local tiles/local-dev/boundaries-us-local.pmtiles
```

Expected: downloads 4 zips (~120 MB total), prints `states=56 counties=32xx municipalities=3xxxx`, three tippecanoe passes, `tile-join`, then `OK: states z0 …` from verify.sh and `==> Done (local)`. Takes several minutes.

If the mapshaper `largest-overlap` join errors (option name drift across mapshaper versions), check `tiles/boundaries/node_modules/.bin/mapshaper -h join` and adjust the flag in `build.sh` — the required behavior is the polygon-to-polygon largest-area-overlap join; commit the fix.

- [ ] **Step 3: Spot-check attribute correctness on real data**

```bash
tippecanoe-decode tiles/local-dev/boundaries-us-local.pmtiles 12 1051 1524 \
  | jq '[.features[] | select(.properties.layer == "municipalities") | .features[].properties] | .[0:5]'
```

Expected: objects with exactly `name`, `type`, `state`, `county`, `fips` — e.g. Chicago as `{"name":"Chicago","type":"city","state":"IL","county":"Cook","fips":"1714000"}`.

Also check a New England town survived (Boston area, z10 tile 10/310/379 ≈ lon −71.0, lat 42.35):

```bash
tippecanoe-decode tiles/local-dev/boundaries-us-local.pmtiles 10 310 379 \
  | jq '[.features[] | select(.properties.layer == "municipalities") | .features[].properties | select(.type == "town" or .type == "city")] | .[0:5]'
```

Expected: Massachusetts towns/cities with `state: "MA"` and county names filled in.

- [ ] **Step 4: Commit any fixes, then confirm clean tree**

Run: `git status`
Expected: clean (the local pmtiles output lands in `tiles/local-dev/` which is fine to leave untracked, or delete it).

---

## After the plan: launch + verification (not code tasks)

1. Push to `main`, dispatch **Build Boundaries Tiles** from the Actions tab.
2. After the run: `curl -sI https://tiles.dontgetflocked.com/boundaries-us.json` — expect 200 TileJSON. If 404, the Worker allowlists archive names; the fix is a one-line addition in the tiles Worker (FLOCKHOPPER DATA RESEARCH repo) — **user deploys it**, never this repo.
3. Spot-check a tile: `curl -s "https://tiles.dontgetflocked.com/boundaries-us/6/16/23" | wc -c` — expect nonzero.
```
