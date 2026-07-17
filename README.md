# deflock-data

Data & tiles hub for ALPR (automated license plate reader) camera locations — ingestion, tile pipelines, and analysis in one place:

| Hub | Contents |
|-----|----------|
| [`data/`](data/) | Code that pulls raw camera data (US + Canada) from Overpass and publishes it to R2 hourly |
| [`tiles/`](tiles/) | Tile pipelines — currently the cameras tileset, more to come |
| [`analysis/`](analysis/) | Analysis & research on the dataset |

The active piece today is the **camera tile pipeline**: every hour, a GitHub Action turns the latest camera GeoJSON (~117K points, sourced from OpenStreetMap surveillance tagging) into one [PMTiles](https://docs.protomaps.com/pmtiles/) archive **per country** served from Cloudflare R2 — no tile server required.

**Anyone can use the data.** No API key, no rate limits beyond Cloudflare's defaults.

| Dataset | Cadence | Producer | GeoJSON | TileJSON |
|---------|---------|----------|---------|----------|
| Hourly (new app) | hourly | This repo's GitHub Actions | `deflock-data` bucket: `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz` (public serving TBD) | `https://tiles.dontgetflocked.com/cameras-us-hourly.json` / `…-ca-hourly.json` (archives also mirrored to `deflock-data`) |
| Daily (FlockHopper) | daily 08:00 UTC | Cloudflare Worker cron | `https://data.dontgetflocked.com/cameras.geojson.gz` / `…-ca…` | `https://tiles.dontgetflocked.com/cameras-us.json` / `…-ca.json` (frozen: `cameras.pmtiles`) |

> The daily tiles (`cameras-us.pmtiles`, `cameras-ca.pmtiles`) and the legacy merged `cameras.pmtiles` are frozen — no pipeline rebuilds them; they keep serving FlockHopper until it migrates. After migration, also delete the orphaned `cameras-us.geojson.sha256` / `cameras-ca.geojson.sha256` hash objects from the tiles bucket.

A Cloudflare Worker in front of the R2 bucket unpacks each PMTiles archive into standard `z/x/y` tile URLs, so clients don't need the `pmtiles` protocol adapter — any MapLibre/Mapbox-compatible client can consume the TileJSON directly.

## Tile design: heatmap → dots

One tileset drives both a national heatmap and street-level dots:

- **z0–z10** — every camera as a raw, geometry-only point (no attributes, no clustering, `--drop-rate=1`). Each point contributes heatmap weight 1 at its true location, so the density surface is identical at every zoom and heat anchors never move between zoom levels. Geometry-only MVT points compress ~20:1 — the z0 tile is ~60 KB gzipped on the wire.
- **z11–z14** — raw, unclustered points with **all** source properties (`brand`, `direction`, `operator`, `osmId`, …) for individual dot rendering, popups, and direction cones.
- **z11–z13** — the crossfade zone: the heatmap fades out while dot layers fade in.

Reference MapLibre layer definitions live in [`tiles/cameras/layers.json`](tiles/cameras/layers.json) — a heatmap layer (`camera-heat`), dot layers (`camera-point`, `camera-glow`), direction-cone config, and the color palette. Clients can fetch and apply it directly, or use it as a starting point.

### Minimal client example

```js
import maplibregl from 'maplibre-gl';

const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {
      'cameras-us-hourly': {
        type: 'vector',
        url: 'https://tiles.dontgetflocked.com/cameras-us-hourly.json',
      },
      'cameras-ca-hourly': {
        type: 'vector',
        url: 'https://tiles.dontgetflocked.com/cameras-ca-hourly.json',
      },
    },
    layers: [/* see tiles/cameras/layers.json, applied once per source */],
  },
});
```

The app creates one MapLibre source per country, reusing the same [`tiles/cameras/layers.json`](tiles/cameras/layers.json) layer definitions for both. Only tiles in the current viewport are fetched.

## How the pipeline works

Two hourly GitHub Actions run back to back:

**Data ingestion** — [`.github/workflows/fetch-data.yml`](.github/workflows/fetch-data.yml) at :05 queries the Overpass API for ALPR cameras in the **US and Canada**, transforms the results to GeoJSON, validates feature counts, and uploads `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz` to the `deflock-data` R2 bucket with a 1-hour cache (no merged upload). Details in [`data/README.md`](data/README.md).

**Tile build** — [`.github/workflows/build-tiles.yml`](.github/workflows/build-tiles.yml) at :23 (and on manual dispatch) runs [`build.sh`](tiles/cameras/build.sh), which builds **one PMTiles archive per country** from `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz`. It loops the country table and re-invokes itself per country (`build.sh --country <cc>`) so a failure in one country can't block or corrupt the other's build:

1. Downloads that country's source GeoJSON.gz from the private `deflock-data` bucket — `cameras-us-hourly.geojson.gz` for US, `cameras-ca-hourly.geojson.gz` for CA
2. **Skips the build if the data hasn't changed** since the last run (per-country SHA-256 compared against `cameras-<cc>-hourly.geojson.sha256`)
3. Validates the GeoJSON against a per-country feature floor — 50,000 (US) / 300 (CA)
4. Runs [Tippecanoe](https://github.com/felt/tippecanoe) twice — a geometry-only z0–10 heat pass and a full-property z11–14 detail pass — and merges them with `tile-join`, then verifies tile invariants (`verify.sh`)
5. Sanity-checks the output size — 10 MB (US) / 82 KB (CA) floor — then uploads `cameras-us-hourly.pmtiles` or `cameras-ca-hourly.pmtiles` + the new per-country source hash to the public R2 tiles bucket, and mirrors a copy of the archive to `deflock-data`

The whole run takes a few minutes; a country whose data hasn't changed exits in seconds.

## Running it yourself

See [docs/setup-guide.md](docs/setup-guide.md) for the full walkthrough: R2 buckets, API tokens, custom domain, GitHub secrets, and local testing.

Quick local build:

```bash
brew install tippecanoe jq   # or apt-get install tippecanoe jq

export R2_DATA_BUCKET=your-data-bucket
export R2_TILES_BUCKET=your-tiles-bucket
export R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com

bash tiles/cameras/build.sh
```

## Local preview

`tiles/local-dev/` contains a zero-dependency-ish Node server that serves PMTiles as `{z}/{x}/{y}.mvt` (no range-request setup needed) plus preview pages:

```bash
cd tiles/local-dev
npm install
node server.js
# open http://localhost:3000/heatmap-preview.html
```

`heatmap-preview.html` renders the heatmap→dots style against a locally built tileset. Build a preview tileset with `bash tiles/cameras/build.sh --local <geojson> [out.pmtiles]`.

## Repo layout

```
data/
  cameras/fetch.mjs                # Overpass (US + CA) → GeoJSON, dependency-free Node
  cameras/upload.sh                # → R2 data bucket, 1hr cache + metadata
tiles/
  cameras/build.sh                 # fetch → validate → tippecanoe → upload
  cameras/layers.json              # reference MapLibre layers (heatmap + dots)
  local-dev/                       # local tile server + preview/benchmark harness
analysis/                          # analysis & research on the dataset
.github/workflows/fetch-data.yml   # hourly data ingestion (:05) + manual dispatch
.github/workflows/build-tiles.yml  # hourly tile build (:23) + manual dispatch
docs/setup-guide.md                # deploy-from-scratch walkthrough
docs/map-architecture.md           # client-side rendering architecture notes
docs/map-styling.md                # layer styling reference
```

## License

MIT. Camera location data derives from [OpenStreetMap](https://www.openstreetmap.org/copyright) (© OpenStreetMap contributors, ODbL).
