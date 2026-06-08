# deflock-data

Automated pipeline that builds PMTiles from ALPR camera data and serves them via Cloudflare R2.

## How it works

A daily GitHub Action:

1. Fetches GeoJSON from `data.dontgetflocked.com` (~90K camera locations with full properties)
2. Validates the data (non-empty, valid JSON)
3. Runs [Tippecanoe](https://github.com/felt/tippecanoe) to produce a `.pmtiles` file (z0–z14, all properties included)
4. Uploads `cameras.pmtiles` + `styles/layers.json` to Cloudflare R2

## Client integration

Two files served from R2:

| URL | Purpose |
|-----|---------|
| `tiles.dontgetflocked.com/cameras.pmtiles` | Vector tiles — single source for all zoom levels |
| `tiles.dontgetflocked.com/styles/layers.json` | Layer definitions, paint properties, animation config, palette |

Clients fetch `layers.json` on load and apply it directly — no hardcoded styles, update once and all apps get it.

### Single-source architecture

PMTiles includes all camera properties (operator, brand, direction, etc.). Clients use **one source** for both dot-density at national zoom and detail popups at close zoom. No separate GeoJSON fetch needed.

## Styles

`styles/layers.json` contains:

- **layers** — MapLibre-compatible layer definitions with zoom interpolations
- **animation** — pulse ring parameters (duration, radius/opacity ranges)
- **cones** — direction cone geometry config (radius, spread, segments)
- **palette** — color tokens used across all layers

## Setup

### Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `R2_ACCESS_KEY_ID` | Cloudflare R2 API token (access key) |
| `R2_SECRET_ACCESS_KEY` | Cloudflare R2 API token (secret key) |
| `R2_BUCKET_NAME` | Target R2 bucket name |
| `R2_ENDPOINT` | R2 S3-compatible endpoint URL |

### Manual run

```bash
brew install tippecanoe jq

export R2_BUCKET_NAME=your-bucket
export R2_ENDPOINT=https://your-account-id.r2.cloudflarestorage.com

bash scripts/build_tiles.sh
```

## License

MIT
