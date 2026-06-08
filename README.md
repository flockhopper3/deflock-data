# deflock-data

Automated pipeline that builds PMTiles from ALPR camera data and serves them via Cloudflare R2.

## How it works

A daily GitHub Action:

1. Fetches GeoJSON from `data.dontgetflocked.com` (~90K camera locations)
2. Validates the data (non-empty, valid JSON)
3. Runs [Tippecanoe](https://github.com/felt/tippecanoe) to produce a `.pmtiles` file (coordinates only, z0–z14)
4. Uploads to Cloudflare R2, served at `tiles.dontgetflocked.com`

## Styling

The `docs/` folder contains the map layer styling and architecture reference used by Deflock client apps:

- [`docs/map-architecture.md`](docs/map-architecture.md) — rendering architecture, data sources, layer stack
- [`docs/map-styling.md`](docs/map-styling.md) — paint properties, zoom interpolations, color palette

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
# Install tippecanoe and jq
brew install tippecanoe jq

# Set env vars
export R2_BUCKET_NAME=your-bucket
export R2_ENDPOINT=https://your-account-id.r2.cloudflarestorage.com

# Run
bash scripts/build_tiles.sh
```

## License

MIT
