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
