// Enriches a cameras GeoJSON with four small integer filter codes per feature
// and emits the manifest that maps codes back to labels:
//   b — brand id        (0 = missing/unknown, 1..N by descending camera count)
//   o — operator id     (0 = missing,          1..N by descending camera count)
//   z — surveillance zone (0 missing, 1 traffic, 2 town, 3 parking, 4 other)
//   m — mount type        (0 missing, 1 pole, 2 wall, 3 street_light, 4 other)
//
// Ids are build-scoped: they are reassigned every build as counts shift, so the
// manifest and the filter tileset built from the enriched output must always
// ship together as a matched set.
//
// CLI: node enrich.mjs <input.geojson> <enriched-output.geojson> <manifest-output.json>

import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// Ported verbatim from DeFlock Maps (MapPanel.tsx normalizeBrand) — the app is
// the source of truth for these rules; keep the two in sync.
export function normalizeBrand(raw) {
  if (typeof raw !== 'string') return null;
  const lower = raw.toLowerCase().trim();

  // Garbage / not a real brand → treat as unknown
  if (lower.startsWith('unk') || lower === 'unknown' || lower === 'generic'
    || lower === 'other' || lower.length <= 1 || lower.startsWith('wikidata')
    || lower.startsWith('q108') || lower === 'scm?') return null;

  if (lower.startsWith('flock') || lower.startsWith('floc') || lower === 'flow safety') return 'Flock Safety';
  if (lower.startsWith('motor') || lower.startsWith('morto') || lower.startsWith('vigilant')) return 'Motorola Solutions';
  if (lower.startsWith('genetec') || lower.startsWith('genete') || lower.startsWith('autovu')) return 'Genetec';
  if (lower.startsWith('leonardo') || lower.startsWith('elsag')) return 'Leonardo';
  if (lower.startsWith('rekor') || lower === 'rektor') return 'Rekor';
  if (lower.startsWith('neology') || lower.startsWith('pips')) return 'Neology';
  if (lower.startsWith('axis')) return 'Axis Communications';
  if (lower.startsWith('ekin')) return 'Ekin';
  if (lower.startsWith('ubicq')) return 'Ubicquia';
  if (lower.startsWith('avigilon')) return 'Avigilon';
  if (lower.startsWith('verkada')) return 'Verkada';
  if (lower.startsWith('axon')) return 'Axon';
  if (lower.startsWith('kapsch')) return 'Kapsch';
  if (lower.startsWith('live') || lower.startsWith('life') || lower === 'lvt') return 'LiveView Technologies';
  if (lower.startsWith('insight')) return 'Insight LPR';
  if (lower.startsWith('mob')) return 'Mobotix';
  if (lower.startsWith('hanwha')) return 'Hanwha Vision';
  if (lower.includes('cyber') || lower.startsWith('yber')) return 'Cyber Secure';
  if (lower.startsWith('hikvision')) return 'Hikvision';
  if (lower.startsWith('dahua')) return 'Dahua';
  if (lower.startsWith('redspeed')) return 'Redspeed';
  if (lower.startsWith('mesa')) return 'Mesa Technologies';
  if (lower.startsWith('icamera')) return 'ICamera';
  if (lower.startsWith('epic')) return 'EPIC IO';
  if (lower.startsWith('transcore')) return 'TransCore';
  if (lower.startsWith('platesmart')) return 'PlateSmart';
  if (lower.startsWith('adaptive')) return 'Adaptive Recognition';
  if (lower.startsWith('ndi')) return 'NDI Recognition Systems';
  if (lower.startsWith('mav')) return 'Mav Systems';
  if (lower.startsWith('jenoptik')) return 'Jenoptik';
  if (lower.startsWith('uniview') || lower === 'unv') return 'Uniview';
  if (lower.startsWith('platelogiq')) return 'PlateLogiq';

  return raw.trim();
}

const ZONE_LABELS = ['traffic', 'town', 'parking'];
const MOUNT_LABELS = ['pole', 'wall', 'street_light'];

function categoryCode(raw, labels) {
  if (raw === undefined || raw === null) return 0;
  const s = String(raw).trim().toLowerCase();
  if (s === '') return 0;
  const idx = labels.indexOf(s);
  return idx === -1 ? 4 : idx + 1;
}

export function zoneCode(raw) {
  return categoryCode(raw, ZONE_LABELS);
}

export function mountCode(raw) {
  return categoryCode(raw, MOUNT_LABELS);
}

// [{id, label, count}] sorted by count desc; label sort breaks ties so id
// assignment is deterministic for a given dataset.
function rankByCount(counts) {
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([label, count], i) => ({ id: i + 1, label, count }));
}

function categoryManifest(labels, tally) {
  return [...labels, 'other'].map((label, i) => ({ id: i + 1, label, count: tally[i + 1] }));
}

/**
 * Enrich a FeatureCollection with b/o/z/m codes and build the manifest body.
 * Returns { collection, manifest } where manifest = { total, brands,
 * operators, zones, mounts } (version/generatedAt are added by the CLI).
 */
export function enrichCollection(fc) {
  const brandCounts = new Map(); // canonical label -> count
  const operatorGroups = new Map(); // lowercased key -> { count, casings: Map<label, count> }
  const zoneTally = [0, 0, 0, 0, 0];
  const mountTally = [0, 0, 0, 0, 0];

  const perFeature = fc.features.map((f) => {
    const p = f.properties ?? {};

    const brand = normalizeBrand(p.brand);
    if (brand) brandCounts.set(brand, (brandCounts.get(brand) ?? 0) + 1);

    let opKey = null;
    if (typeof p.operator === 'string' && p.operator.trim() !== '') {
      const label = p.operator.trim();
      opKey = label.toLowerCase();
      let group = operatorGroups.get(opKey);
      if (!group) {
        group = { count: 0, casings: new Map() };
        operatorGroups.set(opKey, group);
      }
      group.count += 1;
      group.casings.set(label, (group.casings.get(label) ?? 0) + 1);
    }

    const z = zoneCode(p.surveillanceZone);
    const m = mountCode(p.mountType);
    zoneTally[z] += 1;
    mountTally[m] += 1;

    return { brand, opKey, z, m };
  });

  const brands = rankByCount(brandCounts);
  const brandIds = new Map(brands.map((b) => [b.label, b.id]));

  // Display label per operator group = its most common casing (first-seen wins ties)
  const operatorCounts = new Map(); // display label -> count, insertion-ordered by group
  const operatorLabelByKey = new Map();
  for (const [key, group] of operatorGroups) {
    let best = null;
    for (const [label, n] of group.casings) {
      if (best === null || n > best.n) best = { label, n };
    }
    operatorCounts.set(best.label, group.count);
    operatorLabelByKey.set(key, best.label);
  }
  const operators = rankByCount(operatorCounts);
  const operatorIds = new Map(operators.map((o) => [o.label, o.id]));

  const features = fc.features.map((f, i) => {
    const { brand, opKey, z, m } = perFeature[i];
    return {
      ...f,
      properties: {
        ...f.properties,
        b: brand ? brandIds.get(brand) : 0,
        o: opKey ? operatorIds.get(operatorLabelByKey.get(opKey)) : 0,
        z,
        m,
      },
    };
  });

  return {
    collection: { type: 'FeatureCollection', features },
    manifest: {
      total: fc.features.length,
      brands,
      operators,
      zones: categoryManifest(ZONE_LABELS, zoneTally),
      mounts: categoryManifest(MOUNT_LABELS, mountTally),
    },
  };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  const [input, enrichedOut, manifestOut] = process.argv.slice(2);
  if (!input || !enrichedOut || !manifestOut) {
    console.error('usage: node enrich.mjs <input.geojson> <enriched-output.geojson> <manifest-output.json>');
    process.exit(1);
  }

  const bytes = readFileSync(input);
  const { collection, manifest } = enrichCollection(JSON.parse(bytes.toString('utf8')));

  writeFileSync(enrichedOut, JSON.stringify(collection));
  writeFileSync(
    manifestOut,
    JSON.stringify({
      // Hash of the exact input snapshot — lets the client detect a
      // manifest/tileset generation mismatch across builds.
      version: createHash('sha256').update(bytes).digest('hex').slice(0, 16),
      generatedAt: new Date().toISOString(),
      ...manifest,
    })
  );

  console.log(
    `Enriched ${manifest.total} features: ${manifest.brands.length} brands, ${manifest.operators.length} operators`
  );
}
