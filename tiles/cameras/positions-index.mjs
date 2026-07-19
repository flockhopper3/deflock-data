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
  // NOTE: plain `u8.slice()` is NOT safe here when u8 is a Node Buffer: Node
  // overrides Buffer.prototype.slice to behave like subarray() (a view onto
  // the SAME shared, possibly-pooled ArrayBuffer at the SAME nonzero
  // byteOffset) instead of Uint8Array.prototype.slice's copy-to-a-fresh-
  // zero-offset-buffer semantics. Calling the TypedArray base method
  // explicitly forces the real copy this function's alignment guarantee
  // depends on.
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
