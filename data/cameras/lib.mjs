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

const TIMEOUT_MS = 300_000; // 5 minutes, matches [timeout:300] in the query

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

export async function queryOverpass(query, fetchImpl = fetch) {
  const errors = [];

  for (const endpoint of OVERPASS_ENDPOINTS) {
    // Cleared in finally — a dangling 300s timer would keep Node's event
    // loop alive long after the query resolves or fails.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await fetchImpl(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'User-Agent': 'deflock-data/1.0 (github.com/flockhopper3/deflock-data)',
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
          `Non-JSON response from ${endpoint}: ${text.slice(0, 200)}`
        );
      }

      if (!data.elements || data.elements.length === 0) {
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

const CARDINALS = {
  N: 0, NNE: 22.5, NE: 45, ENE: 67.5,
  E: 90, ESE: 112.5, SE: 135, SSE: 157.5,
  S: 180, SSW: 202.5, SW: 225, WSW: 247.5,
  W: 270, WNW: 292.5, NW: 315, NNW: 337.5,
};

export function parseDirection(value) {
  if (!value) return null;

  const upper = value.toUpperCase();
  if (upper in CARDINALS) return CARDINALS[upper];

  const str = value.includes(';') ? value.split(';')[0] : value;
  const num = parseFloat(str);
  return isNaN(num) ? null : num;
}

export function transformOverpassToGeoJSON(data) {
  // Build node lookup for way centroid calculation
  const nodesById = new Map();
  for (const el of data.elements) {
    if (el.type === 'node' && el.lat !== undefined && el.lon !== undefined) {
      nodesById.set(el.id, { lat: el.lat, lon: el.lon });
    }
  }

  const features = [];

  for (const el of data.elements) {
    const tags = el.tags ?? {};

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
    const direction = parseDirection(directionTag);
    // directionCardinal stores the raw tag only when it's a cardinal string (N, SW, etc.)
    const isCardinal = directionTag ? directionTag.toUpperCase() in CARDINALS : false;

    const properties = {
      osmId: el.id,
      osmType: el.type,
    };

    if (tags['operator']) properties.operator = tags['operator'];
    if (tags['brand'] || tags['manufacturer']) {
      properties.brand = tags['brand'] || tags['manufacturer'];
    }
    if (direction !== null) properties.direction = direction;
    if (isCardinal) properties.directionCardinal = directionTag;
    if (tags['surveillance:zone']) properties.surveillanceZone = tags['surveillance:zone'];
    if (tags['camera:mount']) properties.mountType = tags['camera:mount'];
    if (tags['ref']) properties.ref = tags['ref'];
    if (tags['start_date']) properties.startDate = tags['start_date'];
    if (el.timestamp) properties.osmTimestamp = el.timestamp;
    if (el.version) properties.osmVersion = el.version;

    features.push({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [lon, lat] },
      properties,
    });
  }

  features.sort((a, b) => a.properties.osmId - b.properties.osmId);

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
