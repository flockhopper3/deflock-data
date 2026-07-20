// Shapes Census cartographic-boundary GeoJSON into the three boundary tile
// layers (states, counties, municipalities) with exactly the attributes the
// app filters on. Consumes GeoJSON produced by mapshaper in build.sh:
//   states.json    — cb_<vintage>_us_state_500k, as-is
//   counties.json  — cb_<vintage>_us_county_500k, as-is
//   places.json    — cb_<vintage>_us_place_500k with co_name/co_geoid
//                    largest-overlap county join applied by mapshaper
//   cousubs.json   — cb_<vintage>_us_cousub_500k, as-is
//
// CLI: node prep.mjs <states.json> <counties.json> <places.json> <cousubs.json> <outdir>

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

// States where county subdivisions (MCDs) are functioning municipal
// governments — New England towns plus mid-Atlantic/Midwest townships.
export const STRONG_MCD_STATEFPS = new Set([
  '09', '23', '25', '33', '44', '50', // CT ME MA NH RI VT
  '36', '34', '42', // NY NJ PA
  '26', '55', '27', // MI WI MN
]);

// Municipality kinds incorporated places already cover — a cousub of one of
// these types is the same government appearing in both source files.
const PLACE_COVERED_TYPES = new Set(['city', 'village', 'borough']);

// NAMELSAD is NAME plus the LSAD descriptor ("Springfield city",
// "Bethesda CDP", "Town of X"). The descriptor may prefix or suffix the
// name; whatever remains after removing NAME is the type.
export function deriveType(name, namelsad) {
  if (!namelsad || namelsad === name) return 'municipality';
  let rest = null;
  if (namelsad.startsWith(name)) rest = namelsad.slice(name.length).trim();
  else if (namelsad.endsWith(name)) rest = namelsad.slice(0, namelsad.length - name.length).trim();
  if (!rest) return 'municipality';
  return rest === 'CDP' ? 'CDP' : rest.toLowerCase();
}

export function keepCousub(props) {
  return (
    props.FUNCSTAT === 'A' &&
    STRONG_MCD_STATEFPS.has(props.STATEFP) &&
    (props.ALAND ?? 0) > 0 &&
    !PLACE_COVERED_TYPES.has(deriveType(props.NAME, props.NAMELSAD))
  );
}

const feature = (src, properties) => ({ type: 'Feature', properties, geometry: src.geometry });

export function shapeState(f) {
  const p = f.properties;
  return feature(f, { name: p.NAME, abbrev: p.STUSPS, fips: p.GEOID });
}

export function shapeCounty(f) {
  const p = f.properties;
  return feature(f, { name: p.NAME, state: p.STUSPS, fips: p.GEOID });
}

export function shapePlace(f) {
  const p = f.properties;
  return feature(f, {
    name: p.NAME,
    type: deriveType(p.NAME, p.NAMELSAD),
    state: p.STUSPS,
    county: p.co_name ?? null,
    fips: p.GEOID,
  });
}

export function shapeCousub(f, countyNames) {
  const p = f.properties;
  return feature(f, {
    name: p.NAME,
    type: deriveType(p.NAME, p.NAMELSAD),
    state: p.STUSPS,
    county: countyNames.get(p.STATEFP + p.COUNTYFP) ?? null,
    fips: p.GEOID,
  });
}

const collection = (features) => ({ type: 'FeatureCollection', features });

export function buildLayers({ states, counties, places, cousubs }) {
  const countyNames = new Map(counties.features.map((f) => [f.properties.GEOID, f.properties.NAME]));
  return {
    states: collection(states.features.map(shapeState)),
    counties: collection(counties.features.map(shapeCounty)),
    municipalities: collection([
      ...places.features.map(shapePlace),
      ...cousubs.features.filter((f) => keepCousub(f.properties)).map((f) => shapeCousub(f, countyNames)),
    ]),
  };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  const [statesIn, countiesIn, placesIn, cousubsIn, outDir] = process.argv.slice(2);
  if (!statesIn || !countiesIn || !placesIn || !cousubsIn || !outDir) {
    console.error('usage: node prep.mjs <states.json> <counties.json> <places.json> <cousubs.json> <outdir>');
    process.exit(1);
  }
  const load = (p) => JSON.parse(readFileSync(p, 'utf8'));
  const layers = buildLayers({
    states: load(statesIn),
    counties: load(countiesIn),
    places: load(placesIn),
    cousubs: load(cousubsIn),
  });
  mkdirSync(outDir, { recursive: true });
  for (const [name, fc] of Object.entries(layers)) {
    writeFileSync(join(outDir, `${name}.geojson`), JSON.stringify(fc));
  }
  console.log(
    `states=${layers.states.features.length} counties=${layers.counties.features.length} municipalities=${layers.municipalities.features.length}`
  );
}
