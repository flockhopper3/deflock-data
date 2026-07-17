import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeBrand, zoneCode, mountCode, enrichCollection } from './enrich.mjs';

// ── normalizeBrand (ported from DeFlock Maps — behavior must match the app) ──

test('brand: Flock Safety typos and variants merge', () => {
  for (const raw of ['Flock Safety', 'flock saftey', 'FLOCK', 'floc', 'flow safety', 'Flock Safetu']) {
    assert.equal(normalizeBrand(raw), 'Flock Safety', raw);
  }
});

test('brand: Motorola family incl Vigilant and typos', () => {
  for (const raw of ['motorola', 'Motorola Solutions', 'mortorola', 'motorolla', 'Vigilant Solutions']) {
    assert.equal(normalizeBrand(raw), 'Motorola Solutions', raw);
  }
});

test('brand: sub-brand and acronym mappings', () => {
  assert.equal(normalizeBrand('AutoVu'), 'Genetec');
  assert.equal(normalizeBrand('ELSAG'), 'Leonardo');
  assert.equal(normalizeBrand('rektor'), 'Rekor');
  assert.equal(normalizeBrand('PIPS Technology'), 'Neology');
  assert.equal(normalizeBrand('LVT'), 'LiveView Technologies');
  assert.equal(normalizeBrand('lifeview'), 'LiveView Technologies');
  assert.equal(normalizeBrand('UNV'), 'Uniview');
  assert.equal(normalizeBrand('PlateLogiq'), 'PlateLogiq');
});

test('brand: cyber matched anywhere in the string', () => {
  assert.equal(normalizeBrand('SafetyCyberCam'), 'Cyber Secure');
  assert.equal(normalizeBrand('yber secure'), 'Cyber Secure');
});

test('brand: garbage values are unknown (null)', () => {
  for (const raw of ['unknown', 'unk', 'Unknown brand', 'generic', 'other', 'scm?', 'x', '', 'wikidata:Q123', 'Q1080000']) {
    assert.equal(normalizeBrand(raw), null, JSON.stringify(raw));
  }
  assert.equal(normalizeBrand(undefined), null);
  assert.equal(normalizeBrand(null), null);
});

test('brand: unmatched brands keep trimmed raw string', () => {
  assert.equal(normalizeBrand('  Acme Cameras  '), 'Acme Cameras');
});

// ── zone / mount codes ──

test('zoneCode maps the fixed categories', () => {
  assert.equal(zoneCode(undefined), 0);
  assert.equal(zoneCode(''), 0);
  assert.equal(zoneCode('traffic'), 1);
  assert.equal(zoneCode('Town'), 2);
  assert.equal(zoneCode(' parking '), 3);
  assert.equal(zoneCode('street'), 4);
  assert.equal(zoneCode('entrance'), 4);
});

test('mountCode maps the fixed categories', () => {
  assert.equal(mountCode(undefined), 0);
  assert.equal(mountCode('pole'), 1);
  assert.equal(mountCode('Pole'), 1);
  assert.equal(mountCode('wall'), 2);
  assert.equal(mountCode('street_light'), 3);
  assert.equal(mountCode('street_lamp'), 4);
  assert.equal(mountCode('gantry'), 4);
});

// ── enrichCollection ──

function feat(props) {
  return { type: 'Feature', geometry: { type: 'Point', coordinates: [0, 0] }, properties: props };
}

const FC = {
  type: 'FeatureCollection',
  features: [
    feat({ osmId: 1, brand: 'Flock Safety', operator: 'Amarillo Police Department', surveillanceZone: 'traffic', mountType: 'pole' }),
    feat({ osmId: 2, brand: 'flock saftey', operator: 'AMARILLO POLICE DEPARTMENT' }),
    feat({ osmId: 3, brand: 'motorola', operator: 'Amarillo Police Department', surveillanceZone: 'street', mountType: 'street_lamp' }),
    feat({ osmId: 4, brand: 'unknown', operator: 'City of Waco', surveillanceZone: 'town', mountType: 'wall' }),
    feat({ osmId: 5 }),
  ],
};

test('enrichCollection: codes assigned by descending count, 0 for missing', () => {
  const { collection } = enrichCollection(FC);
  const codes = collection.features.map((f) => f.properties);
  // Flock Safety (2) → id 1, Motorola Solutions (1) → id 2
  assert.equal(codes[0].b, 1);
  assert.equal(codes[1].b, 1);
  assert.equal(codes[2].b, 2);
  assert.equal(codes[3].b, 0); // unknown brand
  assert.equal(codes[4].b, 0); // missing brand
  // Amarillo PD (3, case-insensitive) → id 1, City of Waco (1) → id 2
  assert.deepEqual(codes.map((c) => c.o), [1, 1, 1, 2, 0]);
  assert.deepEqual(codes.map((c) => c.z), [1, 0, 4, 2, 0]);
  assert.deepEqual(codes.map((c) => c.m), [1, 0, 4, 2, 0]);
});

test('enrichCollection: original properties preserved, codes are integers', () => {
  const { collection } = enrichCollection(FC);
  const p = collection.features[0].properties;
  assert.equal(p.operator, 'Amarillo Police Department');
  assert.equal(p.brand, 'Flock Safety');
  assert.equal(p.surveillanceZone, 'traffic');
  for (const k of ['b', 'o', 'z', 'm']) {
    assert.ok(Number.isInteger(p[k]), `${k} integer`);
  }
  // input not mutated
  assert.equal(FC.features[0].properties.b, undefined);
});

test('enrichCollection: manifest labels, counts, and ordering', () => {
  const { manifest } = enrichCollection(FC);
  assert.equal(manifest.total, 5);
  assert.deepEqual(manifest.brands, [
    { id: 1, label: 'Flock Safety', count: 2 },
    { id: 2, label: 'Motorola Solutions', count: 1 },
  ]);
  // most common casing wins the operator label
  assert.deepEqual(manifest.operators, [
    { id: 1, label: 'Amarillo Police Department', count: 3 },
    { id: 2, label: 'City of Waco', count: 2 - 1 },
  ]);
  assert.deepEqual(manifest.zones, [
    { id: 1, label: 'traffic', count: 1 },
    { id: 2, label: 'town', count: 1 },
    { id: 3, label: 'parking', count: 0 },
    { id: 4, label: 'other', count: 1 },
  ]);
  assert.deepEqual(manifest.mounts, [
    { id: 1, label: 'pole', count: 1 },
    { id: 2, label: 'wall', count: 1 },
    { id: 3, label: 'street_light', count: 0 },
    { id: 4, label: 'other', count: 1 },
  ]);
});

test('enrichCollection: count ties broken by label sort for determinism', () => {
  const fc = {
    type: 'FeatureCollection',
    features: [feat({ brand: 'Verkada' }), feat({ brand: 'Axon' })],
  };
  const { manifest } = enrichCollection(fc);
  assert.deepEqual(manifest.brands.map((b) => b.label), ['Axon', 'Verkada']);
});
