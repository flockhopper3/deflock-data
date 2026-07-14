# Per-Country Camera Tiles — Design

**Date:** 2026-07-13
**Status:** Approved (approach A: country loop in build.sh)

## Goal

Replace the single merged `cameras.pmtiles` with one PMTiles archive per country — `cameras-us.pmtiles` and `cameras-ca.pmtiles` — built hourly from the per-country GeoJSONs the Worker cron already writes to R2. This puts Canadian cameras on the map (the merged key the old build consumed was US-only) and gives the app independent per-country sources.

## Decisions made

- **Per-country only.** The merged `cameras.pmtiles` is no longer built. The app will add one MapLibre source per country (app repo follow-up, out of scope here).
- **Source keys:** `cameras.geojson.gz` (US) and `cameras-ca.geojson.gz` (CA) in `flockhopper-data`, refreshed hourly by the Worker cron (confirmed by user). US is the legacy un-suffixed key — confirmed against the Worker's fetcher registry; the spec previously assumed `cameras-us.geojson.gz`. The Actions ingestion cutover remains a separate, later project.
- **Transition:** the existing merged `cameras.pmtiles` object stays in R2, frozen, so the current app keeps serving until it migrates. Deleted afterward, along with the orphaned `cameras.geojson.sha256`.

## Data flow

```
Worker cron (hourly, unchanged)
  └─ writes R2 flockhopper-data: cameras-us.geojson.gz, cameras-ca.geojson.gz
Build Tiles GitHub Action (hourly, workflow file unchanged)
  └─ build.sh: for each country in table
       fetch cameras-<cc>.geojson.gz
       skip-check vs cameras-<cc>.geojson.sha256 (hash includes BUILD_CONFIG)
       two-pass tippecanoe (z0–10 geometry-only, z11–14 full props) + tile-join
       verify.sh <file> <count>
       upload cameras-<cc>.pmtiles + cameras-<cc>.geojson.sha256
flockhopper-tiles Worker (unchanged, generic {name}.pmtiles routing)
  └─ serves /cameras-us/{z}/{x}/{y}.mvt, /cameras-us.json, same for -ca
```

## Country table (in build.sh)

| Country | Source key | Output | Min features | Min output size |
|---------|------------|--------|--------------|-----------------|
| us | cameras.geojson.gz (legacy key) | cameras-us.pmtiles | 50,000 | 10 MB |
| ca | cameras-ca.geojson.gz | cameras-ca.pmtiles | 300 | 100 KB |

Floors mirror `data/cameras/fetch.mjs`. Size floors become per-country because the old global 10 MB minimum would reject Canada's ~1K-camera archive.

## Failure isolation

Each country builds inside its own guarded block. A failure in one country (fetch, validation, build, verify) must not prevent other countries from building and uploading. The script exits non-zero at the end if any country failed, naming the failures, so CI still reports red.

## Unchanged components

- `verify.sh` — already parameterized `<pmtiles> <expected_count>`; runs per country before upload.
- `.github/workflows/build-tiles.yml` — still just runs `build.sh`.
- The tile Worker — `{name}` routing already serves any archive in the bucket.
- `build.sh --local <geojson> [out]` — unchanged; works for any single GeoJSON.
- Style layer definitions (`tiles/cameras/layers.json` paint/layout) — only the source URL changes, and that lives app-side. The repo copy's `source.url` should point at one of the new TileJSONs (us) with a comment noting the per-country scheme.

## Cleanup / docs

- README.md, tiles/cameras/README.md, docs/setup-guide.md: describe per-country outputs and URLs.
- After the app migrates (out of scope): delete `cameras.pmtiles` and `cameras.geojson.sha256` from `flockhopper-tiles`.

## Testing

- Local: `bash tiles/cameras/build.sh --local <downloaded cameras-ca.geojson>` then `verify.sh` (CA is small and fast); shellcheck.
- The hourly-path loop tested by running `build.sh` in R2 mode from CI (workflow_dispatch) and confirming both archives upload, one skip-run behaves per-country, and a simulated bad country (feature floor) fails that country only.
- Prod acceptance: `/cameras-us.json` and `/cameras-ca.json` return 200; a Toronto z10 tile (`10/286/373`) returns data; the frozen `/cameras/{z}/{x}/{y}.mvt` still serves.

## Risks

- App outage risk is nil during transition (frozen merged archive keeps serving), but merged data goes stale from day one — app migration should follow promptly.
- If the Worker cron's per-country keys ever lag or vanish, per-country floors abort that country's build and the previous archive keeps serving (fail-closed, same as today).
