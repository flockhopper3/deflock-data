// Camera ingestion library — ported from the FlockHopper Cloudflare data worker.
// Queries Overpass for ALPR cameras, transforms to GeoJSON point features.

// DeFlock's own instance first — it's built for exactly this query load;
// the public instances remain as fallbacks.
export const OVERPASS_ENDPOINTS = [
  'https://overpass.deflock.org/api/interpreter',
  'https://overpass-api.de/api/interpreter',
  'https://overpass.kumi.systems/api/interpreter',
  'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
];

// DeFlock's Overpass sits behind a reverse proxy that returns 504 at ~60s of
// wall-clock, regardless of the [timeout:N] in the query. Abort below that so a
// stuck request fails fast to the next endpoint instead of hanging the pipeline.
export const TIMEOUT_MS = 55_000;

// Overpass mirrors (especially deflock.org) reject or rate-limit requests without
// a User-Agent that identifies the app and provides a contact channel.
export const OVERPASS_USER_AGENT =
  'FlockHopper-Data/1.0 (+https://dontgetflocked.com; alerts@dontgetflocked.com)';

export function buildCamerasQuery(countryCode) {
  return `[out:json][timeout:300];
area["ISO3166-1"="${countryCode}"]->.country;
(
  node["man_made"="surveillance"]["surveillance:type"="ALPR"](area.country);
  way["man_made"="surveillance"]["surveillance:type"="ALPR"](area.country);
);
out meta;
>;
out skel qt;`;
}

/**
 * Run an Overpass query, falling back across endpoints. Rejects on an empty
 * `elements` result unless `allowEmpty` is set — use `allowEmpty: true` for
 * queries where a zero-result response is legitimate (e.g. a sparse tile).
 * Returns the parsed JSON response body directly.
 */
export async function queryOverpass(query, fetchImpl = fetch, { allowEmpty = false } = {}) {
  const errors = [];

  for (const endpoint of OVERPASS_ENDPOINTS) {
    // Cleared in finally — a dangling timer would keep Node's event loop
    // alive long after the query resolves or fails.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await fetchImpl(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'User-Agent': OVERPASS_USER_AGENT,
          Accept: 'application/json',
        },
        body: new URLSearchParams({ data: query }),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status} from ${endpoint}`);
      }

      // Overpass instances can return HTTP 200 with an XML/HTML error
      // document (rate limits, WAF blocks) — surface the body's head so
      // the CI log shows WHY an endpoint was skipped.
      const text = await response.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        throw new Error(
          `Non-JSON response from ${endpoint}: ${text.slice(0, 500).replace(/\s+/g, ' ')}`
        );
      }

      if (!allowEmpty && (!data.elements || data.elements.length === 0)) {
        throw new Error(`Empty response from ${endpoint}`);
      }

      return data;
    } catch (error) {
      const err = error instanceof Error ? error : new Error(String(error));
      errors.push(err);
      console.error(`Overpass endpoint ${endpoint} failed: ${err.message}`);
    } finally {
      clearTimeout(timeoutId);
    }
  }

  throw new Error(
    `All Overpass endpoints failed: ${errors.map((e) => e.message).join('; ')}`
  );
}

/** Retry an async function with exponential backoff (base * 2^(attempt-1)). */
export async function retryWithBackoff(fn, maxRetries, baseDelayMs) {
  let lastError;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err));
      if (attempt < maxRetries) {
        const delay = baseDelayMs * Math.pow(2, attempt - 1);
        await new Promise((r) => setTimeout(r, delay));
      }
    }
  }

  throw lastError;
}

/**
 * Per-tile integrity check: true if a tile's fetched feature count fell short of
 * what its count-probe promised (beyond `tolerance`). Catches a tile that returns
 * HTTP 200 but partial/empty data — the silent-failure case tiling introduces.
 */
export function tileIntegrityFailed(produced, probed, tolerance) {
  if (probed <= 0) return false;
  return produced < probed * (1 - tolerance);
}

/** Per-country write floor: true if a dataset's count is below its minimum acceptable value. */
export function belowMinimum(count, min) {
  return count < min;
}

const CARDINALS = {
  N: 0, NNE: 22.5, NE: 45, ENE: 67.5,
  E: 90, ESE: 112.5, SE: 135, SSE: 157.5,
  S: 180, SSW: 202.5, SW: 225, WSW: 247.5,
  W: 270, WNW: 292.5, NW: 315, NNW: 337.5,
};

const SPELLED_CARDINALS = {
  NORTH: 0, NORTHEAST: 45, EAST: 90, SOUTHEAST: 135,
  SOUTH: 180, SOUTHWEST: 225, WEST: 270, NORTHWEST: 315,
};

const BOUND_DIRECTIONS = {
  NB: 0, EB: 90, SB: 180, WB: 270,
};

function normalizeDegrees(deg) {
  return ((deg % 360) + 360) % 360;
}

/** Resolve a simple token (cardinal, spelled-out, bound, or numeric) to raw degrees. No normalization, no range/semicolon handling. */
function resolveSimple(token) {
  const upper = token.trim().toUpperCase();
  if (!upper) return null;
  if (upper in CARDINALS) return CARDINALS[upper];
  if (upper in SPELLED_CARDINALS) return SPELLED_CARDINALS[upper];
  if (upper in BOUND_DIRECTIONS) return BOUND_DIRECTIONS[upper];
  const num = Number(upper); // Number() rejects "338-23" unlike parseFloat
  return isNaN(num) ? null : num;
}

/** Compute the midpoint bearing of a clockwise sector from startDeg to endDeg (raw values, not pre-normalized). */
function rangeMidpoint(startDeg, endDeg) {
  const rawArc = endDeg - startDeg;
  const arc = ((rawArc % 360) + 360) % 360;
  // Full circle: raw values differ but normalized arc is 0 (e.g. 0->360)
  if (arc === 0 && rawArc !== 0) return normalizeDegrees(startDeg + 180);
  if (arc === 0) return normalizeDegrees(startDeg);
  return normalizeDegrees(startDeg + arc / 2);
}

/** Parse a single direction token which may be a cardinal, numeric, bound, spelled-out, or range (e.g. "338-23"). */
function parseSingleToken(token) {
  const trimmed = token.trim();
  if (!trimmed) return null;

  // Try simple resolve first (cardinal, spelled-out, bound, numeric)
  const simple = resolveSimple(trimmed);
  if (simple !== null) return normalizeDegrees(simple);

  // Range notation: "338-23", "WSW-ESE" — find dash that isn't a leading negative
  const dashIdx = trimmed.indexOf('-', 1);
  if (dashIdx > 0) {
    const left = resolveSimple(trimmed.slice(0, dashIdx));
    const right = resolveSimple(trimmed.slice(dashIdx + 1));
    if (left !== null && right !== null) {
      return rangeMidpoint(left, right);
    }
  }

  return null;
}

/** Parse a direction tag into all resolved bearings (handles semicolons and commas). */
export function parseDirections(value) {
  if (!value) return [];
  const tokens = value.split(/[;,]/).map((t) => t.trim()).filter(Boolean);
  const results = [];
  for (const token of tokens) {
    const deg = parseSingleToken(token);
    if (deg !== null) results.push(deg);
  }
  return results;
}

/** Parse a direction tag into a single bearing (first resolved value). Backward-compatible. */
export function parseDirection(value) {
  const dirs = parseDirections(value);
  return dirs.length > 0 ? dirs[0] : null;
}

/**
 * Transform a batch of Overpass elements (one tile's response, or a whole national
 * response) into GeoJSON point features, merging into `featureMap`. Keyed by
 * `${type}/${id}` so cameras appearing in two overlapping tiles are deduped to one.
 */
export function addElementsToFeatures(elements, featureMap) {
  // Build node lookup for way centroid calculation (scoped to this batch; Overpass
  // recursion `>;` keeps a selected way's child nodes in the same response).
  const nodesById = new Map();
  for (const el of elements) {
    if (el.type === 'node' && el.lat !== undefined && el.lon !== undefined) {
      nodesById.set(el.id, { lat: el.lat, lon: el.lon });
    }
  }

  for (const el of elements) {
    const tags = el.tags ?? {};

    // Only process surveillance ALPR elements
    if (tags['man_made'] !== 'surveillance') continue;
    if (tags['surveillance:type'] !== 'ALPR') continue;

    let lat = el.lat;
    let lon = el.lon;

    // For ways, compute centroid from child nodes
    if (el.type === 'way' && el.nodes) {
      const wayNodes = el.nodes
        .map((id) => nodesById.get(id))
        .filter((n) => n !== undefined);

      if (wayNodes.length > 0) {
        lat = wayNodes.reduce((sum, n) => sum + n.lat, 0) / wayNodes.length;
        lon = wayNodes.reduce((sum, n) => sum + n.lon, 0) / wayNodes.length;
      }
    }

    if (lat === undefined || lon === undefined) continue;

    const directionTag = tags['direction'] || tags['camera:direction'];
    const directions = parseDirections(directionTag);
    const direction = directions.length > 0 ? directions[0] : null;
    // directionCardinal stores the original cardinal string when the (first) token is a 16-point compass point
    const firstToken = directionTag?.split(/[;,]/)[0]?.trim();
    const isCardinal = firstToken ? firstToken.toUpperCase() in CARDINALS : false;

    const properties = {
      osmId: el.id,
      osmType: el.type,
    };

    if (tags['operator']) properties.operator = tags['operator'];
    if (tags['brand'] || tags['manufacturer']) {
      properties.brand = tags['brand'] || tags['manufacturer'];
    }
    if (direction !== null) properties.direction = direction;
    if (directions.length > 1) properties.directions = directions;
    if (isCardinal) properties.directionCardinal = firstToken;
    if (tags['surveillance:zone']) properties.surveillanceZone = tags['surveillance:zone'];
    if (tags['camera:mount']) properties.mountType = tags['camera:mount'];
    if (tags['ref']) properties.ref = tags['ref'];
    if (tags['start_date']) properties.startDate = tags['start_date'];
    if (el.timestamp) properties.osmTimestamp = el.timestamp;
    if (el.version) properties.osmVersion = el.version;

    featureMap.set(`${el.type}/${el.id}`, {
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [lon, lat] },
      properties,
    });
  }
}

export function transformOverpassToGeoJSON(data) {
  const featureMap = new Map();
  addElementsToFeatures(data.elements, featureMap);

  const features = [...featureMap.values()].sort(
    (a, b) => a.properties.osmId - b.properties.osmId
  );

  return { type: 'FeatureCollection', features };
}

// Merge per-country collections into one, deduping border-area elements that
// fall inside both country polygons.
export function mergeFeatureCollections(collections) {
  const seen = new Set();
  const features = [];

  for (const fc of collections) {
    for (const f of fc.features) {
      const key = `${f.properties.osmType}/${f.properties.osmId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      features.push(f);
    }
  }

  features.sort((a, b) => a.properties.osmId - b.properties.osmId);

  return { type: 'FeatureCollection', features };
}
