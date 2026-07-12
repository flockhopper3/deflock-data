# Setup Guide: GitHub → Cloudflare R2 tile pipeline

End-to-end walkthrough for deploying the hourly tile pipeline from scratch. At the end you'll have `cameras.pmtiles` rebuilding hourly and served from a public URL.

## Prerequisites

- A Cloudflare account (free plan is fine — R2 has a generous free tier)
- A GitHub account
- The source data: a gzipped GeoJSON FeatureCollection of camera points, uploaded to R2 as `cameras.geojson.gz`

## 1. Cloudflare R2 buckets

Two buckets keep read and write concerns separate:

| Bucket | Purpose | Public? |
|--------|---------|---------|
| `flockhopper-data` | Source `cameras.geojson.gz` (pipeline reads) | No |
| `flockhopper-tiles` | Built `cameras.pmtiles` (pipeline writes, world reads) | Yes |

In the Cloudflare dashboard: **R2 → Create bucket**, once per bucket. Location "Automatic" is fine.

## 2. R2 API token

The GitHub Action talks to R2 through the S3-compatible API.

1. **R2 → Manage R2 API Tokens → Create API Token**
2. Permissions: **Object Read & Write**, scoped to only the two buckets above
3. Save the three values it shows you:
   - Access Key ID
   - Secret Access Key
   - Endpoint (`https://<account-id>.r2.cloudflarestorage.com`)

## 3. Public access for the tiles bucket

PMTiles needs the bucket to answer HTTP range requests, which R2 supports out of the box. Two options:

**Option A — tile-serving Worker (what this deployment uses):** a Cloudflare Worker bound to the tiles bucket unpacks the PMTiles archive and serves `/{name}/{z}/{x}/{y}.mvt` + TileJSON at `/{name}.json` on `tiles.dontgetflocked.com`. Protomaps publishes a ready-made one: [protomaps/PMTiles serverless for Cloudflare](https://docs.protomaps.com/deploy/cloudflare). Clients get plain `z/x/y` URLs — no `pmtiles` protocol adapter needed, and each tile caches independently at the edge.

**Option B — direct custom domain on the bucket:** open the tiles bucket → **Settings → Public access → Custom Domains → Connect Domain** and enter a hostname on a zone you have in Cloudflare. Clients then fetch `cameras.pmtiles` directly via HTTP range requests using the `pmtiles` JS protocol adapter.

**CORS** — clients on other origins need it. Bucket → **Settings → CORS policy**:

```json
[
  {
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["range", "if-match"],
    "ExposeHeaders": ["etag", "content-length", "content-range"],
    "MaxAgeSeconds": 86400
  }
]
```

**Cache rule (important for hourly updates):** the PMTiles file is overwritten in place every hour, so don't let the edge cache it for a day. Dashboard → your zone → **Caching → Cache Rules**: for hostname `tiles.dontgetflocked.com`, set **Edge TTL** to ~1 hour and **Browser TTL** to ~5 minutes. Range requests are cached per-range, so this works fine with PMTiles.

## 4. GitHub repository

1. Push this repo to GitHub. **Make it public** — public repos get unlimited free Actions minutes, which matters for an hourly schedule (a private repo would burn ~1,500–3,000 min/month against a 2,000-minute free quota).
2. **Settings → Secrets and variables → Actions → New repository secret**, three times:

| Secret | Value |
|--------|-------|
| `R2_ACCESS_KEY_ID` | from step 2 |
| `R2_SECRET_ACCESS_KEY` | from step 2 |
| `R2_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |

Bucket names are not secret; they're set as plain env vars in the workflow file — edit them there if yours differ.

The same three secrets power both workflows — data ingestion (`Fetch Camera Data`) and the tile build (`Build Tiles`).

## 5. First run

Don't wait for the schedule — trigger manually, ingestion first so the data bucket is populated:

**Actions → Fetch Camera Data → Run workflow**, then **Actions → Build Tiles → Run workflow**

Watch the log. A healthy run looks like:

```
==> Fetching GeoJSON from R2
==> Checking whether source data changed since last build
==> Validating GeoJSON
    102998 features found
==> Running Tippecanoe
==> Uploading to Cloudflare R2
==> Done. Uploaded cameras.pmtiles (35M)
```

Re-run it immediately and you should instead see `Source unchanged — skipping build`.

## 6. Verify the served tiles

With the Worker (Option A):

```bash
curl -s https://tiles.dontgetflocked.com/cameras.json | jq '{minzoom, maxzoom, tiles}'
# z0 should be tiny (a few KB) — clustered; z11+ carries raw points
curl -s -o /dev/null -w "%{http_code} %{size_download} bytes\n" https://tiles.dontgetflocked.com/cameras/0/0/0.mvt
```

With a direct bucket domain (Option B), check range-request support instead (expect `HTTP 206`):

```bash
curl -sI -H "Range: bytes=0-16383" https://<your-domain>/cameras.pmtiles | head -5
```

Then point the [PMTiles viewer](https://pmtiles.io) at the file URL — clustered points at low zoom, raw points from z11.

## 7. Test the heatmap → dots rendering locally

```bash
cd tiles/local-dev
npm install
node server.js
```

Open `http://localhost:3000/heatmap-preview.html`. It renders whatever `cameras-heatmap.pmtiles` is present locally (build one with the same flags as `tiles/cameras/build.sh`, or copy the production file). Zoom from national level (heatmap) through z11–13 (crossfade) to street level (dots + popups).

To tune the look, edit the paint expressions in `tiles/cameras/layers.json` and mirror them in `heatmap-preview.html`:

- `heatmap-weight` — how much each cluster contributes; keyed off `point_count`
- `heatmap-radius` / `heatmap-intensity` — blob size and brightness per zoom
- The z11–13 opacity ramps on `camera-heat` vs `camera-point`/`camera-glow` — the crossfade

## 8. Schedule notes

- The cron is `23 * * * *` — GitHub heavily delays jobs scheduled at :00, so keep an odd minute.
- Scheduled workflows are **automatically disabled after 60 days without repo activity**. Any commit resets the clock; watch for the warning email from GitHub.
- The `concurrency: tile-build` group means a slow run can't overlap the next hour's run.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `jq: parse error` right after fetch | Source object wasn't valid JSON — the script gunzips only if it sees gzip magic bytes, so check what's actually stored in the data bucket |
| `Only N features — expected 100K+` | Upstream data generation failed or produced a partial file; the guard aborted before touching prod tiles |
| Browser gets CORS errors on the tiles URL | CORS policy missing on the tiles bucket (step 3) |
| Map shows stale data hours after a build | Edge cache TTL too long — add the cache rule from step 3 |
| Action fails with `SignatureDoesNotMatch` | Wrong `R2_SECRET_ACCESS_KEY` or endpoint account ID mismatch |
