// Adaptive-tiling camera fetcher — ported from the FlockHopper Cloudflare data worker's
// src/fetchers/cameras.ts. Instead of one national Overpass query (which now returns
// ~107K elements / ~54MB and times out unreliably at deflock.org's ~60s proxy ceiling),
// this covers the US with a grid of bounding boxes, splitting any box holding more than
// SPLIT_THRESHOLD cameras into quadrants first, so every individual request stays small
// (seconds, not minutes). CA and MX are fetched separately via authoritative OSM area
// queries and subtracted from the US tile grid by node/way id (their ALPR nodes bleed
// into the border tiles).

import { queryOverpass, retryWithBackoff, tileIntegrityFailed, belowMinimum, addElementsToFeatures } from './lib.mjs';

const SPLIT_THRESHOLD = 5_000; // split a tile holding more cameras than this
const MIN_TILE_SPAN = 0.05; // deg — safety floor; stop subdividing below this size
const TILE_CONCURRENCY = 5; // parallel requests in flight to Overpass
const TILE_RETRIES = 3; // per-tile retries before failing the whole fetch
const TILE_RETRY_DELAY_MS = 1_000;
// A tile's fetched feature count must be within this fraction of its probed count,
// else we treat the response as partial/corrupt and fail (rather than silently
// writing a hole into the dataset). 0.10 tolerates churn between probe and fetch.
const TILE_FETCH_TOLERANCE = 0.10;

// Retryable "the fetch clearly worked" floor on the raw, all-country tiled total,
// before CA/MX subtraction. Distinct from the per-dataset write floors in fetch.mjs.
const RAW_MIN_TOTAL = 50_000;

// CA's full ALPR set was measured at ~500+ nodes; 300 catches a >~40% truncation
// (stricter than prod's 250 floor — deliberate, see task report).
const CA_AREA_MIN_COUNT = 300;
// MX is subtract-only and may legitimately be 0 — never throw on an empty MX response.
const MX_AREA_MIN_COUNT = 0;

/** Coarse seed grid covering the continental US + AK/HI/PR. Splitting handles density. */
export function buildSeedTiles() {
  const tiles = [];
  // Continental US: lat 24..50, lon -125..-66, in ~6.5deg x ~9.84deg cells.
  for (let s = 24; s < 50; s += 6.5) {
    for (let w = -125; w < -66; w += 9.84) {
      tiles.push({ s, w, n: Math.min(s + 6.5, 50), e: Math.min(w + 9.84, -66) });
    }
  }
  tiles.push({ s: 51, w: -180, n: 72, e: -129 }); // Alaska (mainland)
  tiles.push({ s: 18, w: -161, n: 23, e: -154 }); // Hawaii
  tiles.push({ s: 17.5, w: -67.5, n: 18.7, e: -64.5 }); // Puerto Rico + USVI
  return tiles;
}

export function tileSelector(t) {
  const b = `${t.s},${t.w},${t.n},${t.e}`;
  return (
    `node["man_made"="surveillance"]["surveillance:type"="ALPR"](${b});` +
    `way["man_made"="surveillance"]["surveillance:type"="ALPR"](${b});`
  );
}

/** Cheap (<1s) count probe used to decide whether a tile needs splitting. */
export async function countTile(t, fetchImpl = fetch) {
  const query = `[out:json][timeout:60];(${tileSelector(t)});out count;`;
  const data = await queryOverpass(query, fetchImpl, { allowEmpty: true });
  const countEl = data.elements?.[0];
  const total = countEl?.tags?.total;
  return total ? Number(total) : 0;
}

/** Expand the seed grid into leaf tiles each holding <= SPLIT_THRESHOLD cameras. */
export async function planLeafTiles(seed, fetchImpl = fetch) {
  const leaves = [];
  const queue = [...seed];

  while (queue.length > 0) {
    const batch = queue.splice(0, TILE_CONCURRENCY);
    const counts = await Promise.all(
      batch.map((t) => retryWithBackoff(() => countTile(t, fetchImpl), TILE_RETRIES, TILE_RETRY_DELAY_MS))
    );

    batch.forEach((t, i) => {
      const count = counts[i];
      if (count === 0) return; // empty tile — drop it
      const span = Math.min(t.n - t.s, t.e - t.w);
      if (count <= SPLIT_THRESHOLD || span <= MIN_TILE_SPAN) {
        leaves.push({ ...t, probed: count });
        return;
      }
      const my = (t.s + t.n) / 2;
      const mx = (t.w + t.e) / 2;
      queue.push(
        { s: t.s, w: t.w, n: my, e: mx },
        { s: t.s, w: mx, n: my, e: t.e },
        { s: my, w: t.w, n: t.n, e: mx },
        { s: my, w: mx, n: t.n, e: t.e }
      );
    });
  }

  return leaves;
}

/**
 * Fetch one tile's full data, verify it against its probe, then merge into the
 * shared map. Throws if the tile returned far fewer features than the probe
 * promised — that throw is retried by the caller, and if it persists the whole
 * fetch fails closed (the pipeline keeps the previous published data rather than
 * writing a hole).
 */
export async function fetchTileInto(t, featureMap, fetchImpl = fetch) {
  const query = `[out:json][timeout:60];(${tileSelector(t)});out meta;>;out skel qt;`;
  const data = await queryOverpass(query, fetchImpl, { allowEmpty: true });

  // Build into a local map first so we can integrity-check this tile in isolation
  // (counting against the shared map would be skewed by neighbour-tile overlap).
  const local = new Map();
  addElementsToFeatures(data.elements, local);

  if (tileIntegrityFailed(local.size, t.probed, TILE_FETCH_TOLERANCE)) {
    throw new Error(
      `Tile (${t.s},${t.w},${t.n},${t.e}) integrity check failed: got ${local.size} features, probe expected ${t.probed}`
    );
  }

  for (const [key, feature] of local) featureMap.set(key, feature);
}

/**
 * Fetch one country's complete ALPR set via an authoritative OSM area query, then
 * enforce `minCount` as an integrity floor. A truncated or empty Overpass response
 * still returns HTTP 200 (allowEmpty), so a short result is only distinguishable from
 * a genuinely sparse country by this floor — below it we refuse to let a caller
 * subtract a partial result from the US set. A 0-node result is only valid when
 * `minCount` is 0 (e.g. MX, subtract-only).
 */
export async function fetchCountryArea(iso, minCount, fetchImpl = fetch) {
  const query =
    `[out:json][timeout:90];area["ISO3166-1"="${iso}"]["admin_level"="2"]->.a;` +
    `(node["man_made"="surveillance"]["surveillance:type"="ALPR"](area.a);` +
    `way["man_made"="surveillance"]["surveillance:type"="ALPR"](area.a););` +
    `out meta;>;out skel qt;`;
  const data = await queryOverpass(query, fetchImpl, { allowEmpty: true });
  const map = new Map();
  addElementsToFeatures(data.elements, map);

  if (belowMinimum(map.size, minCount)) {
    throw new Error(
      `Area query ${iso}: got ${map.size} ALPR features (integrity floor ${minCount}). Likely a partial/empty Overpass response — refusing to subtract a partial result.`
    );
  }

  return map;
}

/** Pure: return merged features whose ${type}/${id} key is not in foreignKeys. */
export function subtractForeign(merged, foreignKeys) {
  const out = [];
  for (const [key, feature] of merged) {
    if (!foreignKeys.has(key)) out.push(feature);
  }
  return out;
}

/** Pure: sort features by osmId and wrap as a FeatureCollection. */
function sortedFeatureCollection(features) {
  const sorted = [...features].sort((a, b) => a.properties.osmId - b.properties.osmId);
  return { type: 'FeatureCollection', features: sorted };
}

/**
 * Orchestrate the full fetch: tile the US grid -> merge/dedupe -> validate raw total
 * -> fetch CA + MX authoritative area sets -> subtract both from the US set -> return
 * sorted { us, ca } FeatureCollections. Every network-touching step accepts fetchImpl
 * so tests can inject a mock; production callers pass nothing and get global fetch.
 */
export async function fetchAllCameras(fetchImpl = fetch) {
  console.log('Fetching camera data (US tiling + neighbour area subtraction)...');

  // 1. Tile the US grid -> merged, deduped set.
  const seed = buildSeedTiles();
  const leaves = await planLeafTiles(seed, fetchImpl);
  console.log(`Planned ${leaves.length} leaf tiles`);

  const merged = new Map();
  const queue = [...leaves];
  while (queue.length > 0) {
    const batch = queue.splice(0, TILE_CONCURRENCY);
    await Promise.all(
      batch.map((t) => retryWithBackoff(() => fetchTileInto(t, merged, fetchImpl), TILE_RETRIES, TILE_RETRY_DELAY_MS))
    );
  }

  const rawTotal = merged.size;
  console.log(`Merged ${rawTotal} raw camera features from ${leaves.length} tiles`);
  if (rawTotal < RAW_MIN_TOTAL) {
    throw new Error(`Validation failed: only ${rawTotal} raw cameras (minimum ${RAW_MIN_TOTAL}). Skipping update.`);
  }

  // 2. Fetch each bordering country's authoritative ALPR set via OSM area query.
  const caMap = await retryWithBackoff(
    () => fetchCountryArea('CA', CA_AREA_MIN_COUNT, fetchImpl),
    TILE_RETRIES,
    TILE_RETRY_DELAY_MS
  );
  console.log(`Area query CA: ${caMap.size} ALPR features`);
  const mxMap = await retryWithBackoff(
    () => fetchCountryArea('MX', MX_AREA_MIN_COUNT, fetchImpl),
    TILE_RETRIES,
    TILE_RETRY_DELAY_MS
  );
  console.log(`Area query MX: ${mxMap.size} ALPR features`);

  // 3. Subtract all foreign IDs from the merged set -> clean US set.
  const foreignKeys = new Set();
  for (const key of caMap.keys()) foreignKeys.add(key);
  for (const key of mxMap.keys()) foreignKeys.add(key);
  const usFeatures = subtractForeign(merged, foreignKeys);
  console.log(`US after subtraction: ${usFeatures.length} (removed ${rawTotal - usFeatures.length} foreign)`);

  const us = sortedFeatureCollection(usFeatures);
  const ca = sortedFeatureCollection([...caMap.values()]);
  console.log(`  dataset US: ${us.features.length}`);
  console.log(`  dataset CA: ${ca.features.length}`);

  return { us, ca };
}
