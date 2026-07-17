import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildSeedTiles,
  tileSelector,
  countTile,
  planLeafTiles,
  fetchTileInto,
  fetchCountryArea,
  subtractForeign,
  fetchAllCameras,
} from './tiled-fetch.mjs';

const jsonResponse = (data, status = 200) => new Response(JSON.stringify(data), { status });

const alprNode = (id, lat = 40, lon = -90) => ({
  type: 'node',
  id,
  lat,
  lon,
  tags: { 'man_made': 'surveillance', 'surveillance:type': 'ALPR' },
});

const dummyFeature = (osmId) => ({
  type: 'Feature',
  geometry: { type: 'Point', coordinates: [0, 0] },
  properties: { osmId, osmType: 'node' },
});

/** Pull the "(s,w,n,e)" bbox tuple out of a tile query's node/way filter. */
function bboxFromQuery(query) {
  const m = query.match(/\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\)/);
  return m ? m.slice(1, 5).join(',') : null;
}

describe('buildSeedTiles', () => {
  it('produces the continental 4x6 grid plus AK/HI/PR (27 tiles)', () => {
    const tiles = buildSeedTiles();
    assert.equal(tiles.length, 27);
  });

  it('spans the full continental bounds with no gaps', () => {
    const tiles = buildSeedTiles();
    const continental = tiles.filter((t) => t.s >= 24 && t.n <= 50 && t.w >= -125 && t.e <= -66);
    assert.equal(continental.length, 24);
    assert.equal(Math.min(...continental.map((t) => t.s)), 24);
    assert.equal(Math.max(...continental.map((t) => t.n)), 50);
    assert.equal(Math.min(...continental.map((t) => t.w)), -125);
    assert.equal(Math.max(...continental.map((t) => t.e)), -66);
  });

  it('includes Alaska, Hawaii, and Puerto Rico/USVI seed tiles', () => {
    const tiles = buildSeedTiles();
    assert.ok(tiles.some((t) => t.s === 51 && t.w === -180 && t.n === 72 && t.e === -129), 'Alaska tile missing');
    assert.ok(tiles.some((t) => t.s === 18 && t.w === -161 && t.n === 23 && t.e === -154), 'Hawaii tile missing');
    assert.ok(
      tiles.some((t) => t.s === 17.5 && t.w === -67.5 && t.n === 18.7 && t.e === -64.5),
      'Puerto Rico/USVI tile missing'
    );
  });

  it('places a known camera-dense city (Atlanta) inside exactly one seed tile', () => {
    const tiles = buildSeedTiles();
    const atlanta = { lat: 33.749, lon: -84.388 };
    const containing = tiles.filter(
      (t) => atlanta.lat >= t.s && atlanta.lat < t.n && atlanta.lon >= t.w && atlanta.lon < t.e
    );
    assert.equal(containing.length, 1);
  });
});

describe('tileSelector', () => {
  it('embeds the bbox into node and way ALPR filters', () => {
    const q = tileSelector({ s: 1, w: 2, n: 3, e: 4 });
    assert.match(q, /node\["man_made"="surveillance"\]\["surveillance:type"="ALPR"\]\(1,2,3,4\);/);
    assert.match(q, /way\["man_made"="surveillance"\]\["surveillance:type"="ALPR"\]\(1,2,3,4\);/);
  });
});

describe('countTile', () => {
  it('returns the probed total from an out-count response', async () => {
    const fetchImpl = async () => jsonResponse({ elements: [{ type: 'count', tags: { total: '1234' } }] });
    const n = await countTile({ s: 0, w: 0, n: 1, e: 1 }, fetchImpl);
    assert.equal(n, 1234);
  });

  it('returns 0 when the tile is empty (allowEmpty tolerated)', async () => {
    const fetchImpl = async () => jsonResponse({ elements: [] });
    const n = await countTile({ s: 0, w: 0, n: 1, e: 1 }, fetchImpl);
    assert.equal(n, 0);
  });

  it('sends a [timeout:60] count query, never the national [timeout:300] query', async () => {
    let sentQuery;
    const fetchImpl = async (endpoint, init) => {
      sentQuery = init.body.get('data');
      return jsonResponse({ elements: [{ tags: { total: '1' } }] });
    };
    await countTile({ s: 1, w: 2, n: 3, e: 4 }, fetchImpl);
    assert.match(sentQuery, /\[timeout:60\]/);
    assert.ok(!sentQuery.includes('[timeout:300]'));
    assert.match(sentQuery, /out count;/);
  });
});

describe('planLeafTiles', () => {
  it('keeps a tile as a single leaf when its probe is within SPLIT_THRESHOLD', async () => {
    const seed = [{ s: 0, w: 0, n: 10, e: 10 }];
    const fetchImpl = async () => jsonResponse({ elements: [{ tags: { total: '500' } }] });
    const leaves = await planLeafTiles(seed, fetchImpl);
    assert.equal(leaves.length, 1);
    assert.deepEqual(leaves[0], { s: 0, w: 0, n: 10, e: 10, probed: 500 });
  });

  it('splits a tile whose probe exceeds SPLIT_THRESHOLD into 4 quadrants', async () => {
    const seed = [{ s: 0, w: 0, n: 10, e: 10 }];
    const fetchImpl = async (endpoint, init) => {
      const bboxKey = bboxFromQuery(init.body.get('data'));
      const total = bboxKey === '0,0,10,10' ? 6000 : 1000; // parent over threshold, children under
      return jsonResponse({ elements: [{ tags: { total: String(total) } }] });
    };
    const leaves = await planLeafTiles(seed, fetchImpl);
    assert.equal(leaves.length, 4);
    const expectedQuadrants = [
      { s: 0, w: 0, n: 5, e: 5 },
      { s: 0, w: 5, n: 5, e: 10 },
      { s: 5, w: 0, n: 10, e: 5 },
      { s: 5, w: 5, n: 10, e: 10 },
    ];
    for (const q of expectedQuadrants) {
      const found = leaves.find((l) => l.s === q.s && l.w === q.w && l.n === q.n && l.e === q.e);
      assert.ok(found, `expected quadrant ${JSON.stringify(q)}`);
      assert.equal(found.probed, 1000);
    }
  });

  it('drops a tile whose probe count is zero', async () => {
    const seed = [
      { s: 0, w: 0, n: 1, e: 1 },
      { s: 1, w: 1, n: 2, e: 2 },
    ];
    const fetchImpl = async (endpoint, init) => {
      const bboxKey = bboxFromQuery(init.body.get('data'));
      const total = bboxKey === '0,0,1,1' ? 0 : 200;
      return jsonResponse({ elements: [{ tags: { total: String(total) } }] });
    };
    const leaves = await planLeafTiles(seed, fetchImpl);
    assert.equal(leaves.length, 1);
    assert.equal(leaves[0].probed, 200);
  });

  it('stops subdividing at MIN_TILE_SPAN even if the probe still exceeds SPLIT_THRESHOLD', async () => {
    const seed = [{ s: 0, w: 0, n: 0.05, e: 0.05 }]; // span == MIN_TILE_SPAN floor
    const fetchImpl = async () => jsonResponse({ elements: [{ tags: { total: '9999' } }] });
    const leaves = await planLeafTiles(seed, fetchImpl);
    assert.equal(leaves.length, 1);
    assert.equal(leaves[0].probed, 9999);
  });
});

describe('fetchTileInto', () => {
  it('merges a tile whose feature count matches its probe', async () => {
    const tile = { s: 0, w: 0, n: 1, e: 1, probed: 2 };
    const fetchImpl = async () =>
      jsonResponse({ elements: [alprNode(1, 0.5, 0.5), alprNode(2, 0.6, 0.6)] });
    const featureMap = new Map();
    await fetchTileInto(tile, featureMap, fetchImpl);
    assert.equal(featureMap.size, 2);
    assert.ok(featureMap.has('node/1'));
    assert.ok(featureMap.has('node/2'));
  });

  it('merges into an already-populated map without disturbing other tiles (dedupe)', async () => {
    const featureMap = new Map();
    featureMap.set('node/99', dummyFeature(99));
    const tile = { s: 0, w: 0, n: 1, e: 1, probed: 1 };
    const fetchImpl = async () => jsonResponse({ elements: [alprNode(1, 0.5, 0.5)] });
    await fetchTileInto(tile, featureMap, fetchImpl);
    assert.equal(featureMap.size, 2);
    assert.ok(featureMap.has('node/99'));
    assert.ok(featureMap.has('node/1'));
  });

  it('throws an integrity error when the tile returns far fewer features than probed', async () => {
    const tile = { s: 0, w: 0, n: 1, e: 1, probed: 100 };
    const fetchImpl = async () => jsonResponse({ elements: [alprNode(1, 0.5, 0.5)] }); // 1 of 100
    const featureMap = new Map();
    await assert.rejects(fetchTileInto(tile, featureMap, fetchImpl), /integrity check failed/);
  });

  it('sends a [timeout:60] fetch query (out meta / skel qt), never the national query', async () => {
    let sentQuery;
    const fetchImpl = async (endpoint, init) => {
      sentQuery = init.body.get('data');
      return jsonResponse({ elements: [] });
    };
    const tile = { s: 0, w: 0, n: 1, e: 1, probed: 0 };
    await fetchTileInto(tile, new Map(), fetchImpl);
    assert.match(sentQuery, /\[timeout:60\]/);
    assert.match(sentQuery, /out meta;>;out skel qt;/);
  });
});

describe('fetchCountryArea', () => {
  it('includes admin_level=2 in the area query', async () => {
    let sentQuery;
    const fetchImpl = async (endpoint, init) => {
      sentQuery = init.body.get('data');
      return jsonResponse({ elements: [alprNode(1, 45, -75)] });
    };
    await fetchCountryArea('CA', 0, fetchImpl);
    assert.match(sentQuery, /area\["ISO3166-1"="CA"\]\["admin_level"="2"\]/);
  });

  it('returns a map of features when the count meets the minimum', async () => {
    const fetchImpl = async () => jsonResponse({ elements: [alprNode(1, 45, -75), alprNode(2, 46, -76)] });
    const map = await fetchCountryArea('CA', 2, fetchImpl);
    assert.equal(map.size, 2);
  });

  it('throws when the result is below the integrity floor', async () => {
    const fetchImpl = async () => jsonResponse({ elements: [alprNode(1, 45, -75)] });
    await assert.rejects(fetchCountryArea('CA', 300, fetchImpl), /integrity floor/);
  });

  it('allows an empty result when minCount is 0 (MX, subtract-only)', async () => {
    const fetchImpl = async () => jsonResponse({ elements: [] });
    const map = await fetchCountryArea('MX', 0, fetchImpl);
    assert.equal(map.size, 0);
  });
});

describe('subtractForeign', () => {
  it('drops merged features whose key is in foreignKeys', () => {
    const merged = new Map([
      ['node/10', dummyFeature(10)],
      ['node/20', dummyFeature(20)],
    ]);
    const us = subtractForeign(merged, new Set(['node/20']));
    assert.deepEqual(us.map((f) => f.properties.osmId), [10]);
  });

  it('ignores foreign keys absent from merged (no-op)', () => {
    const merged = new Map([['node/10', dummyFeature(10)]]);
    const us = subtractForeign(merged, new Set(['node/999']));
    assert.deepEqual(us.map((f) => f.properties.osmId), [10]);
  });
});

describe('fetchAllCameras', () => {
  function makeHappyPathFetch({ perTileCount = 2000, overlapId = 900001 } = {}) {
    let nextId = 1;
    let addedOverlap = false;
    return async (endpoint, init) => {
      const query = init.body.get('data');
      if (query.includes('ISO3166-1')) {
        const iso = query.match(/ISO3166-1"="(\w+)"/)[1];
        if (iso === 'CA') {
          const feats = [];
          for (let i = 0; i < 320; i++) feats.push(alprNode(500000 + i, 45, -75));
          feats.push(alprNode(overlapId, 45, -75)); // border camera shared with the US tile grid
          return jsonResponse({ elements: feats });
        }
        return jsonResponse({ elements: [] }); // MX: legitimately empty, minCount 0
      }
      if (query.includes('out count;')) {
        return jsonResponse({ elements: [{ tags: { total: String(perTileCount) } }] });
      }
      // tile fetch
      const feats = [];
      for (let i = 0; i < perTileCount; i++) feats.push(alprNode(nextId++, 40, -90));
      if (!addedOverlap) {
        feats.push(alprNode(overlapId, 40, -90));
        addedOverlap = true;
      }
      return jsonResponse({ elements: feats });
    };
  }

  it('tiles the US, subtracts the CA/MX border overlap, and returns sorted FeatureCollections', async () => {
    const fetchImpl = makeHappyPathFetch();
    const result = await fetchAllCameras(fetchImpl);

    assert.equal(result.us.type, 'FeatureCollection');
    assert.equal(result.ca.type, 'FeatureCollection');
    // 27 seed tiles x 2000 unique cameras each, minus the 1 shared border camera subtracted back out.
    assert.equal(result.us.features.length, 27 * 2000);
    // CA's own published set (320 + the shared border camera) is NOT subtracted from.
    assert.equal(result.ca.features.length, 321);
    assert.ok(
      !result.us.features.some((f) => f.properties.osmId === 900001),
      'border camera should be removed from the US set'
    );
    const ids = result.us.features.map((f) => f.properties.osmId);
    assert.deepEqual(ids, [...ids].sort((a, b) => a - b));
  });

  it('throws when the raw tiled total is below RAW_MIN_TOTAL', async () => {
    const fetchImpl = makeHappyPathFetch({ perTileCount: 5 }); // 27*5 = 135, far below the 50,000 floor
    await assert.rejects(fetchAllCameras(fetchImpl), /Validation failed.*raw cameras/);
  });

  it('fails the whole fetch when one tile persistently fails its integrity check (retries then throws)', async () => {
    let idCounter = 1;
    const failBbox = '24,-125,30.5,-115.16'; // buildSeedTiles()[0]
    const fetchImpl = async (endpoint, init) => {
      const query = init.body.get('data');
      if (query.includes('ISO3166-1')) return jsonResponse({ elements: [] });
      if (query.includes('out count;')) return jsonResponse({ elements: [{ tags: { total: '100' } }] });
      const bboxKey = bboxFromQuery(query);
      if (bboxKey === failBbox) {
        // Always undercounts relative to its probed 100 -> integrity check fails on every retry.
        return jsonResponse({ elements: [alprNode(idCounter++, 24.1, -124.9)] });
      }
      const feats = [];
      for (let i = 0; i < 100; i++) feats.push(alprNode(idCounter++, 40, -90));
      return jsonResponse({ elements: feats });
    };
    const start = Date.now();
    await assert.rejects(fetchAllCameras(fetchImpl), /integrity check failed/);
    // TILE_RETRIES=3, TILE_RETRY_DELAY_MS=1000 -> real backoff delays of ~1000ms + ~2000ms.
    assert.ok(Date.now() - start >= 2500, `expected real retry backoff delay, got ${Date.now() - start}ms`);
  });
});
