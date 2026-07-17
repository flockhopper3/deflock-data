#!/usr/bin/env node
// Fetches ALPR camera data from Overpass for the US and Canada and writes
// per-country GeoJSON (hourly naming: cameras-<cc>-hourly.geojson) to an
// output directory, plus meta.json with the metadata upload.sh attaches
// to each R2 object.
//
// Usage: node data/cameras/fetch.mjs [--out <dir>]

import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import {
  buildCamerasQuery,
  queryOverpass,
  transformOverpassToGeoJSON,
} from './lib.mjs';

// minFeatures guards against publishing a truncated/broken Overpass response
// over good data. US has ~100K+ mapped ALPRs, Canada ~1K+.
const COUNTRIES = [
  { code: 'US', slug: 'us', minFeatures: 50_000 },
  { code: 'CA', slug: 'ca', minFeatures: 300 },
];

const COURTESY_DELAY_MS = 10_000; // pause between Overpass queries

function outDirFromArgs(argv) {
  const i = argv.indexOf('--out');
  return i !== -1 && argv[i + 1] ? argv[i + 1] : 'out';
}

async function main() {
  const outDir = outDirFromArgs(process.argv.slice(2));
  await mkdir(outDir, { recursive: true });

  const lastUpdated = new Date().toISOString();
  const meta = {};

  for (const [i, country] of COUNTRIES.entries()) {
    if (i > 0) await new Promise((r) => setTimeout(r, COURTESY_DELAY_MS));

    console.log(`==> Fetching ${country.code} cameras from Overpass`);
    const data = await queryOverpass(buildCamerasQuery(country.code));

    console.log(`    ${data.elements.length} elements received, transforming`);
    const fc = transformOverpassToGeoJSON(data);
    const count = fc.features.length;
    console.log(`    ${count} camera features`);

    if (count < country.minFeatures) {
      throw new Error(
        `Validation failed for ${country.code}: only ${count} cameras (minimum ${country.minFeatures}). Aborting.`
      );
    }

    const name = `cameras-${country.slug}-hourly`;
    await writeFile(join(outDir, `${name}.geojson`), JSON.stringify(fc));
    meta[name] = { featureCount: count, lastUpdated, source: 'overpass' };
  }

  await writeFile(join(outDir, 'meta.json'), JSON.stringify(meta, null, 2));
  console.log(`==> Wrote ${Object.keys(meta).join(', ')} to ${outDir}/`);
}

main().catch((err) => {
  console.error(err.message ?? err);
  process.exit(1);
});
