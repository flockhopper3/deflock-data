# Camera Positions Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit a slim binary positions index (`cameras-<cc>-hourly-index.bin`) + JSON sidecar per country, built alongside the tile archives from the same snapshot, so the maps client counts viewport cameras in-memory instead of via `queryRenderedFeatures`.

**Architecture:** A new `tiles/cameras/positions-index.mjs` reuses `enrich.mjs`'s brand ranking (so `brandId` == the filter manifest's `b` code), serializes a little-endian columnar binary, and writes a paired JSON sidecar. `build.sh` calls it per country right after the existing enrich/filter step and uploads both artifacts to R2 alongside the manifest. Serving the `.bin` at the edge needs a Cloudflare Worker route change (separate repo, user-deployed) — this plan delivers a paste-ready snippet, not a deploy.

**Tech Stack:** Node 22 ESM (`.mjs`), `node:test` + `node:assert/strict`, `node:crypto`, `Buffer`/`DataView`/typed arrays. Bash + AWS CLI (R2) for the pipeline. No new dependencies.

## Global Constraints

- Binary format v1: little-endian, columnar, **no padding**. Header 16B (`"FHIX"` | `uint32 version=1` | `uint32 count N` | `uint32 reserved=0`), then `int32[N]` lat µ°, `int32[N]` lng µ°, `uint8[N]` brandId. Total size == `16 + 9N`.
- Records sorted by **latitude then longitude ascending**. No delta encoding.
- Latitude/longitude are `Math.round(coord × 1e6)` microdegrees; `coordinates = [lng, lat]`.
- `brandId`: `0` = unknown/missing; `1..254` = the manifest `b` code (brand ranked by camera count); `255` = `"other"`, materialized **only** when the manifest has > 254 brands (ranks 255+ collapse into it).
- Sidecar `brands[i]` is the display name for `brandId = i`; `brands[0] = "unknown"`; dense array; `"other"` appended only in the overflow case.
- Sidecar `build` == `sha256(input snapshot bytes).slice(0,16)` — the identical value `enrich.mjs` stamps into the manifest `version`.
- Artifacts are gzipped at upload with `Content-Encoding: gzip`, `.bin` as `application/octet-stream`, `.json` as `application/json` — same pattern as `cameras-<cc>-hourly-manifest.json`.
- Do **not** change the tile archives, filter tileset, or manifest. Purely additive.
- Bump `build.sh`'s `BUILD_CONFIG` so unchanged sources emit the index on the next run.
- Reuse `enrich.mjs`'s `enrichCollection`; never re-derive brand ranking independently.

---

### Task 1: Core index module — build data, encode, decode, validate

**Files:**
- Create: `tiles/cameras/positions-index.mjs`
- Test: `tiles/cameras/positions-index.test.mjs`

**Interfaces:**
- Consumes: `enrichCollection(fc)` from `tiles/cameras/enrich.mjs` → `{ collection, manifest }` where `collection.features[i].properties.b` is the brand code (`0` = unknown, `1..M` by descending count) and `manifest.brands` is `[{id, label, count}]` (id `1..M`, sorted by count desc, ties by label).
- Produces:
  - `MAGIC = 'FHIX'`, `FORMAT_VERSION = 1` (exported constants)
  - `buildIndexData(fc) → { records: Array<{lat:number, lng:number, brandId:number}>, brands: string[] }` — records sorted by lat then lng ascending; `brands` dense with `brands[0] === 'unknown'`.
  - `encodeIndex(records) → Buffer` — v1 layout, length `16 + 9*records.length`.
  - `decodeIndex(bytes) → { magic, version, count, lat: Int32Array, lng: Int32Array, brand: Uint8Array }` — reads the layout the way the client will.
  - `validateIndex(buf, brands, expectedCount) → void` — throws on any invariant violation.
  - `buildSidecar(count, build, brands) → object` — the JSON sidecar body.

- [ ] **Step 1: Write the failing tests**

Create `tiles/cameras/positions-index.test.mjs`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MAGIC, FORMAT_VERSION, buildIndexData, encodeIndex, decodeIndex,
  validateIndex, buildSidecar,
} from './positions-index.mjs';

function feat(brand, lng, lat) {
  return { type: 'Feature', geometry: { type: 'Point', coordinates: [lng, lat] }, properties: brand ? { brand } : {} };
}

// Flock(2) → id 1, Motorola(1) → id 2, unknown/missing → 0
const FC = {
  type: 'FeatureCollection',
  features: [
    feat('Flock Safety', -73.968285, 40.785091),
    feat('flock saftey', -122.419416, 37.774929),
    feat('motorola', -0.5, -0.25),
    feat('unknown', 10.1, 20.2),
    feat(null, -179.9, -89.9),
  ],
};

test('buildIndexData: brandId matches manifest b, dense brands table', () => {
  const { records, brands } = buildIndexData(FC);
  assert.equal(records.length, 5);
  assert.deepEqual(brands, ['unknown', 'Flock Safety', 'Motorola Solutions']);
  // brandId 0..2 all < brands.length
  for (const r of records) assert.ok(r.brandId < brands.length);
});

test('buildIndexData: records sorted by lat then lng ascending', () => {
  const { records } = buildIndexData(FC);
  for (let i = 1; i < records.length; i++) {
    const prev = records[i - 1], cur = records[i];
    assert.ok(prev.lat < cur.lat || (prev.lat === cur.lat && prev.lng <= cur.lng),
      `record ${i} out of order`);
  }
});

test('buildIndexData: microdegree rounding, coords within bounds', () => {
  const { records } = buildIndexData(FC);
  // the -73.968285 / 40.785091 point
  const nyc = records.find((r) => r.lat === 40785091);
  assert.ok(nyc, 'NYC lat rounded to microdegrees');
  assert.equal(nyc.lng, -73968285);
  for (const r of records) {
    assert.ok(r.lat >= -90e6 && r.lat <= 90e6, `lat ${r.lat} in range`);
    assert.ok(r.lng >= -180e6 && r.lng <= 180e6, `lng ${r.lng} in range`);
  }
});

test('encodeIndex/decodeIndex: header + columns round-trip', () => {
  const { records } = buildIndexData(FC);
  const buf = encodeIndex(records);
  assert.equal(buf.length, 16 + 9 * records.length);
  const d = decodeIndex(buf);
  assert.equal(d.magic, MAGIC);
  assert.equal(d.version, FORMAT_VERSION);
  assert.equal(d.count, records.length);
  for (let i = 0; i < records.length; i++) {
    assert.equal(d.lat[i], records[i].lat);
    assert.equal(d.lng[i], records[i].lng);
    assert.equal(d.brand[i], records[i].brandId);
  }
});

test('decodeIndex works on a Buffer read back from bytes (alignment)', () => {
  const { records } = buildIndexData(FC);
  const buf = encodeIndex(records);
  // simulate a pooled, non-zero-offset Buffer slice
  const padded = Buffer.concat([Buffer.from([0, 0, 0]), buf]).subarray(3);
  const d = decodeIndex(padded);
  assert.equal(d.magic, MAGIC);
  assert.equal(d.count, records.length);
});

test('brand overflow: >254 brands collapse to 255="other"', () => {
  const features = [];
  for (let i = 0; i < 300; i++) {
    const label = 'Brand' + String(i).padStart(3, '0'); // unique, count 1, passes through normalizeBrand
    features.push(feat(label, i * 0.001, i * 0.001));
  }
  const { records, brands } = buildIndexData({ type: 'FeatureCollection', features });
  assert.equal(brands.length, 256);            // unknown + 254 real + "other"
  assert.equal(brands[255], 'other');
  assert.equal(brands[254], 'Brand253');       // rank 254 keeps its slot
  assert.equal(brands[1], 'Brand000');         // rank 1
  // feature i has lat==lng==round(i*1000); rank(BrandNNN)=NNN+1
  const tail = records.find((r) => r.lat === 299000); // Brand299 → id 300 → collapsed
  assert.equal(tail.brandId, 255);
  const last254 = records.find((r) => r.lat === 253000); // Brand253 → id 254
  assert.equal(last254.brandId, 254);
});

test('validateIndex: passes a good buffer, throws on count mismatch', () => {
  const { records, brands } = buildIndexData(FC);
  const buf = encodeIndex(records);
  assert.doesNotThrow(() => validateIndex(buf, brands, records.length));
  assert.throws(() => validateIndex(buf, brands, records.length + 1), /count/);
});

test('buildSidecar: shape matches the contract', () => {
  const s = buildSidecar(5, 'abc123def456', ['unknown', 'Flock Safety']);
  assert.equal(s.version, FORMAT_VERSION);
  assert.equal(s.count, 5);
  assert.equal(s.build, 'abc123def456');
  assert.deepEqual(s.brands, ['unknown', 'Flock Safety']);
  assert.equal(typeof s.note, 'string');
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test tiles/cameras/positions-index.test.mjs`
Expected: FAIL — `Cannot find module './positions-index.mjs'` (module not created yet).

- [ ] **Step 3: Write the module**

Create `tiles/cameras/positions-index.mjs`:

```js
// Builds a slim binary positions index (+ JSON sidecar) from a cameras GeoJSON,
// paired with the filter manifest so the maps client can count viewport cameras
// in-memory instead of via queryRenderedFeatures.
//
// Binary format v1 (little-endian, columnar, no padding):
//   header 16B: "FHIX" | uint32 version=1 | uint32 count N | uint32 reserved=0
//   int32[N] latitude  (round(lat*1e6))  sorted by lat then lng ascending
//   int32[N] longitude (round(lng*1e6))
//   uint8[N] brandId   (0=unknown, 1..254 = manifest 'b' code, 255="other")
//
// brandId reuses enrich.mjs's ranking so it matches cameras-<cc>-hourly-manifest.json.
//
// CLI: node positions-index.mjs <input.geojson> <bin-out> <json-out> <expected_count>

import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, statSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { enrichCollection } from './enrich.mjs';

export const MAGIC = 'FHIX';
export const FORMAT_VERSION = 1;
const MAX_REAL_BRAND_ID = 254; // 255 reserved for the "other" bucket
const OTHER_BRAND_ID = 255;

// Build sorted records + the dense brand-label table from a FeatureCollection.
// Reuses enrichCollection so brandId === the manifest 'b' code by construction.
export function buildIndexData(fc) {
  const { collection, manifest } = enrichCollection(fc);
  const labels = manifest.brands.map((brand) => brand.label); // labels[0] is brand id 1

  const brands = labels.length > MAX_REAL_BRAND_ID
    ? ['unknown', ...labels.slice(0, MAX_REAL_BRAND_ID), 'other']
    : ['unknown', ...labels];

  const records = collection.features.map((f) => {
    const [lng, lat] = f.geometry.coordinates;
    const b = f.properties.b; // 0..manifest.brands.length
    return {
      lat: Math.round(lat * 1e6),
      lng: Math.round(lng * 1e6),
      brandId: b <= MAX_REAL_BRAND_ID ? b : OTHER_BRAND_ID,
    };
  });

  records.sort((r1, r2) => r1.lat - r2.lat || r1.lng - r2.lng);
  return { records, brands };
}

// Serialize sorted records into the v1 binary layout.
export function encodeIndex(records) {
  const n = records.length;
  const buf = Buffer.alloc(16 + 9 * n);
  buf.write(MAGIC, 0, 'ascii');
  buf.writeUInt32LE(FORMAT_VERSION, 4);
  buf.writeUInt32LE(n, 8);
  buf.writeUInt32LE(0, 12);

  let off = 16;
  for (const r of records) { buf.writeInt32LE(r.lat, off); off += 4; }
  for (const r of records) { buf.writeInt32LE(r.lng, off); off += 4; }
  for (const r of records) { buf.writeUInt8(r.brandId, off); off += 1; }
  return buf;
}

// Read the layout back the way the client will (DataView header + typed-array
// columns). Copies into a fresh zero-offset ArrayBuffer so Int32Array views are
// aligned regardless of how the input Buffer was allocated (Node may pool small
// Buffers at odd byte offsets).
export function decodeIndex(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  // Force a real copy: Node overrides Buffer.prototype.slice to alias (like
  // subarray), so `u8.slice().buffer` would leak a pooled Buffer's shared 8KB
  // ArrayBuffer at the wrong offset. The base TypedArray slice always copies.
  const buf = Uint8Array.prototype.slice.call(u8).buffer;
  const dv = new DataView(buf);
  const magic = String.fromCharCode(...new Uint8Array(buf, 0, 4));
  const version = dv.getUint32(4, true);
  const count = dv.getUint32(8, true);
  const lat = new Int32Array(buf, 16, count);
  const lng = new Int32Array(buf, 16 + count * 4, count);
  const brand = new Uint8Array(buf, 16 + count * 8, count);
  return { magic, version, count, lat, lng, brand };
}

// Assert the binary holds together against the sidecar's brand table and the
// feature count fed to the tile build. Throws on any violation.
export function validateIndex(buf, brands, expectedCount) {
  const { magic, version, count, brand } = decodeIndex(buf);
  if (magic !== MAGIC) throw new Error(`bad magic ${JSON.stringify(magic)} (expected ${MAGIC})`);
  if (version !== FORMAT_VERSION) throw new Error(`bad version ${version}`);
  if (count !== expectedCount) {
    throw new Error(`count ${count} != expected ${expectedCount} (features fed to the tile build)`);
  }
  if (buf.length !== 16 + 9 * count) {
    throw new Error(`size ${buf.length} != ${16 + 9 * count} (expected 16 + 9N)`);
  }
  for (let i = 0; i < brand.length; i++) {
    if (brand[i] >= brands.length) {
      throw new Error(`brandId ${brand[i]} >= brands.length ${brands.length} at index ${i}`);
    }
  }
}

// Assemble the JSON sidecar body.
export function buildSidecar(count, build, brands) {
  return {
    version: FORMAT_VERSION,
    count,
    build,
    brands,
    note:
      "brandId 0=unknown; 1..254 by camera count (== filter manifest 'b'); "
      + "255='other' only when >254 brands. Columns are little-endian native typed arrays.",
  };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  const [input, binOut, jsonOut, expectedRaw] = process.argv.slice(2);
  if (!input || !binOut || !jsonOut || expectedRaw === undefined) {
    console.error('usage: node positions-index.mjs <input.geojson> <bin-out> <json-out> <expected_count>');
    process.exit(1);
  }
  const expectedCount = Number(expectedRaw);
  if (!Number.isInteger(expectedCount) || expectedCount < 0) {
    console.error(`invalid expected_count ${JSON.stringify(expectedRaw)}`);
    process.exit(1);
  }

  const bytes = readFileSync(input);
  const fc = JSON.parse(bytes.toString('utf8'));
  const { records, brands } = buildIndexData(fc);
  const buf = encodeIndex(records);

  // Same snapshot hash enrich.mjs stamps into the manifest 'version', so the
  // client can pair this index with the tileset/manifest it was built from.
  const build = createHash('sha256').update(bytes).digest('hex').slice(0, 16);

  validateIndex(buf, brands, expectedCount);

  writeFileSync(binOut, buf);
  writeFileSync(jsonOut, JSON.stringify(buildSidecar(records.length, build, brands)));

  console.log(
    `positions index: ${records.length} cameras, ${brands.length} brand slots, `
    + `build ${build} — ${binOut} ${statSync(binOut).size}B raw, `
    + `${jsonOut} ${statSync(jsonOut).size}B`
  );
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test tiles/cameras/positions-index.test.mjs`
Expected: PASS — all 8 tests green.

- [ ] **Step 5: Commit**

```bash
git add tiles/cameras/positions-index.mjs tiles/cameras/positions-index.test.mjs
git commit -m "Add positions-index module: binary v1 encode/decode + brand mapping

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: CLI end-to-end — produce files and pass the acceptance snippet

**Files:**
- Uses: `tiles/cameras/positions-index.mjs` (CLI main from Task 1)

**Interfaces:**
- Consumes: `node positions-index.mjs <input.geojson> <bin-out> <json-out> <expected_count>` — writes the two files, exits non-zero on count mismatch or invalid args.
- Produces: nothing new; this task verifies the CLI contract `build.sh` depends on.

- [ ] **Step 1: Write a fixture + acceptance script**

Create the fixture and run the CLI in a temp dir (nothing committed to the repo):

```bash
TMP="$(mktemp -d)"
cat > "$TMP/sample.geojson" <<'EOF'
{"type":"FeatureCollection","features":[
{"type":"Feature","geometry":{"type":"Point","coordinates":[-73.968285,40.785091]},"properties":{"brand":"Flock Safety"}},
{"type":"Feature","geometry":{"type":"Point","coordinates":[-122.419416,37.774929]},"properties":{"brand":"motorola"}},
{"type":"Feature","geometry":{"type":"Point","coordinates":[10.1,20.2]},"properties":{}}
]}
EOF
node tiles/cameras/positions-index.mjs "$TMP/sample.geojson" "$TMP/out.bin" "$TMP/out.json" 3
```

Expected: prints `positions index: 3 cameras, 3 brand slots, build <hash> — .../out.bin 43B raw, .../out.json ...B` (43 == 16 + 9×3).

- [ ] **Step 2: Run the acceptance snippet against the produced files**

```bash
node -e '
const fs = require("fs");
const dir = process.argv[1];
// Copy into a fresh, zero-offset ArrayBuffer — a small file's Buffer is pooled
// (non-zero byteOffset into the shared 8KB pool), so .buffer directly would
// misread. Mirrors the spec's `new Uint8Array(arrayBuffer).buffer` construction.
const buf = new Uint8Array(fs.readFileSync(dir + "/out.bin")).buffer;
const dv = new DataView(buf);
const magic = String.fromCharCode(...new Uint8Array(buf, 0, 4));
const n = dv.getUint32(8, true);
const lat = new Int32Array(buf, 16, n);
const lng = new Int32Array(buf, 16 + n * 4, n);
const brand = new Uint8Array(buf, 16 + n * 8, n);
const side = JSON.parse(fs.readFileSync(dir + "/out.json", "utf8"));
if (magic !== "FHIX") throw new Error("bad magic " + magic);
if (n !== side.count || n !== 3) throw new Error("count mismatch");
for (let i = 0; i < n; i++) {
  if (lat[i] < -90e6 || lat[i] > 90e6) throw new Error("lat out of range");
  if (lng[i] < -180e6 || lng[i] > 180e6) throw new Error("lng out of range");
  if (brand[i] >= side.brands.length) throw new Error("brandId out of range");
}
// the NYC point survived at expected coords
if (![...lat].includes(40785091) || ![...lng].includes(-73968285)) throw new Error("NYC coord missing");
console.log("ACCEPTANCE OK:", n, "cameras, brands", JSON.stringify(side.brands));
' "$TMP"
```

Expected: `ACCEPTANCE OK: 3 cameras, brands ["unknown","Flock Safety","Motorola Solutions"]`

- [ ] **Step 3: Verify the count-mismatch guard fails loudly**

```bash
node tiles/cameras/positions-index.mjs "$TMP/sample.geojson" "$TMP/bad.bin" "$TMP/bad.json" 99; echo "exit=$?"
```

Expected: throws an Error mentioning `count 3 != expected 99` and `exit=1` (non-zero). Then clean up: `rm -rf "$TMP"`.

- [ ] **Step 4: Commit**

No code change in this task (verification only). If Step 2 or 3 surfaced a bug, fix it in `positions-index.mjs`, re-run Task 1's tests, and commit:

```bash
git add tiles/cameras/positions-index.mjs
git commit -m "Fix positions-index CLI per acceptance round-trip

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

(If nothing needed fixing, skip the commit — proceed to Task 3.)

---

### Task 3: Wire the index into `build.sh` (both modes) + bump BUILD_CONFIG

**Files:**
- Modify: `tiles/cameras/build.sh` (`BUILD_CONFIG` at line 48; `--local` block ~176–190; `--country` file vars ~204–209, filter-verify tail ~268, upload block ~275–297, cleanup ~301, done echo ~302)

**Interfaces:**
- Consumes: `node positions-index.mjs <geojson> <bin> <json> <expected_count>` (Task 1/2).
- Produces: `cameras-<cc>-hourly-index.bin` + `.json` uploaded to `R2_TILES_BUCKET` (and mirror) per country; local-mode index files next to the local pmtiles.

- [ ] **Step 1: Bump the build config so unchanged sources re-emit**

In `tiles/cameras/build.sh`, replace the `BUILD_CONFIG` line and its comment (lines ~44–48):

```bash
# Bump BUILD_CONFIG whenever tippecanoe flags, upload destinations, or the set
# of emitted artifacts change so the skip check doesn't short-circuit a rebuild
# with unchanged source data. (v8 adds the per-country positions index.)
BUILD_CONFIG="v8-positions-index"
```

- [ ] **Step 2: Add index generation + upload to `--country` mode**

In the `--country` block, add the two filenames next to the existing file vars (after the `ENRICHED_FILE=` line, ~line 209):

```bash
  INDEX_BIN_FILE="cameras-${CC}-hourly-index.bin"
  INDEX_JSON_FILE="cameras-${CC}-hourly-index.json"
```

Then, immediately after the filter-size check block (after the `fi` that closes the `FILTER_SIZE` guard, ~line 268) and before the `gzip -9 -c "${MANIFEST_FILE}"` line, insert:

```bash
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
```

In the upload block (after the `aws s3 cp "${MANIFEST_FILE}.gz" ...` upload, ~line 281), add the index uploads:

```bash
  aws s3 cp "${INDEX_BIN_FILE}.gz" "s3://${R2_TILES_BUCKET}/${INDEX_BIN_FILE}" \
    --content-encoding gzip --content-type application/octet-stream \
    --endpoint-url "${R2_ENDPOINT}"
  aws s3 cp "${INDEX_JSON_FILE}.gz" "s3://${R2_TILES_BUCKET}/${INDEX_JSON_FILE}" \
    --content-encoding gzip --content-type application/json \
    --endpoint-url "${R2_ENDPOINT}"
```

In the mirror block (inside the `if [ -n "${R2_TILES_MIRROR_BUCKET:-}" ]` body, after the mirrored manifest upload, ~line 296), add:

```bash
    aws s3 cp "${INDEX_BIN_FILE}.gz" "s3://${R2_TILES_MIRROR_BUCKET}/${INDEX_BIN_FILE}" \
      --content-encoding gzip --content-type application/octet-stream \
      --endpoint-url "${R2_ENDPOINT}"
    aws s3 cp "${INDEX_JSON_FILE}.gz" "s3://${R2_TILES_MIRROR_BUCKET}/${INDEX_JSON_FILE}" \
      --content-encoding gzip --content-type application/json \
      --endpoint-url "${R2_ENDPOINT}"
```

Update the cleanup `rm -f` line (~line 301) to remove the index temporaries too:

```bash
  rm -f "${GEOJSON_FILE}" "${MANIFEST_FILE}.gz" \
    "${INDEX_BIN_FILE}" "${INDEX_BIN_FILE}.gz" "${INDEX_JSON_FILE}" "${INDEX_JSON_FILE}.gz"
```

And extend the final done echo (~line 302) to mention the index:

```bash
  echo "==> [${CC}] Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1)), ${FILTER_OUTPUT_FILE} ($(du -h "${FILTER_OUTPUT_FILE}" | cut -f1)), ${MANIFEST_FILE}, ${INDEX_BIN_FILE}, ${INDEX_JSON_FILE}"
```

- [ ] **Step 3: Add index generation to `--local` mode**

In the `--local` block, after the `bash "${SCRIPT_DIR}/verify-filter.sh" ...` line (~line 188) and before the `echo "==> Done (local)..."` line, insert:

```bash
  INDEX_BIN_FILE="${OUTPUT_FILE%.pmtiles}-index.bin"
  INDEX_JSON_FILE="${OUTPUT_FILE%.pmtiles}-index.json"
  echo "==> Building positions index"
  node "${SCRIPT_DIR}/positions-index.mjs" \
    "${GEOJSON_FILE}" "${INDEX_BIN_FILE}" "${INDEX_JSON_FILE}" "${FEATURE_COUNT}"
```

And update the local done echo (~line 190) to append `, ${INDEX_BIN_FILE}, ${INDEX_JSON_FILE}` inside its message.

- [ ] **Step 4: Syntax-check the script**

Run: `bash -n tiles/cameras/build.sh`
Expected: no output, exit 0 (script parses).

- [ ] **Step 5: Re-run the module tests and CLI acceptance to confirm the contract build.sh calls is intact**

Run: `node --test tiles/cameras/positions-index.test.mjs`
Expected: PASS.

Then re-run Task 2 Steps 1–2 (fixture → CLI → acceptance snippet).
Expected: `ACCEPTANCE OK: 3 cameras, ...`.

(Full `build.sh --local`/`--country` runs need `tippecanoe` + R2 creds; the real end-to-end gate is a `workflow_dispatch` of **Build Tiles** in CI, covered in Task 4's verification notes.)

- [ ] **Step 6: Commit**

```bash
git add tiles/cameras/build.sh
git commit -m "build.sh: emit + upload per-country positions index; bump BUILD_CONFIG v8

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Docs + Worker route handoff (serving is user-deployed)

**Files:**
- Create: `docs/superpowers/handoffs/2026-07-18-tiles-worker-bin-route.md`
- Modify: `README.md` (hourly outputs section — add the index endpoints, flag serving pending)

**Interfaces:**
- Consumes: nothing.
- Produces: a paste-ready Worker route the user applies in the `flockhopper-tiles` Worker repo, plus README documentation of the new endpoints.

- [ ] **Step 1: Write the Worker handoff snippet**

Create `docs/superpowers/handoffs/2026-07-18-tiles-worker-bin-route.md`:

```markdown
# flockhopper-tiles Worker: serve `*-index.bin` — handoff

The build pipeline (this repo) now uploads two objects per country to the
`flockhopper-tiles` bucket:

- `cameras-<cc>-hourly-index.bin`  — stored gzipped, `Content-Encoding: gzip`,
  `Content-Type: application/octet-stream`
- `cameras-<cc>-hourly-index.json` — stored gzipped, `Content-Encoding: gzip`,
  `Content-Type: application/json`

The Worker currently routes `*.pmtiles` (unpacked to `z/x/y`) and the manifest
JSON. It does **not** yet serve `.bin`. Add a passthrough route that streams the
stored R2 object with its metadata intact — same CORS + cache/etag policy as the
manifest. **You deploy this** (per the no-Cloudflare-deploys rule); the pipeline
side only uploads.

Reference route (adapt to the actual Worker structure — the key detail is
forwarding `httpMetadata.contentEncoding`, setting the etag, and CORS):

```js
// Inside the Worker fetch handler, alongside the manifest passthrough:
if (key.endsWith('-index.bin') || key.endsWith('-index.json')) {
  const obj = await env.TILES.get(key, {
    // hourly-fresh conditional read, same as manifest/pmtiles swaps
    onlyIf: request.headers,
  });
  if (!obj) return new Response('Not found', { status: 404 });
  const headers = new Headers();
  obj.writeHttpMetadata(headers);          // Content-Type + Content-Encoding: gzip
  headers.set('etag', obj.httpEtag);
  headers.set('Access-Control-Allow-Origin', '*');
  headers.set('Cache-Control', 'public, max-age=300, s-maxage=3600');
  if (obj.body === null) return new Response(null, { status: 304, headers });
  return new Response(obj.body, { headers });
}
```

Notes:
- `writeHttpMetadata` re-emits the stored `Content-Encoding: gzip`, so the
  browser/undici transparently inflates on `fetch().arrayBuffer()` — clients see
  the raw `16 + 9N` bytes. Do **not** decompress in the Worker.
- If the Worker already has a generic "any other key → R2 passthrough" branch
  that forwards `Content-Encoding` + etag + CORS, no change is needed; confirm it
  covers `.bin` and does not force `Content-Type`.

## Acceptance after deploy

```
curl -sI https://tiles.dontgetflocked.com/cameras-us-hourly-index.bin
# → 200, content-encoding: gzip, content-type: application/octet-stream,
#   access-control-allow-origin: *, etag present
```

Then the client acceptance snippet in
`docs/superpowers/specs/2026-07-18-camera-positions-index-design.md` must
round-trip against both US and CA `.bin` + `.json`.
```

- [ ] **Step 2: Document the new endpoints in the README**

In `README.md`, update the hourly row / outputs description to list the index artifacts. Find the hourly table row (the `| Hourly (new app) | ... |` line) and append to its "served at" cell:

```
 + `…-index.bin` / `…-index.json` (positions index for in-app viewport counting; **edge serving pending the Worker `.bin` route** — see `docs/superpowers/handoffs/2026-07-18-tiles-worker-bin-route.md`)
```

(Keep the existing cell content; add the index note after it. Match the surrounding table's tone.)

- [ ] **Step 3: Verify the docs render and links resolve**

Run: `ls docs/superpowers/handoffs/2026-07-18-tiles-worker-bin-route.md && grep -n "index.bin" README.md`
Expected: the handoff file exists and README shows the new `index.bin` mention.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/handoffs/2026-07-18-tiles-worker-bin-route.md README.md
git commit -m "Document positions index endpoints + tiles-worker .bin route handoff

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Post-merge CI acceptance (manual, after the branch merges)**

Once merged and a **Build Tiles** run has completed (chained off a fetch, or `gh workflow run "Build Tiles"`):
1. Confirm the objects exist: `aws s3 ls s3://flockhopper-tiles/ --endpoint-url "$R2_ENDPOINT" | grep index` → four keys (`us`/`ca` × `.bin`/`.json`).
2. After the user deploys the Worker `.bin` route, run the client acceptance snippet against `https://tiles.dontgetflocked.com/cameras-us-hourly-index.bin` and `…-ca-hourly-index.bin` — header count == sidecar `count`, coords in range, known cameras at expected coords.
3. Confirm `build` in each sidecar matches `version` in the sibling `cameras-<cc>-hourly-manifest.json`.

---

## Self-Review

**Spec coverage:**
- Binary format v1 (header + columns, LE, no padding) → Task 1 `encodeIndex` + tests. ✓
- Sorted lat then lng → Task 1 `buildIndexData` + sort-order test. ✓
- brandId mapping (0 unknown / 1..254 / 255 other) reusing manifest ranking → Task 1 `buildIndexData` + overflow test. ✓
- JSON sidecar (version/count/build/brands/note), `build` == manifest version hash → Task 1 `buildSidecar` + CLI main. ✓
- Paired artifacts per country, same build run, additive → Task 3 build.sh wiring. ✓
- Gzip + content-encoding upload, mirror, same as manifest → Task 3 upload/mirror steps. ✓
- BUILD_CONFIG bump → Task 3 Step 1. ✓
- Validation (count triple-equality, brandId < brands.length, size 16+9N, log sizes) → Task 1 `validateIndex` + CLI log; Task 3 shell re-check. ✓
- Acceptance snippet round-trip → Task 2 Steps 1–2, Task 4 Step 5. ✓
- Serving worker change (separate repo, user-deployed) → Task 4 handoff doc. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**Type consistency:** `buildIndexData → {records, brands}`, `encodeIndex(records) → Buffer`, `decodeIndex(bytes) → {magic,version,count,lat,lng,brand}`, `validateIndex(buf, brands, expectedCount)`, `buildSidecar(count, build, brands)` — names and shapes used identically across Tasks 1–3. CLI arg order `<input> <bin> <json> <expected_count>` matches `build.sh` calls in Task 3. ✓
