# Geometry-Only Heat Tiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace tippecanoe clustering in the z0–10 heatmap zoom range with geometry-only raw points, so heat anchors never move across zoom levels and the heatmap reads as one coherent surface.

**Architecture:** `tiles/cameras/build.sh` currently runs one tippecanoe pass with `--cluster-distance=5 --cluster-maxzoom=10 --keep-point-cluster-position`. Tippecanoe re-clusters independently per zoom, so heat anchors jump (measured: median 2.3 km per zoom crossing at z6→z7 over Atlanta). The fix: two tippecanoe passes — a geometry-only (`--exclude-all`) unclustered pass for z0–10 and a full-property pass for z11–14 — merged with `tile-join`. Geometry-only MVT points compress ~20:1 (z0 tile: 60 KB gzipped vs 4.4 KB clustered — measured, acceptable). A new `verify.sh` enforces the invariants (z0 carries every camera, no `point_count`, no attributes in heat range, `osmId` present in detail range) both locally and in CI before upload.

**Tech Stack:** bash, felt/tippecanoe 2.79.0 (`tippecanoe`, `tippecanoe-decode`, `tile-join`), jq, GitHub Actions, Cloudflare R2, MapLibre style JSON.

## Global Constraints

- Tippecanoe version: felt/tippecanoe **2.79.0** (built from source in CI; apt's Mapbox fork can't write PMTiles).
- Layer name in all tiles: `cameras` (apps depend on `source-layer: "cameras"`).
- Zoom split: heat = **z0–z10**, detail = **z11–z14**. Never drop features: `--drop-rate=1` in every pass.
- `BUILD_CONFIG` string in `build.sh` MUST be bumped whenever tippecanoe flags change (skip-check hash includes it). This change bumps it to `v3-geomonly-heat`.
- CI cache key MUST change when the set of cached tippecanoe binaries changes (old cache lacks `tile-join`). New key: `tippecanoe-2.79.0-r2`.
- Output filename stays `cameras.pmtiles`; R2 buckets and Worker URLs are unchanged.
- Do not modify `data/` (ingestion) or `analysis/` — out of scope.

**Branch:** create `git checkout -b geometry-only-heat` before Task 1.

---

### Task 1: `verify.sh` — tile invariants checker (the failing test)

**Files:**
- Create: `tiles/cameras/verify.sh`

**Interfaces:**
- Produces: `verify.sh <file.pmtiles> <expected_feature_count>` — exits 0 if all invariants hold, exits 1 with a `FAIL:` message otherwise. Task 2's `build.sh` calls it as `bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}" "${FEATURE_COUNT}"`.
- Requires on PATH: `tippecanoe-decode`, `jq`, `awk`.

- [ ] **Step 1: Write the verifier**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Verifies invariants of a built cameras PMTiles:
#   1. the z0 tile contains every camera exactly once (no clustering, no drops)
#   2. heat-range tiles are geometry-only (no attributes, no point_count)
#   3. detail-range tiles keep full properties (osmId present at z12)
#
# Usage: verify.sh <file.pmtiles> <expected_feature_count>

FILE="${1:?usage: verify.sh <pmtiles> <expected_count>}"
EXPECTED="${2:?usage: verify.sh <pmtiles> <expected_count>}"

fail() { echo "FAIL: $1"; exit 1; }

Z0_JSON=$(tippecanoe-decode "${FILE}" 0 0 0)

Z0_COUNT=$(jq '[.features[].features | length] | add // 0' <<<"${Z0_JSON}")
[ "${Z0_COUNT}" = "${EXPECTED}" ] \
  || fail "z0 has ${Z0_COUNT} features, expected ${EXPECTED} — clustering or dropping present"

CLUSTERED=$(jq '[.features[].features[].properties | select(has("point_count"))] | length' <<<"${Z0_JSON}")
[ "${CLUSTERED}" = "0" ] \
  || fail "${CLUSTERED} z0 features carry point_count — clustered tiles detected"

ATTRS=$(jq '[.features[].features[].properties | length] | add // 0' <<<"${Z0_JSON}")
[ "${ATTRS}" = "0" ] \
  || fail "z0 features carry ${ATTRS} attributes — heat tiles should be geometry-only"

# Detail range: decode the z12 tile containing the first camera and check osmId survives.
read -r LON LAT <<<"$(jq -r '.features[0].features[0].geometry.coordinates | "\(.[0]) \(.[1])"' <<<"${Z0_JSON}")"
read -r TX TY <<<"$(awk -v lon="${LON}" -v lat="${LAT}" 'BEGIN {
  z = 12; n = 2^z; pi = 3.14159265358979; r = lat * pi / 180;
  x = int((lon + 180) / 360 * n);
  y = int((1 - log(sin(r)/cos(r) + 1/cos(r)) / pi) / 2 * n);
  print x, y }')"
WITH_ID=$(tippecanoe-decode "${FILE}" 12 "${TX}" "${TY}" \
  | jq '[.features[].features[].properties | select(has("osmId"))] | length')
[ "${WITH_ID}" -gt 0 ] \
  || fail "z12 tile ${TX}/${TY} has no features with osmId — detail range lost its properties"

echo "OK: ${Z0_COUNT} cameras at z0, geometry-only heat range, properties intact at z12"
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x tiles/cameras/verify.sh`

- [ ] **Step 3: Run against the current clustered build — verify it FAILS**

Run:
```bash
EXPECTED=$(jq '.features | length' tiles/cameras.geojson)
bash tiles/cameras/verify.sh tiles/local-dev/cameras-heatmap.pmtiles "${EXPECTED}"
```
Expected: exit 1 with `FAIL: z0 has <small number> features, expected <~114000> — clustering or dropping present`. (The current build clusters z0–10, so the z0 tile holds a few hundred cluster points, not every camera.)

Note: if the count assertion in later tasks turns out flaky because tippecanoe coalesces exactly-coincident points, relax assertion 1 to `Z0_COUNT >= EXPECTED * 99 / 100` — but only if Task 2's run actually shows a mismatch; do not preemptively weaken it.

- [ ] **Step 4: Commit**

```bash
git add tiles/cameras/verify.sh
git commit -m "Add verify.sh asserting unclustered geometry-only heat tiles

Fails against the current clustered build by design — the build change
lands next.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Two-pass build in `build.sh` with `--local` mode

**Files:**
- Modify: `tiles/cameras/build.sh` (full rewrite below)

**Interfaces:**
- Consumes: `tiles/cameras/verify.sh <pmtiles> <count>` from Task 1.
- Produces: `build.sh` (R2 mode, unchanged CLI for CI) and `build.sh --local <geojson> [out.pmtiles]` (no R2, no hash check, no upload — used by local preview in Task 4). Tippecanoe flags live ONLY here; nothing else in the repo defines build flags.

- [ ] **Step 1: Replace `tiles/cameras/build.sh` with**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Builds cameras.pmtiles from camera GeoJSON and (in R2 mode) uploads it.
#
# R2 mode (default — used by CI). Required env:
#   R2_DATA_BUCKET   — bucket holding cameras.geojson.gz (read)
#   R2_TILES_BUCKET  — bucket the built PMTiles are uploaded to (write)
#   R2_ENDPOINT      — R2 S3-compatible endpoint URL
#
# Local mode (preview pipeline/style changes without R2):
#   build.sh --local <cameras.geojson> [output.pmtiles]
#
# Zoom strategy (heatmap → dots):
#   z0–z10  geometry-only raw points — feeds the heatmap layer. Every camera
#           appears at its true location at every zoom, so heat anchors never
#           move between zoom levels. (Tippecanoe clustering re-anchors every
#           zoom independently — that made the heatmap "shift" on zoom.)
#   z11–z14 raw points with all properties — feeds dot + popup layers.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOCAL=0
if [ "${1:-}" = "--local" ]; then
  LOCAL=1
  GEOJSON_FILE="${2:?usage: build.sh --local <cameras.geojson> [output.pmtiles]}"
  OUTPUT_FILE="${3:-cameras-local.pmtiles}"
else
  OUTPUT_FILE="cameras.pmtiles"
  GEOJSON_FILE="cameras.geojson"
  HASH_FILE="cameras.geojson.sha256"

  echo "==> Fetching GeoJSON from R2"
  aws s3 cp "s3://${R2_DATA_BUCKET}/cameras.geojson.gz" "${GEOJSON_FILE}.download" \
    --endpoint-url "${R2_ENDPOINT}"

  # The object may or may not actually be gzip-compressed — check magic bytes.
  if [ "$(head -c 2 "${GEOJSON_FILE}.download" | xxd -p)" = "1f8b" ]; then
    echo "==> Decompressing gzip"
    gunzip -c "${GEOJSON_FILE}.download" > "${GEOJSON_FILE}"
    rm "${GEOJSON_FILE}.download"
  else
    mv "${GEOJSON_FILE}.download" "${GEOJSON_FILE}"
  fi

  echo "==> Checking whether source data changed since last build"
  # Bump BUILD_CONFIG whenever tippecanoe flags change so the skip check
  # doesn't short-circuit a rebuild with unchanged source data.
  BUILD_CONFIG="v3-geomonly-heat"
  NEW_HASH="$(shasum -a 256 "${GEOJSON_FILE}" | cut -d' ' -f1)-${BUILD_CONFIG}"
  OLD_HASH=$(aws s3 cp "s3://${R2_TILES_BUCKET}/${HASH_FILE}" - \
    --endpoint-url "${R2_ENDPOINT}" 2>/dev/null || echo "none")
  if [ "${NEW_HASH}" = "${OLD_HASH}" ]; then
    echo "==> Source unchanged (${NEW_HASH:0:12}…) — skipping build"
    exit 0
  fi
fi

echo "==> Validating GeoJSON"
FEATURE_COUNT=$(jq '.features | length' "${GEOJSON_FILE}")
if [ "${LOCAL}" = "0" ] && [ "${FEATURE_COUNT}" -lt 1000 ]; then
  echo "ERROR: Only ${FEATURE_COUNT} features — expected 100K+. Aborting to protect prod."
  exit 1
fi
echo "    ${FEATURE_COUNT} features found"

HEAT_TMP="$(mktemp -u).heat.pmtiles"
DETAIL_TMP="$(mktemp -u).detail.pmtiles"
trap 'rm -f "${HEAT_TMP}" "${DETAIL_TMP}"' EXIT

echo "==> Tippecanoe pass 1/2: heat range (z0–10, geometry-only, unclustered)"
tippecanoe \
  -o "${HEAT_TMP}" \
  --force \
  --exclude-all \
  --no-feature-limit \
  --no-tile-size-limit \
  --drop-rate=1 \
  --minimum-zoom=0 \
  --maximum-zoom=10 \
  --no-tile-stats \
  --layer=cameras \
  "${GEOJSON_FILE}"

echo "==> Tippecanoe pass 2/2: detail range (z11–14, all properties)"
tippecanoe \
  -o "${DETAIL_TMP}" \
  --force \
  --no-feature-limit \
  --no-tile-size-limit \
  --drop-rate=1 \
  --minimum-zoom=11 \
  --maximum-zoom=14 \
  --no-tile-stats \
  --layer=cameras \
  "${GEOJSON_FILE}"

echo "==> Merging zoom ranges with tile-join"
tile-join -o "${OUTPUT_FILE}" --force --no-tile-size-limit \
  "${HEAT_TMP}" "${DETAIL_TMP}"

echo "==> Verifying tile invariants"
bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}" "${FEATURE_COUNT}"

if [ "${LOCAL}" = "1" ]; then
  echo "==> Done (local). Built ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
  exit 0
fi

FILE_SIZE=$(stat -f%z "${OUTPUT_FILE}" 2>/dev/null || stat -c%s "${OUTPUT_FILE}")
MIN_SIZE=$((10 * 1024 * 1024))
if [ "${FILE_SIZE}" -lt "${MIN_SIZE}" ]; then
  echo "ERROR: Output is only $(du -h "${OUTPUT_FILE}" | cut -f1) — suspiciously small. Aborting."
  exit 1
fi

echo "==> Uploading to Cloudflare R2"
aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_BUCKET}/${OUTPUT_FILE}" \
  --endpoint-url "${R2_ENDPOINT}"
echo "${NEW_HASH}" | aws s3 cp - "s3://${R2_TILES_BUCKET}/${HASH_FILE}" \
  --endpoint-url "${R2_ENDPOINT}"

rm -f "${GEOJSON_FILE}"

echo "==> Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
```

- [ ] **Step 2: Run the local build against the checked-out GeoJSON**

Run:
```bash
cd tiles/local-dev
bash ../cameras/build.sh --local ../cameras.geojson cameras-local.pmtiles
```
Expected: both tippecanoe passes run, tile-join merges, then `OK: <~114000> cameras at z0, geometry-only heat range, properties intact at z12`, then `==> Done (local). Built cameras-local.pmtiles (…)`. If the z0-count assertion fails with a near-miss (coincident-point coalescing), apply the tolerance fallback documented in Task 1 Step 3 and re-run.

- [ ] **Step 3: Confirm verify.sh still fails the OLD build (test discriminates)**

Run: `EXPECTED=$(jq '.features | length' ../cameras.geojson); bash ../cameras/verify.sh cameras-heatmap.pmtiles "${EXPECTED}"`
Expected: exit 1, `FAIL: z0 has … — clustering or dropping present`.

- [ ] **Step 4: Commit**

```bash
git add tiles/cameras/build.sh
git commit -m "Build heat range z0-10 as geometry-only raw points, drop clustering

Tippecanoe re-clusters each zoom independently, so heat anchors moved
(median 2.3km per zoom crossing measured over Atlanta) and the heatmap
visibly shifted on every zoom. Raw geometry-only points are stable by
construction and cost about the same on the wire (z0: 60KB gzipped).
Two passes merged with tile-join; adds --local mode for preview builds.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: CI — ship `tile-join`, bump cache key

**Files:**
- Modify: `.github/workflows/build-tiles.yml:18-36` (cache + build-tippecanoe steps)

**Interfaces:**
- Produces: `tile-join` on PATH for `build.sh` (Task 2) in CI. `jq` is preinstalled on `ubuntu-latest`; `verify.sh` needs no workflow changes.

- [ ] **Step 1: Update the tippecanoe comment, cache key, and binary copy**

Replace this block:

```yaml
      # The apt-packaged tippecanoe is the old Mapbox fork and lacks flags we
      # use (--keep-point-cluster-position), so build felt/tippecanoe once and
      # cache the binary.
      - name: Cache tippecanoe
        id: cache-tippecanoe
        uses: actions/cache@v4
        with:
          path: ~/tippecanoe-bin
          key: tippecanoe-2.79.0
```

with:

```yaml
      # The apt-packaged tippecanoe is the old Mapbox fork and can't write
      # PMTiles directly, so build felt/tippecanoe once and cache the
      # binaries. Cache key is suffixed because the cached set of binaries
      # changed (added tile-join) — bump the suffix if it changes again.
      - name: Cache tippecanoe
        id: cache-tippecanoe
        uses: actions/cache@v4
        with:
          path: ~/tippecanoe-bin
          key: tippecanoe-2.79.0-r2
```

And in the `Build tippecanoe` step, replace:

```yaml
          cp /tmp/tippecanoe/tippecanoe /tmp/tippecanoe/tippecanoe-decode ~/tippecanoe-bin/
```

with:

```yaml
          cp /tmp/tippecanoe/tippecanoe /tmp/tippecanoe/tippecanoe-decode /tmp/tippecanoe/tile-join ~/tippecanoe-bin/
```

- [ ] **Step 2: Validate workflow syntax**

Run: `gh workflow view "Build Tiles" >/dev/null && ruby -ryaml -e 'YAML.load_file(".github/workflows/build-tiles.yml")' && echo YAML-OK` (or `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/build-tiles.yml'))" && echo YAML-OK`)
Expected: `YAML-OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/build-tiles.yml
git commit -m "Cache tile-join alongside tippecanoe, bump cache key

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Style — constant heatmap weight; preview against the new tiles

**Files:**
- Modify: `tiles/cameras/layers.json:2` (version) and `:37` (weight)
- Modify: `tiles/local-dev/heatmap-preview.html:42` (weight) and its TileJSON URL

**Interfaces:**
- Consumes: `cameras-local.pmtiles` built in Task 2 Step 2 (served by `tiles/local-dev/server.js` as `/tiles/cameras-local.json`).
- Produces: `layers.json` version 3 — the canonical style apps copy. Heatmap weight is a constant `1`.

- [ ] **Step 1: Update `layers.json`**

Change `"version": 2,` → `"version": 3,` and change:

```json
        "heatmap-weight": ["coalesce", ["get", "point_count"], 1],
```

to:

```json
        "heatmap-weight": 1,
```

(All other paint properties — intensity, radius, color ramp, opacity crossfade — were tuned for heat *totals*, which linear `point_count` weighting already conserved; a constant weight of 1 per raw camera produces the same totals, so they stay.)

- [ ] **Step 2: Update `heatmap-preview.html`**

Change line 42:

```js
              'heatmap-weight': ['coalesce', ['get', 'point_count'], 1],
```

to:

```js
              'heatmap-weight': 1,
```

and point its TileJSON URL at the new local build: replace the string `cameras-heatmap.json` with `cameras-local.json`.

- [ ] **Step 3: Visual check — the actual bug**

Run: `cd tiles/local-dev && node server.js` (if not already running), open `http://localhost:3000/heatmap-preview.html`.
Expected: zooming z4→z10 over a dense metro (Atlanta, Dallas), heat blobs sharpen in place — no lateral jumps, no blob reshuffling at integer zoom crossings, no brightness steps.

- [ ] **Step 4: Commit**

```bash
git add tiles/cameras/layers.json tiles/local-dev/heatmap-preview.html
git commit -m "Style v3: constant heatmap weight for unclustered heat tiles

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Docs — describe the new zoom strategy

**Files:**
- Modify: `README.md:26-27,65`
- Modify: `tiles/cameras/README.md:12`
- Modify: `docs/setup-guide.md:98,108,122`

(`docs/map-styling.md` and `docs/map-architecture.md` need NO changes — their "unclustered-point" mentions are app layer names, unrelated to tippecanoe clustering.)

- [ ] **Step 1: `README.md`** — replace line 26 with:

```markdown
- **z0–z10** — every camera as a raw, geometry-only point (no attributes, no clustering, `--drop-rate=1`). Each point contributes heatmap weight 1 at its true location, so the density surface is identical at every zoom and heat anchors never move between zoom levels. Geometry-only MVT points compress ~20:1 — the z0 tile is ~60 KB gzipped on the wire.
```

Line 27 (z11–z14) is unchanged. Replace line 65 (`4. Runs [Tippecanoe]… with the clustered-then-raw zoom strategy above`) with:

```markdown
4. Runs [Tippecanoe](https://github.com/felt/tippecanoe) twice — a geometry-only z0–10 heat pass and a full-property z11–14 detail pass — and merges them with `tile-join`, then verifies tile invariants (`verify.sh`)
```

- [ ] **Step 2: `tiles/cameras/README.md`** — replace line 12 with:

```markdown
- **z0–z10** — raw, geometry-only points (`--exclude-all --drop-rate=1`, no clustering); every camera sits at its true location at every zoom so the heatmap never shifts on zoom transitions
```

- [ ] **Step 3: `docs/setup-guide.md`** — replace:

Line 98: `# z0 should be tiny (a few KB) — clustered; z11+ carries raw points` →
```markdown
# z0 carries every camera geometry-only (~60 KB gzipped); z11+ adds full properties
```

Line 108: `…clustered points at low zoom, raw points from z11.` →
```markdown
Then point the [PMTiles viewer](https://pmtiles.io) at the file URL — geometry-only points at low zoom, full-property points from z11.
```

Line 122: `- \`heatmap-weight\` — how much each cluster contributes; keyed off \`point_count\`` →
```markdown
- `heatmap-weight` — constant 1 per camera; density comes purely from point concentration
```

- [ ] **Step 4: Sweep for stragglers**

Run: `grep -rn "point_count\|cluster-distance\|keep-point-cluster\|cluster-maxzoom" README.md docs/ tiles/ --include="*.md" --include="*.json" --include="*.sh" --include="*.html" | grep -v local-dev/node_modules | grep -v superpowers/plans`
Expected: the only hits are in `tiles/local-dev/build-test-tiles.sh` (deleted in Task 6). `docs/map-styling.md`'s "Unclustered Point Layer" headings don't match this pattern and are correct as-is — they're app layer names.

- [ ] **Step 5: Commit**

```bash
git add README.md tiles/cameras/README.md docs/setup-guide.md
git commit -m "Document geometry-only heat range in README and setup guide

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Clean up superseded experiment files

**Files:**
- Delete (tracked): `tiles/local-dev/build-test-tiles.sh`, `tiles/local-dev/dark-theme-layers.json`
- Delete (untracked local artifacts, not in git): stale sweep `.pmtiles` in `tiles/local-dev/`

**Interfaces:**
- Consumes: nothing. `build.sh --local` (Task 2) replaces `build-test-tiles.sh` as the way to build preview tiles.

- [ ] **Step 1: Remove the tracked experiment files**

Run:
```bash
git rm tiles/local-dev/build-test-tiles.sh tiles/local-dev/dark-theme-layers.json
```
Rationale: `build-test-tiles.sh` was the cluster-parameter sweep harness (cluster distances 5–30, drop rates) — its question is settled and its configs are now misleading; local builds go through `build.sh --local` so flags can't drift from prod. `dark-theme-layers.json` is an unpromoted style experiment from June.

- [ ] **Step 2: Remove stale untracked binaries (local only, gitignored)**

Run:
```bash
cd tiles/local-dev
rm -f cameras-cap-500k.pmtiles-journal cameras-heatmap.pmtiles cameras-geomonly.pmtiles probe-result.json
ls *.pmtiles
```
Expected: `cameras-full.pmtiles cameras-local.pmtiles` remain (`full` is the raw-baseline reference used by `index.html`; `local` is the current-pipeline preview build).

- [ ] **Step 3: Verify the preview still works without the deleted files**

Run: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/tiles/cameras-local.json`
Expected: `200`

- [ ] **Step 4: Commit**

```bash
git commit -m "Remove cluster-sweep harness and unpromoted dark theme experiment

Local preview builds now go through build.sh --local, so tippecanoe
flags can't drift between prod and local experiments.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Ship and verify production

**Files:** none (deploy + observe)

- [ ] **Step 1: Merge to main and push**

```bash
git checkout main && git merge --no-ff geometry-only-heat -m "Merge geometry-only heat tiles

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```
(If the repo prefers PRs, open one with `gh pr create` instead and merge after review — confirm with the user first.)

- [ ] **Step 2: Trigger a build**

Run: `gh workflow run "Build Tiles" && sleep 30 && gh run list --workflow=build-tiles.yml --limit 1`
The `BUILD_CONFIG` bump guarantees the skip-check misses, so this run rebuilds even though source data is unchanged. Watch it: `gh run watch <run-id>`.
Expected: pass 1/2 + pass 2/2 + tile-join + `OK: … cameras at z0, geometry-only heat range, properties intact at z12`, upload succeeds. First run rebuilds tippecanoe (~4 min, new cache key).

- [ ] **Step 3: Probe the live endpoint**

Run:
```bash
curl -s "https://tiles.dontgetflocked.com/cameras/0/0/0.mvt" -o /tmp/z0.mvt && ls -la /tmp/z0.mvt
```
Expected: z0 tile substantially larger than the old 4.4 KB clustered tile (~60 KB compressed / ~1.1 MB raw depending on Worker encoding). Optionally decode locally to confirm no `point_count`.

- [ ] **Step 4: Visual confirmation in the app**

Open the production map, zoom z4→z12 over a metro. Expected: heat sharpens in place across every zoom transition. This is the acceptance criterion for the whole plan.

---

## Follow-ups outside this repo (flag to user, do not do here)

- **App repo style sync:** apps serve their own copy of the style (styles upload was removed from this pipeline in commit `21bd639`). The `heatmap-weight` change in `layers.json` v3 must be mirrored wherever the app defines its heatmap layer (per docs, `src/components/map/MapLibreContainer.tsx`). Until synced, the app's `["coalesce", ["get", "point_count"], 1]` expression still works against the new tiles (every feature falls through to 1) — so deploys are order-independent, but the expression is dead weight.
- **Stale-data investigation:** source GeoJSON hasn't changed since July 9; the Worker cron ingester may have stalled. Separate issue, worth checking.
