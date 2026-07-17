# Filter Companion Tileset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a per-country filter companion PMTiles archive (`cameras-<cc>-hourly-filter.pmtiles`) plus manifest (`cameras-<cc>-hourly-manifest.json`) in the same pipeline run that builds `cameras-<cc>-hourly.pmtiles`, for both `us` and `ca`.

**Architecture:** A Node preprocessing script (`tiles/cameras/enrich.mjs`) reads the country GeoJSON, assigns four small integer codes per feature (`b` brand, `o` operator, `z` zone, `m` mount) and writes an enriched GeoJSON + a manifest JSON mapping codes→labels. `build.sh` then runs the existing two-pass tippecanoe build a second time from the enriched GeoJSON — heat range keeps only the four codes, detail range keeps everything — and uploads all artifacts together. The existing `cameras-<cc>-hourly.pmtiles` build path is untouched.

**Tech Stack:** Node 22 (`node --test`), bash + tippecanoe + tile-join + aws CLI, GitHub Actions.

## Global Constraints

- **Do not change the main archive build in any way** — `build_tiles()`, its flags, and its output must produce byte-identical `cameras-<cc>-hourly.pmtiles`.
- Both new artifacts are produced from the same input `cameras-<cc>-hourly.geojson` in the same run — they ship as a matched set. Ids are build-scoped.
- Filter build keeps `--drop-rate=1 --no-feature-limit --no-tile-size-limit` — every point present at every zoom.
- Filter heat range (z0–10) carries ONLY `b`, `o`, `z`, `m`. Detail range (z11–14) carries all original properties plus the codes.
- Brand normalization is ported **verbatim** from DeFlock Maps `MapPanel.tsx normalizeBrand` (the app is the source of truth).
- Codes: `b`/`o` = 0 missing/unknown, ids 1..N by descending count. `z`: 0 missing, 1 traffic, 2 town, 3 parking, 4 other. `m`: 0 missing, 1 pole, 2 wall, 3 street_light, 4 other.
- Manifest schema: `{ version, generatedAt, total, brands: [{id,label,count}], operators: [...], zones: [...], mounts: [...] }`.
- Manifest uploaded gzipped (`--content-encoding gzip`, `--content-type application/json`), no cache-control metadata (same as the pmtiles uploads; TTL policy lives in the serving worker).
- No Cloudflare deploys — Actions→R2 uploads only.
- Bump `BUILD_CONFIG` so the next CI run rebuilds and populates the new artifacts even with unchanged source.

## File Structure

- Create `tiles/cameras/enrich.mjs` — pure enrichment functions + CLI (`node enrich.mjs <in.geojson> <enriched.geojson> <manifest.json>`)
- Create `tiles/cameras/enrich.test.mjs` — unit tests (node:test)
- Create `tiles/cameras/verify-filter.sh` — invariant checks for the filter archive
- Modify `tiles/cameras/build.sh` — add `build_filter_tiles()`, enrich step, verify, upload; bump `BUILD_CONFIG`
- Modify `.github/workflows/build-tiles.yml` — setup-node + run tile tests before build
- Modify `tiles/cameras/README.md` — document new artifacts

---

### Task 1: `enrich.mjs` — normalization, coding, manifest (TDD)

**Files:**
- Create: `tiles/cameras/enrich.mjs`
- Test: `tiles/cameras/enrich.test.mjs`

**Interfaces:**
- Produces: `normalizeBrand(raw: string) -> string|null`, `enrichCollection(fc) -> { collection, manifest }` where `manifest = { total, brands, operators, zones, mounts }` (no version/generatedAt — CLI adds those). CLI: `node enrich.mjs <input.geojson> <enriched-out.geojson> <manifest-out.json>`.

- [ ] **Step 1: Write failing tests** covering:
  - brand typo merging (`flock saftey`, `FLOCK`, `mortorola`, `lvt`, `autovu`, `elsag`, `pips`, `unv`)
  - unknown handling (`unknown`, `scm?`, `q108…`, `wikidata…`, single char, missing)
  - `includes('cyber')` behavior; unmatched brands kept as trimmed raw
  - zone codes (traffic/town/parking/other/missing; case-insensitive)
  - mount codes (pole/wall/street_light/other/missing; `Pole` → 1)
  - operator case-insensitive dedupe with most-common casing as label
  - ids assigned 1..N by descending count (tie → label sort); `0` never in manifest
  - every enriched feature has integer `b/o/z/m` plus original properties intact
  - manifest totals and per-entry counts
- [ ] **Step 2: Run `node --test tiles/cameras/enrich.test.mjs`** — expect FAIL (module missing)
- [ ] **Step 3: Implement `enrich.mjs`** (normalizeBrand ported verbatim; categoryCode; enrichCollection; CLI with sha256-prefix version + ISO generatedAt)
- [ ] **Step 4: Run tests** — expect PASS; also `node --test data/cameras/*.test.mjs` still green
- [ ] **Step 5: Commit** `feat: add camera filter-code enrichment script + manifest generator`

### Task 2: `verify-filter.sh`

**Files:**
- Create: `tiles/cameras/verify-filter.sh`

**Interfaces:**
- Consumes: a built filter pmtiles + expected feature count. Mirrors `verify.sh` structure (z0 full-count check, z12 four-corner candidate scan).
- Checks: (1) z0 count == expected, no `point_count`; (2) every z0 feature's property keys are exactly `["b","m","o","z"]` with integer values; (3) a genuine z12 tile has features with both `osmId` and `b`.

- [ ] **Step 1: Write the script** (copy verify.sh's z12 candidate logic; swap the assertions)
- [ ] **Step 2: Test against a small local build** (enrich a 3-feature fixture, run tippecanoe passes manually, verify PASS; also verify it FAILS against the main-archive-style geometry-only build)
- [ ] **Step 3: Commit** `feat: add filter-archive invariant verification`

### Task 3: Wire into `build.sh` (local + country modes) and workflow

**Files:**
- Modify: `tiles/cameras/build.sh`
- Modify: `.github/workflows/build-tiles.yml`
- Modify: `tiles/cameras/README.md`

**Changes to `build.sh`:**
- Add `FILTER_HEAT_TMP`/`FILTER_DETAIL_TMP` mktemp names to the trap
- Add `build_filter_tiles <enriched-geojson> <out.pmtiles>`: heat pass `-Z0 -z10 --include=b --include=o --include=z --include=m`, detail pass `-Z11 -z14` (all props), `tile-join`
- `--local` mode: after main build+verify, run enrich → filter build → `verify-filter.sh`; outputs `<out>-filter.pmtiles` + `<out>-manifest.json` next to the main output
- `--country` mode: after main verify + size guard, run enrich → filter build → verify-filter + size guard (same MIN_BYTES); upload `cameras-<cc>-hourly-filter.pmtiles` and gzipped `cameras-<cc>-hourly-manifest.json` (`--content-encoding gzip --content-type application/json`) to tiles bucket + mirror, BEFORE the hash file write (hash write stays last so a failed upload retries next run)
- `BUILD_CONFIG` → `v5-filter-companion`
- **Do not touch `build_tiles()` or the main-archive tippecanoe flags**

**Changes to workflow:** add `actions/setup-node@v4` (node 22) + `node --test tiles/cameras/*.test.mjs` step before the build step.

- [ ] **Step 1: Edit build.sh** as above
- [ ] **Step 2: `bash -n build.sh`** (syntax) and run `--local` mode against a small fixture — main + filter archives build, both verifies pass
- [ ] **Step 3: Edit workflow + README**
- [ ] **Step 4: Commit** `feat: build + upload filter companion tileset and manifest per country`

### Task 4: End-to-end verification on real data (acceptance criteria)

- [ ] **Step 1:** Run `build.sh --local tiles/cameras.geojson` (US snapshot, ~103K features) in scratchpad; also run against `tiles/local-dev/cameras-ca.geojson` (CA)
- [ ] **Step 2:** `tippecanoe-decode` a z4 tile from the filter archive — features carry only `b/o/z/m` integers
- [ ] **Step 3:** Decode a z12 tile — original attributes plus codes
- [ ] **Step 4:** Per-tile feature counts equal between main and filter archive at same z/x/y (z0 and a dense z4 tile)
- [ ] **Step 5:** Size comparison table at z2/z4/z6/z8 for densest tiles (incl. 4/4/6, 4/3/6) — flag any filter tile > 2× its geometry-only counterpart
- [ ] **Step 6:** Manifest spot-checks: Flock typos merged, no obvious duplicate brands; totals match feature count
- [ ] **Step 7:** Confirm main archive unchanged: build main archive from same geojson with pre-change and post-change build.sh → identical bytes (or diff of `build_tiles()` region empty)
- [ ] **Step 8:** Full test suite green; commit any fixes

## Self-Review

- Spec coverage: preprocessing (Task 1), manifest (Task 1), filter archive build (Task 3), verify & measure (Tasks 2/4), upload for both countries (Task 3 — `--country` loop already covers us+ca), matched-set guarantee (same run, hash written last, BUILD_CONFIG bump).
- Naming: spec's `cameras-filter.pmtiles`/`cameras-manifest.json` maps to this repo's per-country convention `cameras-<cc>-hourly-filter.pmtiles`/`cameras-<cc>-hourly-manifest.json`, matching `cameras-<cc>-hourly.pmtiles`.
