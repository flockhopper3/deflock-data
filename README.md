# deflock-data

Data & tiles hub for ALPR (automated license plate reader) camera locations — ingestion, tile pipelines, and analysis in one place:

| Hub | Contents |
|-----|----------|
| [`data/`](data/) | Code that pulls raw camera data (US + Canada) from Overpass and publishes it to R2 hourly |
| [`tiles/`](tiles/) | Tile pipelines — currently the cameras tileset, more to come |
| [`analysis/`](analysis/) | Analysis & research on the dataset |

The active piece today is the **camera tile pipeline**: every hour, a GitHub Action turns the latest camera GeoJSON (~103K points, sourced from OpenStreetMap surveillance tagging) into a single [PMTiles](https://docs.protomaps.com/pmtiles/) file served from Cloudflare R2 — no tile server required.

**Anyone can use the data.** No API key, no rate limits beyond Cloudflare's defaults.

| URL | What it is |
|-----|------------|
| `https://tiles.dontgetflocked.com/cameras.json` | [TileJSON](https://github.com/mapbox/tilejson-spec) for the camera tileset |
| `https://tiles.dontgetflocked.com/cameras/{z}/{x}/{y}.mvt` | Vector tiles, z0–z14, layer name `cameras` |

A Cloudflare Worker in front of the R2 bucket unpacks the PMTiles archive into standard `z/x/y` tile URLs, so clients don't need the `pmtiles` protocol adapter — any MapLibre/Mapbox-compatible client can consume the TileJSON directly.

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
      cameras: {
        type: 'vector',
        url: 'https://tiles.dontgetflocked.com/cameras.json',
      },
    },
    layers: [/* see tiles/cameras/layers.json */],
  },
});
```

Only tiles in the current viewport are fetched.

## How the pipeline works

Two hourly GitHub Actions run back to back:

**Data ingestion** — [`.github/workflows/fetch-data.yml`](.github/workflows/fetch-data.yml) at :05 queries the Overpass API for ALPR cameras in the **US and Canada**, transforms the results to GeoJSON, validates feature counts, and uploads `cameras.geojson.gz` (merged) plus `cameras-us.geojson.gz` / `cameras-ca.geojson.gz` to the R2 data bucket with a 1-hour cache. Details in [`data/README.md`](data/README.md).

**Tile build** — [`.github/workflows/build-tiles.yml`](.github/workflows/build-tiles.yml) at :23 (and on manual dispatch):

1. Downloads `cameras.geojson.gz` from the private R2 data bucket
2. **Skips the build if the data hasn't changed** since the last run (SHA-256 compared against the hash stored alongside the tiles)
3. Validates the GeoJSON (feature count sanity check)
4. Runs [Tippecanoe](https://github.com/felt/tippecanoe) twice — a geometry-only z0–10 heat pass and a full-property z11–14 detail pass — and merges them with `tile-join`, then verifies tile invariants (`verify.sh`)
5. Sanity-checks the output size, then uploads `cameras.pmtiles` + the new source hash to the public R2 tiles bucket

The whole run takes a few minutes; unchanged-data runs exit in seconds.

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
