# Hourly Camera Data (GeoJSON + Tiles) — Design

**Date:** 2026-07-16
**Status:** Approved

## Goal

Publish two hourly-updated GeoJSONs and two hourly-updated PMTiles archives under new names, fully separate from the daily files the Cloudflare Worker cron produces and FlockHopper consumes. The hourly set feeds a new app the user is building; the daily set is untouched.

## New outputs and endpoints

| Artifact | R2 key / object | Served at |
|----------|-----------------|-----------|
| US geojson (hourly) | `flockhopper-data:cameras-us-hourly.geojson.gz` | `https://data.dontgetflocked.com/cameras-us-hourly.geojson.gz` |
| CA geojson (hourly) | `flockhopper-data:cameras-ca-hourly.geojson.gz` | `https://data.dontgetflocked.com/cameras-ca-hourly.geojson.gz` |
| US tiles (hourly) | `flockhopper-tiles:cameras-us-hourly.pmtiles` | `https://tiles.dontgetflocked.com/cameras-us-hourly.json` + `/cameras-us-hourly/{z}/{x}/{y}.mvt` |
| CA tiles (hourly) | `flockhopper-tiles:cameras-ca-hourly.pmtiles` | `https://tiles.dontgetflocked.com/cameras-ca-hourly.json` + `/cameras-ca-hourly/{z}/{x}/{y}.mvt` |

Untouched daily set: `cameras.geojson.gz` (US), `cameras-ca.geojson.gz` (CA), `cameras-us.pmtiles`, `cameras-ca.pmtiles`, frozen `cameras.pmtiles`, the Worker cron, and FlockHopper.

## Data flow

```
Fetch Camera Data GitHub Action (hourly at :05, re-enabled)
  └─ fetch.mjs: Overpass → cameras-us-hourly.geojson, cameras-ca-hourly.geojson
  └─ upload.sh: → flockhopper-data as .geojson.gz keys (uncompressed body, per bucket convention)
flockhopper-data Worker (data.dontgetflocked.com, generic .geojson(.gz) route)
  └─ serves the new keys immediately; small tweak: shorter cache TTL for *-hourly keys
Build Tiles GitHub Action (hourly at :23, re-enabled)
  └─ build.sh: fetch cameras-<cc>-hourly.geojson.gz, sha256 skip-check,
     two-pass tippecanoe + tile-join, verify.sh, upload cameras-<cc>-hourly.pmtiles
flockhopper-tiles Worker (tiles.dontgetflocked.com, unchanged)
  └─ serves the new archives via existing generic {name}.pmtiles routing
```

## Changes by component

### 1. `data/cameras/fetch.mjs` (this repo)

- Output names gain the `-hourly` suffix: `cameras-us-hourly.geojson`, `cameras-ca-hourly.geojson` (and matching `meta.json` entries).
- **Drop the merged `cameras.geojson` output.** Nothing consumes it, and via `upload.sh` it would upload to `cameras.geojson.gz` — the daily Worker's US key — clobbering FlockHopper's data. This is the one genuinely dangerous collision; removing the merged output eliminates it.
- Feature floors unchanged (US 50,000 / CA 300).

### 2. `data/cameras/upload.sh` (this repo)

- No logic change expected — it loops over `cameras*.geojson` and uploads each as `<name>.geojson.gz`. Keeps the bucket convention: `.geojson.gz` keys hold **uncompressed** JSON (Cloudflare compresses `application/geo+json` at the edge; `build.sh` sniffs magic bytes so either encoding works downstream).

### 3. `.github/workflows/fetch-data.yml` (this repo)

- Content unchanged (hourly cron at :05 already in place). Re-enable with `gh workflow enable "Fetch Camera Data"`.

### 4. `tiles/cameras/build.sh` (this repo)

- `country_source_key()`: `us` → `cameras-us-hourly.geojson.gz`, `ca` → `cameras-ca-hourly.geojson.gz`.
- Output names: `cameras-<cc>-hourly.pmtiles`; skip-check hashes stored as `cameras-<cc>-hourly.geojson.sha256`.
- Everything else (two-pass build, per-country failure isolation, floors, verify.sh) unchanged. The old `cameras-us.pmtiles` / `cameras-ca.pmtiles` objects stay frozen in R2 exactly as `cameras.pmtiles` does.

### 5. `.github/workflows/build-tiles.yml` (this repo)

- Content unchanged (hourly cron at :23). Re-enable with `gh workflow enable "Build Tiles"`.

### 6. `flockhopper-data` Worker (repo: `FLOCK/DEFLOCK Website/DEFLOCK MAPS/FOGGED LENS/flockhopper 3/worker`)

- In `handleFetchRequest`, keys containing `-hourly` get `Cache-Control: public, max-age=300, s-maxage=3600` instead of the current `max-age=3600, s-maxage=86400`, so the new app sees hourly updates within the hour. All other keys keep the existing header. Deploy with `npx wrangler deploy`.
- The Worker cron / fetchers are not touched.

### 7. `flockhopper-tiles` Worker

- **No changes.** Generic `{name}.pmtiles` routing already serves the new archives. The etag-conditional-read fix required for hourly archive swaps is already deployed.

## Failure behavior

Carried over from the existing pipelines: per-country isolation (one country's failure doesn't block the other), feature-count floors abort a bad publish, sha256 skip-check avoids rebuilding tiles when data is unchanged, and on any failure the previous hour's objects keep serving (fail-closed / stale-but-serving).

## Testing / acceptance

1. Unit tests (`node --test data/cameras/lib.test.mjs`) still pass after the fetch.mjs rename.
2. `workflow_dispatch` **Fetch Camera Data**; confirm both `-hourly.geojson.gz` keys exist in R2 and the two daily keys' `LastModified` did **not** change (clobber check).
3. `curl -I https://data.dontgetflocked.com/cameras-us-hourly.geojson.gz` → 200, CORS header present, new shorter Cache-Control (after Worker deploy).
4. `workflow_dispatch` **Build Tiles**; verify.sh passes per country; `curl https://tiles.dontgetflocked.com/cameras-us-hourly.json` and `/cameras-ca-hourly.json` → 200; a Toronto z10 tile (`cameras-ca-hourly/10/286/373`) returns data.
5. Daily endpoints still healthy: `https://data.dontgetflocked.com/cameras.geojson.gz` 200, `https://tiles.dontgetflocked.com/cameras-us.json` 200.
6. Second dispatched Build Tiles run with unchanged data skips both countries (sha256 skip path).

## Risks

- Hourly Overpass load: fetch.mjs already uses a courtesy delay and endpoint fallback; floors catch truncated responses. Overpass 200-with-`remark` partial responses remain unchecked (pre-existing caveat, floors are the mitigation).
- The old per-country archives (`cameras-us.pmtiles`, `cameras-ca.pmtiles`) go stale from re-enable day since no pipeline rebuilds them; they were built for a FlockHopper migration that hasn't happened. Acceptable per user — revisit when FlockHopper migrates.
- Edge caches may serve the *daily* endpoint up to 24h stale (existing `s-maxage=86400`, unchanged by design).
