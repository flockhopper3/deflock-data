#!/usr/bin/env node
// Fetches ALPR camera data from Overpass for the US (via adaptive tiling) and Canada
// (via an authoritative OSM area query), and writes per-country GeoJSON (hourly naming:
// cameras-<cc>-hourly.geojson) to an output directory, plus meta.json with the metadata
// upload.sh attaches to each R2 object.
//
// Usage: node data/cameras/fetch.mjs [--out <dir>]

import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fetchAllCameras } from './tiled-fetch.mjs';

// minFeatures guards against publishing a truncated/broken Overpass response
// over good data. US has ~100K+ mapped ALPRs, Canada ~500+.
const US_MIN_FEATURES = 50_000;
const CA_MIN_FEATURES = 300;

function outDirFromArgs(argv) {
  const i = argv.indexOf('--out');
  return i !== -1 && argv[i + 1] ? argv[i + 1] : 'out';
}

async function main() {
  const outDir = outDirFromArgs(process.argv.slice(2));
  await mkdir(outDir, { recursive: true });

  const lastUpdated = new Date().toISOString();
  const { us, ca } = await fetchAllCameras();

  const datasets = [
    { slug: 'us', fc: us, minFeatures: US_MIN_FEATURES },
    { slug: 'ca', fc: ca, minFeatures: CA_MIN_FEATURES },
  ];

  const meta = {};
  for (const { slug, fc, minFeatures } of datasets) {
    const count = fc.features.length;
    console.log(`${slug.toUpperCase()}: ${count} camera features`);

    if (count < minFeatures) {
      throw new Error(
        `Validation failed for ${slug.toUpperCase()}: only ${count} cameras (minimum ${minFeatures}). Aborting.`
      );
    }

    const name = `cameras-${slug}-hourly`;
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
