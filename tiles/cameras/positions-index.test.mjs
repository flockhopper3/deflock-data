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
