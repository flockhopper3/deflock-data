# data/

Ingestion code — the scripts that pull raw camera data (OpenStreetMap surveillance tagging) and publish it to the R2 data bucket.

## Camera ingestion (`cameras/`)

[`.github/workflows/fetch-data.yml`](../.github/workflows/fetch-data.yml) runs hourly at :05 (and on manual dispatch):

1. [`fetch.mjs`](cameras/fetch.mjs) queries the [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) for `man_made=surveillance` + `surveillance:type=ALPR` nodes/ways, once for the **US** and once for **Canada**, with automatic fallback across three Overpass endpoints
2. Elements are transformed to GeoJSON point features (way geometries become centroids; direction tags are normalized to degrees) — logic ported from the original Cloudflare data worker, tested in [`lib.test.mjs`](cameras/lib.test.mjs)
3. Feature counts are validated against per-country minimums so a truncated Overpass response never overwrites good data
4. [`upload.sh`](cameras/upload.sh) uploads the two per-country datasets to the R2 data bucket with `Cache-Control: public, max-age=3600` (1-hour cache) and `x-last-updated` / `x-feature-count` / `x-source` metadata

| R2 key | Contents |
|--------|----------|
| `cameras-us-hourly.geojson.gz` | US only |
| `cameras-ca-hourly.geojson.gz` | Canada only |

There is no merged output — the merged upload key (`cameras.geojson.gz`) is the daily Worker cron's US key, and this pipeline must never write the daily dataset.

> **Note:** the `.gz` keys hold *uncompressed* JSON — the original worker always stored them that way (Cloudflare compresses at the edge) and existing consumers depend on it.

The tile build (`build-tiles.yml`) is chained via `workflow_run` — it starts as soon as a fetch run completes successfully and picks up the fresh `cameras-<cc>-hourly.geojson.gz` keys immediately.

## Running locally

```bash
node --test data/cameras/*.test.mjs   # unit tests
node data/cameras/fetch.mjs --out /tmp/data-out

export R2_DATA_BUCKET=your-data-bucket
export R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
bash data/cameras/upload.sh /tmp/data-out   # needs aws CLI + jq
```

No npm install needed — the scripts are dependency-free (Node 20+).
