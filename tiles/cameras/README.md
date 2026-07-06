# tiles/cameras

The ALPR camera tileset: ~103K points served as `cameras.pmtiles` (z0–z14, layer name `cameras`).

| File | Purpose |
|------|---------|
| `build.sh` | Fetch GeoJSON from R2 → validate → Tippecanoe → upload PMTiles. Run hourly by CI; skips the build when source data is unchanged. |
| `layers.json` | Reference MapLibre style: heatmap (`camera-heat`) at low zoom crossfading into dots (`camera-point`, `camera-glow`) at z11–13, plus direction-cone config and palette. |

## Zoom strategy

- **z0–z10** — Tippecanoe-clustered points (`--cluster-distance=5 --cluster-maxzoom=10 --keep-point-cluster-position`); clusters sit on real camera positions and carry `point_count`, which the heatmap layer uses as its linear density weight
- **z11–z14** — raw points with all source properties (`brand`, `direction`, `operator`, …) for dots, popups, and direction cones

## Manual build

```bash
export R2_DATA_BUCKET=your-data-bucket
export R2_TILES_BUCKET=your-tiles-bucket
export R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com

bash build.sh
```
