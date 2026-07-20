# Setup Guide: GitHub → Cloudflare R2 tile pipeline

End-to-end walkthrough for deploying the hourly tile pipeline from scratch. At the end you'll have one PMTiles archive per country (`cameras-us-hourly.pmtiles`, `cameras-ca-hourly.pmtiles`) rebuilding hourly and served from public URLs.

The same R2 buckets also hold the **daily** dataset (legacy un-suffixed names, e.g. `cameras.geojson.gz` / `cameras-us.pmtiles`), produced by a separate Cloudflare Worker cron for FlockHopper — that pipeline is out of scope for this guide.

## Prerequisites

- A Cloudflare account (free plan is fine — R2 has a generous free tier)
- A GitHub account
- The source data: gzipped GeoJSON FeatureCollections of camera points, uploaded to R2 as `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz`

## 1. Cloudflare R2 buckets

Two buckets keep read and write concerns separate:

| Bucket | Purpose | Public? |
|--------|---------|---------|
| `deflock-data` | Source `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz` (pipeline writes then reads) + a mirror copy of each built `.pmtiles` archive + `pipeline/counts-history.jsonl` (the national count baseline gate's run history, read and rewritten every hourly run) | No |
| `flockhopper-tiles` | Built `cameras-us-hourly.pmtiles` / `cameras-ca-hourly.pmtiles` (pipeline writes, world reads) | Yes |

The pipeline's R2 API token is deliberately scoped to just these two buckets, so it cannot touch the separate daily dataset's bucket even by accident.

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

**Option B — direct custom domain on the bucket:** open the tiles bucket → **Settings → Public access → Custom Domains → Connect Domain** and enter a hostname on a zone you have in Cloudflare. Clients then fetch the per-country archive (`cameras-us-hourly.pmtiles` / `cameras-ca-hourly.pmtiles`) directly via HTTP range requests using the `pmtiles` JS protocol adapter.

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

`Fetch Camera Data` additionally needs `issues: write` — granted via the `permissions:` block at the top of `fetch-data.yml`, not a secret. It's what lets the national count baseline gate file or update a GitHub issue when it blocks an upload; the default `contents: read`-only repo permissions don't allow that.

## 5. First run

Don't wait for the schedule — trigger manually, ingestion first so the data bucket is populated:

**Actions → Fetch Camera Data → Run workflow**, then **Actions → Build Tiles → Run workflow**

Watch the log. `build.sh` loops the country table and re-invokes itself per country (`build.sh --country <cc>`), so a healthy first run looks like:

```
==> [us] Fetching GeoJSON from R2
==> [us] Checking whether source data changed since last build
==> [us] Validating GeoJSON
    102998 features found
==> Tippecanoe pass 1/2: heat range (z0–10, geometry-only, unclustered)
==> Tippecanoe pass 2/2: detail range (z11–14, all properties)
==> Merging zoom ranges with tile-join
==> [us] Verifying tile invariants
OK: 102998 cameras at z0, geometry-only heat range, properties intact at z12
==> [us] Uploading to Cloudflare R2
==> [us] Done. Uploaded cameras-us-hourly.pmtiles (33M)
==> [ca] Fetching GeoJSON from R2
==> [ca] Checking whether source data changed since last build
==> [ca] Validating GeoJSON
    514 features found
==> Tippecanoe pass 1/2: heat range (z0–10, geometry-only, unclustered)
==> Tippecanoe pass 2/2: detail range (z11–14, all properties)
==> Merging zoom ranges with tile-join
==> [ca] Verifying tile invariants
OK: 514 cameras at z0, geometry-only heat range, properties intact at z12
==> [ca] Uploading to Cloudflare R2
==> [ca] Done. Uploaded cameras-ca-hourly.pmtiles (160K)
==> All countries built or skipped successfully
```

Re-run it immediately and each country should instead print `Source unchanged (…) — skipping build`, e.g. `==> [ca] Source unchanged (a1b2c3d4e5f6…) — skipping build`.

## 6. Verify the served tiles

With the Worker (Option A), check each country's TileJSON and a sample tile:

```bash
curl -s https://tiles.dontgetflocked.com/cameras-us-hourly.json | jq '{minzoom, maxzoom, tiles}'
# z0 carries every camera geometry-only (~60 KB gzipped); z11+ adds full properties
curl -s -o /dev/null -w "%{http_code} %{size_download} bytes\n" https://tiles.dontgetflocked.com/cameras-us-hourly/0/0/0.mvt
```

Repeat for Canada — e.g. a Toronto-area tile:

```bash
curl -s -o /dev/null -w "%{http_code} %{size_download} bytes\n" https://tiles.dontgetflocked.com/cameras-ca-hourly/10/286/373.mvt
```

With a direct bucket domain (Option B), check range-request support instead (expect `HTTP 206`):

```bash
curl -sI -H "Range: bytes=0-16383" https://<your-domain>/cameras-us-hourly.pmtiles | head -5
```

Then point the [PMTiles viewer](https://pmtiles.io) at the file URL — geometry-only points at low zoom, full-property points from z11.

## 7. Test the heatmap → dots rendering locally

First build a local tileset (same pipeline as CI, no R2 needed), then serve it:

```bash
bash tiles/cameras/build.sh --local <geojson> tiles/local-dev/cameras-local.pmtiles
cd tiles/local-dev
npm install
node server.js   # PORT=<port> node server.js if 3000 is taken
```

Open `http://localhost:3000/heatmap-preview.html`. The preview fetches `/tiles/cameras-local.json`, which the dev server builds from `cameras-local.pmtiles`. Zoom from national level (heatmap) through z11–13 (crossfade) to street level (dots + popups).

To tune the look, edit the paint expressions in `tiles/cameras/layers.json` and mirror them in `heatmap-preview.html`:

- `heatmap-weight` — constant 1 per camera; density comes purely from point concentration
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
| `Fetch Camera Data` keeps filing/updating the same blocked-baseline issue every hour | **The block is sticky by design.** Rejected runs are recorded in `pipeline/counts-history.jsonl` but never evict accepted history, so the gate does not self-heal — it will keep rejecting on the old median forever, even if the new, lower count is correct. If the drop is a real, intentional dataset shrink (not a fetch failure), a human has to prune the stale accepted entries or otherwise accept the new baseline in `pipeline/counts-history.jsonl`; waiting it out will not work. |
