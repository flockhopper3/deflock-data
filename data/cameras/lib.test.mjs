import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  parseDirection,
  transformOverpassToGeoJSON,
  mergeFeatureCollections,
  queryOverpass,
  buildCamerasQuery,
  OVERPASS_ENDPOINTS,
} from './lib.mjs';

describe('parseDirection', () => {
  it('parses numeric direction', () => {
    assert.equal(parseDirection('180'), 180);
  });

  it('parses cardinal N', () => {
    assert.equal(parseDirection('N'), 0);
  });

  it('parses cardinal SW', () => {
    assert.equal(parseDirection('SW'), 225);
  });

  it('handles semicolon-separated values (takes first)', () => {
    assert.equal(parseDirection('90;270'), 90);
  });

  it('returns null for empty string', () => {
    assert.equal(parseDirection(''), null);
  });

  it('returns null for undefined', () => {
    assert.equal(parseDirection(undefined), null);
  });

  it('returns null for garbage', () => {
    assert.equal(parseDirection('not-a-direction'), null);
  });
});

describe('buildCamerasQuery', () => {
  it('targets the given country', () => {
    const q = buildCamerasQuery('CA');
    assert.ok(q.includes('area["ISO3166-1"="CA"]'));
    assert.ok(q.includes('surveillance:type'));
  });
});

describe('transformOverpassToGeoJSON', () => {
  const minimalNodeResponse = {
    version: 0.6,
    generator: 'Overpass API',
    elements: [
      {
        type: 'node',
        id: 12345,
        lat: 38.89,
        lon: -77.03,
        timestamp: '2025-11-15T00:00:00Z',
        version: 3,
        tags: {
          'man_made': 'surveillance',
          'surveillance:type': 'ALPR',
          'operator': 'Flock Safety',
          'brand': 'Flock',
          'direction': '180',
          'surveillance:zone': 'traffic',
          'camera:mount': 'pole',
          'ref': 'CAM-001',
          'start_date': '2024-06-01',
        },
      },
    ],
  };

  it('transforms a node element to a GeoJSON Feature', () => {
    const fc = transformOverpassToGeoJSON(minimalNodeResponse);

    assert.equal(fc.type, 'FeatureCollection');
    assert.equal(fc.features.length, 1);

    const f = fc.features[0];
    assert.deepEqual(f.geometry.coordinates, [-77.03, 38.89]);
    assert.equal(f.properties.osmId, 12345);
    assert.equal(f.properties.osmType, 'node');
    assert.equal(f.properties.operator, 'Flock Safety');
    assert.equal(f.properties.brand, 'Flock');
    assert.equal(f.properties.direction, 180);
    assert.equal(f.properties.directionCardinal, undefined);
    assert.equal(f.properties.surveillanceZone, 'traffic');
    assert.equal(f.properties.mountType, 'pole');
    assert.equal(f.properties.ref, 'CAM-001');
    assert.equal(f.properties.startDate, '2024-06-01');
    assert.equal(f.properties.osmTimestamp, '2025-11-15T00:00:00Z');
    assert.equal(f.properties.osmVersion, 3);
  });

  it('computes centroid for way elements', () => {
    const wayResponse = {
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        {
          type: 'way',
          id: 99999,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR' },
          nodes: [1, 2],
          timestamp: '2025-01-01T00:00:00Z',
          version: 1,
        },
        { type: 'node', id: 1, lat: 40.0, lon: -74.0 },
        { type: 'node', id: 2, lat: 40.2, lon: -74.2 },
      ],
    };

    const fc = transformOverpassToGeoJSON(wayResponse);
    assert.equal(fc.features.length, 1);

    const coords = fc.features[0].geometry.coordinates;
    assert.ok(Math.abs(coords[0] - -74.1) < 1e-5);
    assert.ok(Math.abs(coords[1] - 40.1) < 1e-5);
  });

  it('skips elements without surveillance:type=ALPR', () => {
    const fc = transformOverpassToGeoJSON({
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        { type: 'node', id: 1, lat: 38.0, lon: -77.0, tags: { 'man_made': 'surveillance' } },
      ],
    });
    assert.equal(fc.features.length, 0);
  });

  it('skips elements without coordinates', () => {
    const fc = transformOverpassToGeoJSON({
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        { type: 'way', id: 1, tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR' }, nodes: [999] },
      ],
    });
    assert.equal(fc.features.length, 0);
  });

  it('sorts features by osmId', () => {
    const tags = { 'man_made': 'surveillance', 'surveillance:type': 'ALPR' };
    const fc = transformOverpassToGeoJSON({
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        { type: 'node', id: 300, lat: 38.0, lon: -77.0, tags },
        { type: 'node', id: 100, lat: 39.0, lon: -76.0, tags },
        { type: 'node', id: 200, lat: 40.0, lon: -75.0, tags },
      ],
    });
    assert.deepEqual(fc.features.map((f) => f.properties.osmId), [100, 200, 300]);
  });

  it('maps manufacturer tag to brand when brand is missing', () => {
    const fc = transformOverpassToGeoJSON({
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        {
          type: 'node', id: 1, lat: 38.0, lon: -77.0,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR', 'manufacturer': 'Vigilant' },
        },
      ],
    });
    assert.equal(fc.features[0].properties.brand, 'Vigilant');
  });

  it('sets directionCardinal only for cardinal strings', () => {
    const fc = transformOverpassToGeoJSON({
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        {
          type: 'node', id: 1, lat: 38.0, lon: -77.0,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR', 'direction': 'SW' },
        },
        {
          type: 'node', id: 2, lat: 39.0, lon: -76.0,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR', 'direction': '270' },
        },
      ],
    });
    assert.equal(fc.features[0].properties.direction, 225);
    assert.equal(fc.features[0].properties.directionCardinal, 'SW');
    assert.equal(fc.features[1].properties.direction, 270);
    assert.equal(fc.features[1].properties.directionCardinal, undefined);
  });
});

describe('mergeFeatureCollections', () => {
  const feature = (id, type = 'node') => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [0, 0] },
    properties: { osmId: id, osmType: type },
  });

  it('merges, dedupes by osmType/osmId, and sorts', () => {
    const merged = mergeFeatureCollections([
      { type: 'FeatureCollection', features: [feature(300), feature(100)] },
      { type: 'FeatureCollection', features: [feature(100), feature(200)] },
    ]);
    assert.deepEqual(merged.features.map((f) => f.properties.osmId), [100, 200, 300]);
  });

  it('keeps same id across different osm types', () => {
    const merged = mergeFeatureCollections([
      { type: 'FeatureCollection', features: [feature(1, 'node'), feature(1, 'way')] },
    ]);
    assert.equal(merged.features.length, 2);
  });
});

describe('queryOverpass', () => {
  const mockData = { version: 0.6, elements: [{ type: 'node', id: 1, lat: 38.9, lon: -77.0 }] };
  const jsonResponse = (data, status = 200) =>
    new Response(JSON.stringify(data), { status });

  it('returns parsed JSON on successful response', async () => {
    const fetchImpl = async () => jsonResponse(mockData);
    const result = await queryOverpass('[out:json];node(1);out;', fetchImpl);
    assert.deepEqual(result, mockData);
  });

  it('falls back to next endpoint on failure', async () => {
    let calls = 0;
    const fetchImpl = async () => {
      calls += 1;
      if (calls === 1) throw new Error('Network error');
      return jsonResponse(mockData);
    };
    const result = await queryOverpass('[out:json];node(1);out;', fetchImpl);
    assert.deepEqual(result, mockData);
    assert.equal(calls, 2);
  });

  it('throws after all endpoints fail', async () => {
    const fetchImpl = async () => {
      throw new Error('fail');
    };
    await assert.rejects(
      queryOverpass('[out:json];node(1);out;', fetchImpl),
      /All Overpass endpoints failed/
    );
  });

  it('throws on non-200 status from every endpoint', async () => {
    const fetchImpl = async () => new Response('error', { status: 429 });
    await assert.rejects(
      queryOverpass('[out:json];node(1);out;', fetchImpl),
      /All Overpass endpoints failed/
    );
  });

  it('exports the 3 known endpoints', () => {
    assert.equal(OVERPASS_ENDPOINTS.length, 3);
  });
});
