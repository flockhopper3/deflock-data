# US Boundaries Tileset — Design

**Date:** 2026-07-19
**Status:** Approved pending user review

## Goal

A single US boundary vector tileset — states, counties, and municipalities as
polygons — with enough metadata on every feature (names, state, county, FIPS)
that the app can filter client-side to whatever slice it wants. No server-side
filtering, no new APIs: one PMTiles archive next to the camera tilesets,
served through the existing tiles Worker.

## Scope

- **US only.** Canada (StatCan census subdivisions) is a possible later
  follow-up with its own build; nothing in this design blocks it.
- **Build + upload only.** App-side rendering/filter UI is out of scope, as
  with the camera filter tileset.

## Data source

Census Bureau **cartographic boundary files**, 2024 vintage, 1:500,000 scale
(pre-simplified for mapping — far smaller than full TIGER/Line and visually
right for tiles):

| File | Features | Used for |
|------|----------|----------|
| `cb_2024_us_state_500k` | 56 | `states` layer |
| `cb_2024_us_county_500k` | ~3,235 | `counties` layer + county-name join |
| `cb_2024_us_place_500k` | ~32,000 | `municipalities` (incorporated places + CDPs) |
| `cb_2024_us_cousub_500k` | ~36,000 (filtered) | `municipalities` (strong-MCD-state townships/towns) |

Downloaded from `https://www2.census.gov/geo/tiger/GENZ2024/shp/` at build
time (zipped shapefiles). The vintage is a pinned constant in the build
script — bump it when the Census publishes a new year.

## Archive layout

One archive: **`boundaries-us.pmtiles`**, three layers, built with one
tippecanoe pass per layer merged via `tile-join` (same pattern as the cameras
build).

| Layer | Zoom range | Attributes |
|-------|-----------|------------|
| `states` | z0–z12 | `name`, `abbrev` (USPS), `fips` (2-digit) |
| `counties` | z2–z12 | `name`, `state` (USPS abbrev), `fips` (5-digit GEOID) |
| `municipalities` | z5–z12 | `name`, `type`, `state` (USPS abbrev), `county` (name), `fips` (7-digit place GEOID or 10-digit cousub GEOID) |

- Max zoom 12: MapLibre overzooms vector tiles past their maxzoom, and 1:500k
  geometry holds up visually well beyond z12. Keeps the archive small.
- Attributes are present at **all** zooms in each layer's range — filtering
  must work everywhere, and polygon attribute overhead is negligible relative
  to geometry.
- Tippecanoe flags per pass: `--coalesce-densest-as-needed` off; use
  `--no-feature-limit --no-tile-size-limit --buffer=4` (polygons need a
  buffer, unlike the camera points which use `--buffer=0`), default
  simplification (the source is already generalized), `--layer=<name>`,
  matching zoom bounds per layer.

## Municipalities layer definition

The `type` attribute makes the layer a superset the client filters:

1. **Places nationwide** (`cb_2024_us_place_500k`): incorporated cities,
   towns, villages, boroughs, plus **CDPs**. `type` is derived from the LSAD
   code (`city`, `town`, `village`, `borough`, `CDP`, …). CDPs are included
   deliberately — they cover unincorporated communities — and are always
   tagged `type: "CDP"` so the client can hide them.
2. **County subdivisions in the 12 strong-MCD states** (CT, ME, MA, NH, RI,
   VT, NY, NJ, PA, MI, WI, MN), where MCDs are functioning municipal
   governments (New England towns, townships). The cartographic boundary
   files carry no `FUNCSTAT` (that field is TIGER/Line-only); filtering
   instead excludes LSAD `00` (undefined/consolidated, already covered by
   its place), `46` (unorganized territory — no functioning government),
   and `86` (reservation — not a municipal government), keeping only
   functioning municipal governments. `type` from LSAD (`town`, `township`,
   `plantation`, …).
3. **Dedup:** where a cousub duplicates an incorporated place (coextensive
   cities, e.g. most MI/WI cities appear in both files), drop the cousub by
   *type*: any cousub whose derived type is `city`, `village`, or `borough`
   is excluded, because incorporated places already carry those governments.
   This is deterministic (no name matching) and never drops New England/NY
   towns or townships. Known limitation: NY villages sit *inside* towns —
   both are legitimate municipalities and both are kept; the client sees
   overlapping polygons and can filter by `type`.

## County-name join

- **Cousubs** carry `COUNTYFP` natively — join to the county name via
  `STATEFP+COUNTYFP`.
- **Places** don't carry a county. Assigned at build time with a
  largest-overlap spatial join against the counties layer (mapshaper
  `-join ... largest-overlap`): a place straddling two counties gets the one
  containing most of its area. Single-valued by design — good enough for
  filtering, and avoids multi-value attribute complexity.

## Pipeline

New directory **`tiles/boundaries/`** mirroring `tiles/cameras/`:

- `build.sh` — orchestrates: download the four CB zips for the pinned
  vintage, unzip, run prep, run tippecanoe passes,
  `tile-join`, sanity-check, upload to R2.
- `prep.mjs` — Node script (mapshaper as a pinned npm dependency) that:
  converts shapefiles to GeoJSON, builds the merged municipalities layer
  (place + filtered cousub + dedup), does the county-name joins, maps LSAD →
  `type`, renames/strips attributes to exactly the spec above, and emits one
  GeoJSON per layer.
- `prep.test.mjs` — unit tests for the pure pieces (LSAD → type mapping,
  dedup rule, attribute shaping) with small fixture features, runnable via
  `node --test` like the camera tests.
- `verify.sh` — post-build sanity: `tippecanoe-decode` a few known tiles and
  assert expected feature counts / attributes (e.g. a z5 tile contains a
  known state; a z10 tile over a known city contains its municipality with
  the right `county`).

**Sanity floors before upload** (same philosophy as the camera build):
states = 56 exactly, counties ≥ 3,000, municipalities ≥ 30,000, archive size
≥ 5 MB. Fail the build rather than upload a truncated archive.

## Workflow / cadence

New **`.github/workflows/build-boundaries.yml`**, `workflow_dispatch` only —
boundaries change roughly annually with Census vintages, so no cron and no
chaining to the hourly camera fetch. Reuses the cached felt/tippecanoe build
steps from `build-tiles.yml`. Uploads:

- `boundaries-us.pmtiles` → `R2_TILES_BUCKET` (`flockhopper-tiles`) +
  `R2_TILES_MIRROR_BUCKET` (`deflock-data`), via the same conditional-write
  care the camera build uses.

No skip-check/hash machinery needed — the workflow is manual and rare; every
dispatch rebuilds and uploads.

## Serving

The tiles Worker already routes `*.pmtiles` generically (per the 2026-07-18
handoff doc), so `https://tiles.dontgetflocked.com/boundaries-us/{z}/{x}/{y}`
and the `boundaries-us.json` TileJSON are expected to work with **no Worker
change**. Verification step after first upload: fetch the TileJSON and a
tile. If the Worker turns out to allowlist archive names, that's a one-line
Worker addition — **user-deployed** (hard rule: no Cloudflare deploys from
this repo).

## Error handling

- Download failures / bad zips: `set -euo pipefail` + explicit unzip checks;
  the build dies before touching R2.
- Prep failures (join produced empty output, dedup removed too much): the
  sanity floors catch them.
- Per-layer tippecanoe failure: whole build fails (no partial upload — the
  three layers ship as one archive or not at all).

## Testing

- `node --test tiles/boundaries/*.test.mjs` in the workflow before building.
- `verify.sh` after the archive is built, before upload.
- Local mode: `build.sh --local [out.pmtiles]` builds without R2 for preview
  in `tiles/local-dev/` (same convention as the cameras build).

## Documentation

- `tiles/boundaries/README.md` — layer/attribute reference for app
  developers (the filtering contract).
- Root `README.md` — add the boundaries tileset row/section alongside the
  camera datasets.
