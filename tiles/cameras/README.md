# tiles/cameras

The ALPR camera tileset: ~117K points served as one PMTiles archive per country — `cameras-us-hourly.pmtiles` and `cameras-ca-hourly.pmtiles` (z0–z14, layer name `cameras`) — plus a filter companion set per country (`cameras-<cc>-hourly-filter.pmtiles` + `cameras-<cc>-hourly-manifest.json`).

| File | Purpose |
|------|---------|
| `build.sh` | Fetch GeoJSON from R2 → validate → Tippecanoe → upload PMTiles, once per country. Loops the country table (`us`, `ca`) and re-invokes itself per country (`build.sh --country <cc>`) for failure isolation. Run hourly by CI; skips a country's build when its source (`cameras-<cc>-hourly.geojson.gz`) is unchanged (per-country hash: `cameras-<cc>-hourly.geojson.sha256`). If `R2_TILES_MIRROR_BUCKET` is set, each archive is also copied there (no hash file in the mirror). Also builds and uploads the filter companion set (below) from the same GeoJSON in the same run. |
| `enrich.mjs` | Adds four integer filter codes to every feature — `b` brand (normalization ported from DeFlock Maps), `o` operator, `z` surveillance zone, `m` mount type; `0` = missing/unknown, brand/operator ids `1..N` by descending count — and writes the code→label manifest. Ids are **build-scoped**: the manifest and filter tileset only make sense as the matched pair from one build. |
| `verify-filter.sh` | Filter-archive invariants: full count at z0, heat range carries exactly `b/o/z/m` (integers), detail range keeps original properties plus codes. |
| `layers.json` | Reference MapLibre style: heatmap (`camera-heat`) at low zoom crossfading into dots (`camera-point`, `camera-glow`) at z11–13, plus direction-cone config and palette. |

## Zoom strategy

- **z0–z10** — raw, geometry-only points (`--exclude-all --drop-rate=1`, no clustering); every camera sits at its true location at every zoom so the heatmap never shifts on zoom transitions
- **z11–z14** — raw points with all source properties (`brand`, `direction`, `operator`, …) for dots, popups, and direction cones

The filter archive mirrors this exactly (same drop settings, so per-tile feature counts match), except its z0–z10 range carries the four filter codes instead of nothing — the app attaches it only while a brand/operator/zone/mount filter is active, and resolves labels→ids at runtime through the manifest.

## Manual build

```bash
export R2_DATA_BUCKET=your-data-bucket
export R2_TILES_BUCKET=your-tiles-bucket
export R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com

bash build.sh
```
