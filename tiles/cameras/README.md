# tiles/cameras

The ALPR camera tileset: ~117K points served as one PMTiles archive per country — `cameras-us-hourly.pmtiles` and `cameras-ca-hourly.pmtiles` (z0–z14, layer name `cameras`).

| File | Purpose |
|------|---------|
| `build.sh` | Fetch GeoJSON from R2 → validate → Tippecanoe → upload PMTiles, once per country. Loops the country table (`us`, `ca`) and re-invokes itself per country (`build.sh --country <cc>`) for failure isolation. Run hourly by CI; skips a country's build when its source (`cameras-<cc>-hourly.geojson.gz`) is unchanged (per-country hash: `cameras-<cc>-hourly.geojson.sha256`). |
| `layers.json` | Reference MapLibre style: heatmap (`camera-heat`) at low zoom crossfading into dots (`camera-point`, `camera-glow`) at z11–13, plus direction-cone config and palette. |

## Zoom strategy

- **z0–z10** — raw, geometry-only points (`--exclude-all --drop-rate=1`, no clustering); every camera sits at its true location at every zoom so the heatmap never shifts on zoom transitions
- **z11–z14** — raw points with all source properties (`brand`, `direction`, `operator`, …) for dots, popups, and direction cones

## Manual build

```bash
export R2_DATA_BUCKET=your-data-bucket
export R2_TILES_BUCKET=your-tiles-bucket
export R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com

bash build.sh
```
