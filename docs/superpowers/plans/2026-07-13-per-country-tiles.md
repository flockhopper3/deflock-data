# Per-Country Camera Tiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one PMTiles archive per country (`cameras-us.pmtiles`, `cameras-ca.pmtiles`) hourly from the per-country GeoJSONs already in R2, replacing the merged `cameras.pmtiles` build.

**Architecture:** `tiles/cameras/build.sh` gains a country table and a main loop that re-invokes itself (`build.sh --country <cc>`) per country — a child process per country gives real `set -e` semantics inside each build while the parent aggregates failures (one country failing must not block the others). Each country runs the existing two-pass geometry-only/full-property build + `tile-join` + `verify.sh` gate, with per-country feature floors, output-size floors, and hash skip-checks. The Worker, CI workflow, and `verify.sh` are untouched. The old merged `cameras.pmtiles` stops being rebuilt but keeps serving (frozen) until the app migrates.

**Tech Stack:** bash, felt/tippecanoe 2.79.0 (`tippecanoe`, `tile-join`, `tippecanoe-decode`), jq, GitHub Actions, Cloudflare R2.

**Spec:** `docs/superpowers/specs/2026-07-13-per-country-tiles-design.md`

## Global Constraints

- Country table: `us` (source `cameras-us.geojson.gz`, output `cameras-us.pmtiles`, min **50000** features, min output **10 MB**) and `ca` (source `cameras-ca.geojson.gz`, output `cameras-ca.pmtiles`, min **300** features, min output **100 KB**, subject to Task 2's measurement).
- Zoom split unchanged: heat z0–z10 geometry-only (`--exclude-all`), detail z11–z14 full properties, `--drop-rate=1` everywhere, layer name `cameras` in every archive.
- `BUILD_CONFIG` stays `v3-geomonly-heat` (tippecanoe flags are unchanged; the per-country hash keys `cameras-<cc>.geojson.sha256` are new, so no stale-hash carryover exists).
- Per-country failure isolation MUST hold: with both countries failing at fetch, the script must attempt BOTH and exit non-zero naming both.
- Bash `set -e` hazard: a function called under `if !` runs with errexit suppressed — per-country builds therefore run as child processes (`bash "${BASH_SOURCE[0]}" --country <cc>`), never as functions tested with `if !`.
- `verify.sh`, `.github/workflows/build-tiles.yml`, and the tile Worker are NOT modified.
- `--local` mode keeps its exact current CLI: `build.sh --local <geojson> [output.pmtiles]`.
- Do not delete the R2 objects `cameras.pmtiles` / `cameras.geojson.sha256` — they serve the app until it migrates (out of scope).

**Branch:** create `git checkout -b per-country-tiles` from `main` before Task 1.

---

### Task 1: Per-country `build.sh`

**Files:**
- Modify: `tiles/cameras/build.sh` (full rewrite below)

**Interfaces:**
- Consumes: `tiles/cameras/verify.sh <pmtiles> <expected_count>` (existing, unchanged).
- Produces: `build.sh` (no args, R2 mode: all countries), `build.sh --country <cc>` (R2 mode: one country), `build.sh --local <geojson> [out]` (unchanged CLI). Task 5 runs the no-args form in CI.

- [ ] **Step 1: Replace `tiles/cameras/build.sh` with**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Builds per-country camera PMTiles from GeoJSON in R2 and uploads them.
#
# Modes:
#   build.sh                          — R2: build every country in COUNTRIES,
#                                       isolating failures per country
#   build.sh --country <cc>           — R2: build one country
#   build.sh --local <geojson> [out]  — local preview build, no R2
#
# R2 modes require env:
#   R2_DATA_BUCKET   — bucket holding cameras-<cc>.geojson.gz (read)
#   R2_TILES_BUCKET  — bucket the built PMTiles are uploaded to (write)
#   R2_ENDPOINT      — R2 S3-compatible endpoint URL
#
# Per-country builds run as child processes, NOT functions: bash suppresses
# `set -e` inside any function tested with `if !`, so a mid-build failure in
# a function would fall through to the upload. A child process keeps errexit
# live and the parent only inspects its exit code.
#
# Zoom strategy (heatmap → dots), identical for every archive:
#   z0–z10  geometry-only raw points — heat anchors never move across zooms
#   z11–z14 raw points with all properties — dots, popups, direction cones

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COUNTRIES=(us ca)

# Bump BUILD_CONFIG whenever tippecanoe flags change so the skip check
# doesn't short-circuit a rebuild with unchanged source data.
BUILD_CONFIG="v3-geomonly-heat"

# Floors mirror data/cameras/fetch.mjs; sizes protect prod from truncated
# uploads. CA archive is small (~1K cameras), hence the much lower floor.
country_min_features() {
  case "$1" in
    us) echo 50000 ;;
    ca) echo 300 ;;
    *) return 1 ;;
  esac
}

country_min_bytes() {
  case "$1" in
    us) echo $((10 * 1024 * 1024)) ;;
    ca) echo $((100 * 1024)) ;;
    *) return 1 ;;
  esac
}

HEAT_TMP="$(mktemp -u).heat.pmtiles"
DETAIL_TMP="$(mktemp -u).detail.pmtiles"
trap 'rm -f "${HEAT_TMP}" "${DETAIL_TMP}"' EXIT

# build_tiles <geojson> <output.pmtiles> — two-pass build + merge
build_tiles() {
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
    "$1"

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
    "$1"

  echo "==> Merging zoom ranges with tile-join"
  tile-join -o "$2" --force --no-tile-size-limit \
    "${HEAT_TMP}" "${DETAIL_TMP}"
}

# ── Local mode ──────────────────────────────────────────────────────────
if [ "${1:-}" = "--local" ]; then
  GEOJSON_FILE="${2:?usage: build.sh --local <cameras.geojson> [output.pmtiles]}"
  OUTPUT_FILE="${3:-cameras-local.pmtiles}"

  FEATURE_COUNT=$(jq '.features | length' "${GEOJSON_FILE}")
  echo "    ${FEATURE_COUNT} features found"

  build_tiles "${GEOJSON_FILE}" "${OUTPUT_FILE}"

  echo "==> Verifying tile invariants"
  bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}" "${FEATURE_COUNT}"

  echo "==> Done (local). Built ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
  exit 0
fi

# ── Single-country R2 mode ──────────────────────────────────────────────
if [ "${1:-}" = "--country" ]; then
  CC="${2:?usage: build.sh --country <cc>}"
  if ! MIN_FEATURES="$(country_min_features "${CC}")"; then
    echo "ERROR: unknown country '${CC}' (known: ${COUNTRIES[*]})"
    exit 1
  fi
  MIN_BYTES="$(country_min_bytes "${CC}")"

  OUTPUT_FILE="cameras-${CC}.pmtiles"
  GEOJSON_FILE="cameras-${CC}.geojson"
  HASH_FILE="cameras-${CC}.geojson.sha256"

  echo "==> [${CC}] Fetching GeoJSON from R2"
  aws s3 cp "s3://${R2_DATA_BUCKET}/cameras-${CC}.geojson.gz" "${GEOJSON_FILE}.download" \
    --endpoint-url "${R2_ENDPOINT}"

  # The object may or may not actually be gzip-compressed — check magic bytes.
  if [ "$(head -c 2 "${GEOJSON_FILE}.download" | xxd -p)" = "1f8b" ]; then
    echo "==> [${CC}] Decompressing gzip"
    gunzip -c "${GEOJSON_FILE}.download" > "${GEOJSON_FILE}"
    rm "${GEOJSON_FILE}.download"
  else
    mv "${GEOJSON_FILE}.download" "${GEOJSON_FILE}"
  fi

  echo "==> [${CC}] Checking whether source data changed since last build"
  NEW_HASH="$(shasum -a 256 "${GEOJSON_FILE}" | cut -d' ' -f1)-${BUILD_CONFIG}"
  OLD_HASH=$(aws s3 cp "s3://${R2_TILES_BUCKET}/${HASH_FILE}" - \
    --endpoint-url "${R2_ENDPOINT}" 2>/dev/null || echo "none")
  if [ "${NEW_HASH}" = "${OLD_HASH}" ]; then
    echo "==> [${CC}] Source unchanged (${NEW_HASH:0:12}…) — skipping build"
    exit 0
  fi

  echo "==> [${CC}] Validating GeoJSON"
  FEATURE_COUNT=$(jq '.features | length' "${GEOJSON_FILE}")
  if [ "${FEATURE_COUNT}" -lt "${MIN_FEATURES}" ]; then
    echo "ERROR: [${CC}] only ${FEATURE_COUNT} features — expected ${MIN_FEATURES}+. Aborting to protect prod."
    exit 1
  fi
  echo "    ${FEATURE_COUNT} features found"

  build_tiles "${GEOJSON_FILE}" "${OUTPUT_FILE}"

  echo "==> [${CC}] Verifying tile invariants"
  bash "${SCRIPT_DIR}/verify.sh" "${OUTPUT_FILE}" "${FEATURE_COUNT}"

  FILE_SIZE=$(stat -f%z "${OUTPUT_FILE}" 2>/dev/null || stat -c%s "${OUTPUT_FILE}")
  if [ "${FILE_SIZE}" -lt "${MIN_BYTES}" ]; then
    echo "ERROR: [${CC}] output is only $(du -h "${OUTPUT_FILE}" | cut -f1) — suspiciously small. Aborting."
    exit 1
  fi

  echo "==> [${CC}] Uploading to Cloudflare R2"
  aws s3 cp "${OUTPUT_FILE}" "s3://${R2_TILES_BUCKET}/${OUTPUT_FILE}" \
    --endpoint-url "${R2_ENDPOINT}"
  echo "${NEW_HASH}" | aws s3 cp - "s3://${R2_TILES_BUCKET}/${HASH_FILE}" \
    --endpoint-url "${R2_ENDPOINT}"

  rm -f "${GEOJSON_FILE}"
  echo "==> [${CC}] Done. Uploaded ${OUTPUT_FILE} ($(du -h "${OUTPUT_FILE}" | cut -f1))"
  exit 0
fi

# ── Main R2 mode: every country, isolating failures ────────────────────
FAILED=()
for CC in "${COUNTRIES[@]}"; do
  if ! bash "${BASH_SOURCE[0]}" --country "${CC}"; then
    echo "!! [${CC}] build failed — continuing with remaining countries"
    FAILED+=("${CC}")
  fi
done

if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "ERROR: failed countries: ${FAILED[*]}"
  exit 1
fi
echo "==> All countries built or skipped successfully"
```

- [ ] **Step 2: Lint**

Run: `shellcheck tiles/cameras/build.sh && bash -n tiles/cameras/build.sh && echo LINT-OK`
Expected: `LINT-OK` (no shellcheck findings).

- [ ] **Step 3: `--local` regression test**

Run (from repo root; the fixture is the June US GeoJSON, ~103K features, build takes ~1–2 min):
```bash
cd tiles/local-dev
bash ../cameras/build.sh --local ../cameras.geojson cameras-local.pmtiles
```
Expected: two tippecanoe passes, tile-join, then `OK: 102998 cameras at z0, geometry-only heat range, properties intact at z12`, then `==> Done (local). Built cameras-local.pmtiles (…)`, exit 0.

- [ ] **Step 4: Failure-isolation test (no credentials needed)**

Run:
```bash
cd /tmp && AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_MAX_ATTEMPTS=1 \
  R2_DATA_BUCKET=bogus R2_TILES_BUCKET=bogus R2_ENDPOINT=http://127.0.0.1:1 \
  bash /Users/jackcauthen/Documents/Developer/FLOCK/Data/tiles/cameras/build.sh; echo "exit=$?"
```
Expected: BOTH `==> [us] Fetching GeoJSON from R2` and `==> [ca] Fetching GeoJSON from R2` appear (proving the loop continued past the us failure), each followed by an aws connection error and `!! [..] build failed — continuing`, ending with `ERROR: failed countries: us ca` and `exit=1`.

- [ ] **Step 5: Unknown-country guard test**

Run: `bash tiles/cameras/build.sh --country zz; echo "exit=$?"`
Expected: `ERROR: unknown country 'zz' (known: us ca)` and `exit=1`.

- [ ] **Step 6: Commit**

```bash
git add tiles/cameras/build.sh
git commit -m "Build one PMTiles archive per country with failure isolation

Loops COUNTRIES (us, ca) and re-invokes itself per country as a child
process — bash suppresses set -e inside functions tested with 'if !',
so a function-based loop would upload half-built archives. Per-country
feature floors, size floors, and hash skip-checks; a failed country no
longer blocks the others. The merged cameras.pmtiles is no longer built.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Canada end-to-end local build + size-floor calibration

**Files:**
- Possibly modify: `tiles/cameras/build.sh` (the `ca` line in `country_min_bytes`, only if the measurement below demands it)

**Interfaces:**
- Consumes: `build.sh --local` from Task 1; `wrangler` (authenticated) in `/Users/jackcauthen/Documents/Developer/FLOCK/FLOCKHOPPER DATA RESEARCH/infra/protomaps-r2/worker/` for the R2 download.
- Produces: measured CA archive size; a `country_min_bytes` `ca` value guaranteed ≤ half the measured size.

- [ ] **Step 1: Download the live Canada GeoJSON from R2**

```bash
cd "/Users/jackcauthen/Documents/Developer/FLOCK/FLOCKHOPPER DATA RESEARCH/infra/protomaps-r2/worker"
npx wrangler r2 object get flockhopper-data/cameras-ca.geojson.gz --remote --pipe \
  > /Users/jackcauthen/Documents/Developer/FLOCK/Data/tiles/local-dev/cameras-ca.geojson
cd /Users/jackcauthen/Documents/Developer/FLOCK/Data/tiles/local-dev
head -c 2 cameras-ca.geojson | xxd -p   # if "1f8b", gunzip: mv cameras-ca.geojson x.gz && gunzip -c x.gz > cameras-ca.geojson && rm x.gz
jq '.features | length' cameras-ca.geojson
```
Expected: a feature count ≥ 300 (roughly 1–2K).

- [ ] **Step 2: Build and verify the CA archive locally**

```bash
bash ../cameras/build.sh --local cameras-ca.geojson cameras-ca-local.pmtiles
ls -la cameras-ca-local.pmtiles
```
Expected: `OK: <count> cameras at z0, geometry-only heat range, properties intact at z12` with `<count>` equal to Step 1's jq count, exit 0. This also exercises `verify.sh`'s four-candidate z12 check on sparse data. Record the archive size in bytes.

- [ ] **Step 3: Calibrate the CA size floor**

If the measured size is **≥ 200 KB**, the `ca) echo $((100 * 1024))` floor stands — no change; skip to Step 4. If it is **< 200 KB**, edit `country_min_bytes` in `tiles/cameras/build.sh` so the `ca` floor is roughly half the measured size rounded down to a clean number (e.g. measured 120 KB → `ca) echo $((60 * 1024))`), re-run `shellcheck tiles/cameras/build.sh`, and commit:

```bash
git add tiles/cameras/build.sh
git commit -m "Calibrate CA archive size floor to measured build output

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Report**

No commit if the floor stood. Record the measured CA feature count and archive size in your report — Task 5 compares the CI-built archive against them.

---

### Task 3: Style points at the per-country TileJSON

**Files:**
- Modify: `tiles/cameras/layers.json:2` (version) and `:6` (source url)

**Interfaces:**
- Produces: `layers.json` version 4 — the reference style apps copy. Its single `source.url` demonstrates the per-country URL scheme using the US archive.

- [ ] **Step 1: Update `layers.json`**

Change `"version": 3,` → `"version": 4,` and change:

```json
    "url": "https://tiles.dontgetflocked.com/cameras.json"
```

to:

```json
    "url": "https://tiles.dontgetflocked.com/cameras-us.json"
```

(The style is per-source; the app instantiates one source per country — `cameras-us.json`, `cameras-ca.json` — reusing these same layer definitions. That guidance lands in the README in Task 4; JSON can't carry comments.)

- [ ] **Step 2: Validate**

Run: `jq empty tiles/cameras/layers.json && jq -r '.version, .source.url' tiles/cameras/layers.json`
Expected: no parse error; prints `4` and `https://tiles.dontgetflocked.com/cameras-us.json`.

- [ ] **Step 3: Commit**

```bash
git add tiles/cameras/layers.json
git commit -m "Style v4: reference the per-country US TileJSON

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Docs describe per-country outputs

**Files:**
- Modify: `README.md`, `tiles/cameras/README.md`, `docs/setup-guide.md`

**Interfaces:** none (prose only). The authoritative endpoint table to place in the docs:

| Country | TileJSON | Tiles |
|---------|----------|-------|
| US | `https://tiles.dontgetflocked.com/cameras-us.json` | `https://tiles.dontgetflocked.com/cameras-us/{z}/{x}/{y}.mvt` |
| Canada | `https://tiles.dontgetflocked.com/cameras-ca.json` | `https://tiles.dontgetflocked.com/cameras-ca/{z}/{x}/{y}.mvt` |

- [ ] **Step 1: Sweep for stale references**

Run: `grep -rn "cameras\.pmtiles\|cameras\.json\|cameras\.geojson\|cameras/{z}" README.md tiles/cameras/README.md docs/setup-guide.md`
This is the authoritative work list — update every hit per Steps 2–4. (Hits inside `docs/superpowers/` are historical records; leave them.)

- [ ] **Step 2: `README.md`**

Update the pipeline description so it reads (adapting to the surrounding bullets' existing voice): the hourly job builds **one archive per country** from `cameras-us.geojson.gz` / `cameras-ca.geojson.gz` (Worker cron ingests both hourly), producing `cameras-us.pmtiles` and `cameras-ca.pmtiles`, each validated by `verify.sh` before upload; per-country floors are 50,000 features (US) / 300 (CA). Insert the endpoint table above where the old single `cameras.json` URL appeared, and add one transition note verbatim:

```markdown
> **Transition note:** the legacy merged `cameras.pmtiles` is frozen (no longer rebuilt) and keeps serving until the app switches to the per-country sources above; delete it from the tiles bucket after the app migrates.
```

Also state that the app creates one MapLibre source per country reusing the same `layers.json` layer definitions.

- [ ] **Step 3: `tiles/cameras/README.md`**

Update the build description: `build.sh` loops the country table (`us`, `ca`) and re-invokes itself per country (`build.sh --country <cc>`) for failure isolation; per-country hash skip-checks (`cameras-<cc>.geojson.sha256`); `--local` mode unchanged. Replace any `cameras.pmtiles` output reference with the two per-country outputs.

- [ ] **Step 4: `docs/setup-guide.md`**

Update the healthy-run log example to show the per-country prefixes (`==> [us] Fetching GeoJSON from R2` … `==> [ca] Source unchanged … — skipping build` … `==> All countries built or skipped successfully`) and change probe examples from `/cameras/{z}/{x}/{y}.mvt` to `/cameras-us/...` plus one `/cameras-ca/...` example (Toronto: `https://tiles.dontgetflocked.com/cameras-ca/10/286/373.mvt`).

- [ ] **Step 5: Re-run the Step 1 grep**

Expected: zero hits outside `docs/superpowers/` (the plan/spec records).

- [ ] **Step 6: Commit**

```bash
git add README.md tiles/cameras/README.md docs/setup-guide.md
git commit -m "Document per-country tile outputs and endpoints

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Ship and verify production

**Files:** none (deploy + observe)

- [ ] **Step 1: Merge and push**

```bash
git checkout main && git merge --no-ff per-country-tiles -m "Merge per-country camera tiles

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 2: First build — both countries**

Run: `gh workflow run "Build Tiles"`, then watch the run (`gh run watch <id>` or a Monitor loop). Expected in the log: `[us]` full build ending `Uploaded cameras-us.pmtiles (~36M)` and `[ca]` full build ending `Uploaded cameras-ca.pmtiles` (size consistent with Task 2's local measurement), both `OK:` verify lines, ending `==> All countries built or skipped successfully`.

- [ ] **Step 3: Second build — per-country skip behavior**

Run `gh workflow run "Build Tiles"` again after the first completes. Expected: both countries print `Source unchanged (…) — skipping build` and the run finishes in well under a minute of build-step time.

- [ ] **Step 4: Prod probes**

```bash
curl -s -o /dev/null -w "us tilejson: %{http_code}\n" https://tiles.dontgetflocked.com/cameras-us.json
curl -s -o /dev/null -w "ca tilejson: %{http_code}\n" https://tiles.dontgetflocked.com/cameras-ca.json
curl -s -o /dev/null -w "toronto z10: %{http_code} %{size_download}B\n" "https://tiles.dontgetflocked.com/cameras-ca/10/286/373.mvt"
curl -s -o /dev/null -w "atlanta z10 (us): %{http_code} %{size_download}B\n" "https://tiles.dontgetflocked.com/cameras-us/10/271/409.mvt"
curl -s -o /dev/null -w "frozen merged still serves: %{http_code}\n" "https://tiles.dontgetflocked.com/cameras/6/16/25.mvt"
```
Expected: all `200`; the Toronto tile has a non-trivial byte size (Canadian cameras exist there — this was the original point of the project); the frozen merged endpoint still serves for the un-migrated app.

- [ ] **Step 5: Record follow-ups**

Report to the user: app repo must add the two per-country sources (URLs in Task 4's table) reusing the existing layer definitions with `heatmap-weight: 1`; after the app ships, delete `cameras.pmtiles` and `cameras.geojson.sha256` from the `flockhopper-tiles` bucket.

---

## Follow-ups outside this repo (report, do not do)

- **App repo:** two MapLibre sources (`cameras-us.json`, `cameras-ca.json`), duplicate the camera layers per source (or parameterize), sync `heatmap-weight: 1`.
- **After app migration:** delete frozen `cameras.pmtiles` + `cameras.geojson.sha256` from `flockhopper-tiles`.
- **Later (user's call):** Actions ingestion cutover (enable `Fetch Camera Data`, retire Worker cron) — orthogonal to this plan since both write the same per-country keys.
