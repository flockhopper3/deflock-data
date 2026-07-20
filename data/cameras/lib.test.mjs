import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  parseDirection,
  parseDirections,
  transformOverpassToGeoJSON,
  addElementsToFeatures,
  mergeFeatureCollections,
  queryOverpass,
  buildCamerasQuery,
  retryWithBackoff,
  tileIntegrityFailed,
  belowMinimum,
  overpassFailed,
  OVERPASS_ENDPOINTS,
  OVERPASS_USER_AGENT,
  TIMEOUT_MS,
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

  it('handles semicolon-separated numeric (takes first)', () => {
    assert.equal(parseDirection('90;270'), 90);
  });

  it('handles semicolon-separated cardinals (takes first)', () => {
    assert.equal(parseDirection('N;S'), 0);
    assert.equal(parseDirection('E;W'), 90);
  });

  it('handles range notation (returns midpoint)', () => {
    // 338-23: sector from 338° clockwise to 23° -> arc=45°, midpoint=0.5°
    assert.ok(Math.abs(parseDirection('338-23') - 0.5) < 0.1);
    // 48-93: arc=45°, midpoint=70.5°
    assert.ok(Math.abs(parseDirection('48-93') - 70.5) < 0.1);
    // 0-360: full circle, arc=360° -> midpoint=180°
    assert.ok(Math.abs(parseDirection('0-360') - 180) < 0.1);
  });

  it('handles cardinal range notation', () => {
    // WSW(247.5)-ESE(112.5): arc = (112.5-247.5+360)%360 = 225°, midpoint = 247.5+112.5 = 0°
    assert.ok(Math.abs(parseDirection('WSW-ESE') - 0) < 0.1);
  });

  it('handles bound directions (NB/SB/EB/WB)', () => {
    assert.equal(parseDirection('NB'), 0);
    assert.equal(parseDirection('SB'), 180);
    assert.equal(parseDirection('EB'), 90);
    assert.equal(parseDirection('WB'), 270);
  });

  it('handles spelled-out cardinals', () => {
    assert.equal(parseDirection('north'), 0);
    assert.equal(parseDirection('south'), 180);
    assert.equal(parseDirection('northeast'), 45);
    assert.equal(parseDirection('northwest'), 315);
  });

  it('normalizes degrees to 0-359', () => {
    assert.equal(parseDirection('360'), 0);
    assert.ok(Math.abs(parseDirection('400') - 40) < 0.1);
    assert.ok(Math.abs(parseDirection('-10') - 350) < 0.1);
  });

  it('handles comma-separated values', () => {
    assert.equal(parseDirection('95, 95'), 95);
    assert.equal(parseDirection('70, 210, 300'), 70);
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

  it('returns null for unresolvable values', () => {
    assert.equal(parseDirection('forward'), null);
    assert.equal(parseDirection('backward'), null);
    assert.equal(parseDirection('both'), null);
    assert.equal(parseDirection('Flock Raven'), null);
  });
});

describe('parseDirections', () => {
  it('returns all directions from semicolon-separated values', () => {
    assert.deepEqual(parseDirections('90;270'), [90, 270]);
    assert.deepEqual(parseDirections('N;S'), [0, 180]);
    assert.deepEqual(parseDirections('0;90;180;270'), [0, 90, 180, 270]);
  });

  it('returns all directions from comma-separated values', () => {
    assert.deepEqual(parseDirections('70, 210, 300'), [70, 210, 300]);
  });

  it('returns single-element array for simple values', () => {
    assert.deepEqual(parseDirections('180'), [180]);
    assert.deepEqual(parseDirections('NW'), [315]);
  });

  it('returns empty array for empty/undefined', () => {
    assert.deepEqual(parseDirections(''), []);
    assert.deepEqual(parseDirections(undefined), []);
  });

  it('returns empty array for garbage', () => {
    assert.deepEqual(parseDirections('not-a-direction'), []);
  });

  it('filters out unresolvable tokens', () => {
    assert.deepEqual(parseDirections('180;forward;270'), [180, 270]);
  });
});

describe('buildCamerasQuery', () => {
  it('targets the given country', () => {
    const q = buildCamerasQuery('CA');
    assert.ok(q.includes('area["ISO3166-1"="CA"]'));
    assert.ok(q.includes('surveillance:type'));
  });
});

describe('addElementsToFeatures', () => {
  const alprNode = (id, lat, lon) => ({
    type: 'node',
    id,
    lat,
    lon,
    tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR' },
  });

  it('dedupes a camera that appears in two overlapping tiles', () => {
    const map = new Map();
    addElementsToFeatures([alprNode(42, 38.0, -77.0)], map);
    addElementsToFeatures([alprNode(42, 38.0, -77.0), alprNode(43, 39.0, -76.0)], map);
    assert.equal(map.size, 2);
    assert.ok(map.has('node/42'));
    assert.ok(map.has('node/43'));
  });

  it('accumulates features across many batches', () => {
    const map = new Map();
    addElementsToFeatures([alprNode(1, 38, -77), alprNode(2, 38, -76)], map);
    addElementsToFeatures([alprNode(3, 39, -75)], map);
    assert.equal(map.size, 3);
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

  it('outputs directions array for multi-directional cameras', () => {
    const fc = transformOverpassToGeoJSON({
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        {
          type: 'node', id: 1, lat: 38.0, lon: -77.0,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR', 'direction': '90;270' },
        },
        {
          type: 'node', id: 2, lat: 39.0, lon: -76.0,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR', 'direction': '180' },
        },
      ],
    });
    // Multi-direction: direction=first, directions=all
    assert.equal(fc.features[0].properties.direction, 90);
    assert.deepEqual(fc.features[0].properties.directions, [90, 270]);
    // Single direction: direction set, no directions array
    assert.equal(fc.features[1].properties.direction, 180);
    assert.equal(fc.features[1].properties.directions, undefined);
  });

  it('handles range notation in transform', () => {
    const fc = transformOverpassToGeoJSON({
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        {
          type: 'node', id: 1, lat: 38.0, lon: -77.0,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR', 'direction': '338-23' },
        },
      ],
    });
    assert.ok(Math.abs(fc.features[0].properties.direction - 0.5) < 0.1);
  });

  it('sets directionCardinal from first token of multi-value cardinal tag', () => {
    const fc = transformOverpassToGeoJSON({
      version: 0.6,
      generator: 'Overpass API',
      elements: [
        {
          type: 'node', id: 1, lat: 38.0, lon: -77.0,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR', 'direction': 'N;S' },
        },
        {
          type: 'node', id: 2, lat: 39.0, lon: -76.0,
          tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR', 'direction': 'NB' },
        },
      ],
    });
    // "N;S" -> first token "N" is a cardinal -> directionCardinal="N"
    assert.equal(fc.features[0].properties.directionCardinal, 'N');
    // "NB" is a bound direction, not a 16-point cardinal -> no directionCardinal
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

  it('includes the response body head when an endpoint returns non-JSON', async () => {
    let calls = 0;
    const fetchImpl = async () => {
      calls += 1;
      if (calls === 1) return new Response('<?xml version="1.0"?><error/>', { status: 200 });
      return jsonResponse(mockData);
    };
    const messages = [];
    const origError = console.error;
    console.error = (msg) => messages.push(msg);
    try {
      const result = await queryOverpass('[out:json];node(1);out;', fetchImpl);
      assert.deepEqual(result, mockData);
    } finally {
      console.error = origError;
    }
    assert.match(messages.join('\n'), /Non-JSON response from .*<\?xml/);
  });

  it('throws on non-200 status from every endpoint', async () => {
    const fetchImpl = async () => new Response('error', { status: 429 });
    await assert.rejects(
      queryOverpass('[out:json];node(1);out;', fetchImpl),
      /All Overpass endpoints failed/
    );
  });

  it('exports the 4 known endpoints, DeFlock first', () => {
    assert.equal(OVERPASS_ENDPOINTS.length, 4);
    assert.equal(
      OVERPASS_ENDPOINTS[0],
      'https://overpass.deflock.org/api/interpreter'
    );
  });

  it('throws on empty elements by default (allowEmpty=false)', async () => {
    const fetchImpl = async () => jsonResponse({ version: 0.6, elements: [] });
    await assert.rejects(
      queryOverpass('[out:json];node(1);out;', fetchImpl),
      /All Overpass endpoints failed/
    );
  });

  it('returns an empty-elements result when allowEmpty is true', async () => {
    const emptyData = { version: 0.6, elements: [] };
    const fetchImpl = async () => jsonResponse(emptyData);
    const result = await queryOverpass('[out:json];node(1);out;', fetchImpl, { allowEmpty: true });
    assert.deepEqual(result, emptyData);
  });

  it('sends the contact-bearing User-Agent and Accept: application/json on every request', async () => {
    const calls = [];
    const fetchImpl = async (endpoint, init) => {
      calls.push(init);
      return jsonResponse(mockData);
    };
    await queryOverpass('[out:json];node(1);out;', fetchImpl);
    assert.equal(calls[0].headers['User-Agent'], OVERPASS_USER_AGENT);
    assert.match(calls[0].headers['User-Agent'], /dontgetflocked\.com/);
    assert.equal(calls[0].headers['Accept'], 'application/json');
  });

  it('exports OVERPASS_USER_AGENT exactly', () => {
    assert.equal(
      OVERPASS_USER_AGENT,
      'FlockHopper-Data/1.0 (+https://dontgetflocked.com; alerts@dontgetflocked.com)'
    );
  });

  it('exports TIMEOUT_MS as 55 seconds', () => {
    assert.equal(TIMEOUT_MS, 55_000);
  });
});

describe('retryWithBackoff', () => {
  it('returns result on first success', async () => {
    let calls = 0;
    const fn = async () => {
      calls += 1;
      return 'ok';
    };
    const result = await retryWithBackoff(fn, 3, 5);
    assert.equal(result, 'ok');
    assert.equal(calls, 1);
  });

  it('retries on failure and returns on eventual success', async () => {
    let calls = 0;
    const fn = async () => {
      calls += 1;
      if (calls < 3) throw new Error(`fail${calls}`);
      return 'ok';
    };
    const result = await retryWithBackoff(fn, 3, 5);
    assert.equal(result, 'ok');
    assert.equal(calls, 3);
  });

  it('throws last error after all retries exhausted', async () => {
    let calls = 0;
    const fn = async () => {
      calls += 1;
      throw new Error(`fail${calls}`);
    };
    await assert.rejects(retryWithBackoff(fn, 3, 5), /fail3/);
    assert.equal(calls, 3);
  });

  it('applies exponential backoff delays', async () => {
    let calls = 0;
    const timestamps = [];
    const fn = async () => {
      calls += 1;
      timestamps.push(Date.now());
      if (calls < 3) throw new Error('fail');
      return 'ok';
    };
    const start = Date.now();
    const result = await retryWithBackoff(fn, 3, 20);
    assert.equal(result, 'ok');
    assert.equal(calls, 3);
    // attempt 1->2 delay ~20ms, attempt 2->3 delay ~40ms: total >= 60ms
    assert.ok(Date.now() - start >= 55, `expected >= 55ms elapsed, got ${Date.now() - start}ms`);
  });

  it('wraps non-Error throws in Error', async () => {
    const fn = async () => {
      throw 'string error';
    };
    await assert.rejects(retryWithBackoff(fn, 1, 5), /string error/);
  });
});

describe('tileIntegrityFailed', () => {
  const TOL = 0.10; // tile must deliver >= 90% of its probed count

  it('passes when the tile delivers what the probe promised', () => {
    assert.equal(tileIntegrityFailed(5000, 5000, TOL), false);
    assert.equal(tileIntegrityFailed(4800, 5000, TOL), false); // -4%, within tolerance
    assert.equal(tileIntegrityFailed(5200, 5000, TOL), false); // more than expected is fine
  });

  it('fails when the tile returns far fewer features than probed', () => {
    assert.equal(tileIntegrityFailed(0, 5000, TOL), true);     // empty response, should have data
    assert.equal(tileIntegrityFailed(2500, 5000, TOL), true);  // half — partial response
    assert.equal(tileIntegrityFailed(4000, 5000, TOL), true);  // -20%, beyond tolerance
  });

  it('does not flag genuinely empty tiles (probe was 0)', () => {
    assert.equal(tileIntegrityFailed(0, 0, TOL), false);
  });
});

describe('belowMinimum', () => {
  it('is true when the count is under the floor', () => {
    assert.equal(belowMinimum(49_999, 50_000), true);
  });
  it('is false at or above the floor', () => {
    assert.equal(belowMinimum(50_000, 50_000), false);
    assert.equal(belowMinimum(60_000, 50_000), false);
  });
  it('never blocks when the floor is 0 (no baseline yet)', () => {
    assert.equal(belowMinimum(0, 0), false);
    assert.equal(belowMinimum(5, 0), false);
  });
});

describe('overpassFailed', () => {
  it('is false for a normal response with no remark', () => {
    assert.equal(overpassFailed({ elements: [] }), false);
    assert.equal(overpassFailed({ elements: [{ type: 'count', tags: { total: '0' } }] }), false);
  });

  it('detects a server-side query timeout', () => {
    assert.equal(
      overpassFailed({ elements: [], remark: 'runtime error: Query timed out in "query" at line 1' }),
      true
    );
  });

  it('detects out-of-memory and rate-limit remarks', () => {
    assert.equal(overpassFailed({ elements: [], remark: 'runtime error: Out of memory' }), true);
    assert.equal(overpassFailed({ elements: [], remark: 'Too many requests, please wait' }), true);
  });

  it('detects a failure remark even when elements are present (partial result)', () => {
    assert.equal(
      overpassFailed({ elements: [{ type: 'node', id: 1 }], remark: 'runtime error: Query timed out' }),
      true
    );
  });

  it('ignores a benign remark so an unrecognized notice cannot halt the pipeline', () => {
    assert.equal(overpassFailed({ elements: [], remark: 'Data generated at 2026-07-20' }), false);
  });

  it('tolerates null, undefined, and non-string remarks', () => {
    assert.equal(overpassFailed(undefined), false);
    assert.equal(overpassFailed(null), false);
    assert.equal(overpassFailed({}), false);
    assert.equal(overpassFailed({ remark: 42 }), false);
  });
});
