# DeFlock Overpass via Adaptive Tiling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the hourly Actions ingestion actually pull from `overpass.deflock.org` by porting the production worker's proven adaptive-tiling fetch (plus its Overpass client settings and richer direction parsing) from TypeScript to this repo's plain-JS pipeline — then test heavily.

**Architecture:** US = seed bbox grid → per-tile count probes → quadrant-split any tile >5K cameras → parallel small fetches (each seconds, under deflock.org's ~60s proxy ceiling) → dedup by `type/id` → subtract CA/MX features fetched via authoritative area queries. CA's area result doubles as the published CA dataset. Output contract (file names, meta.json, floors, upload.sh, build.sh) unchanged.

**Tech Stack:** Node 22 ESM (`.mjs`), `node:test`, no new dependencies.

**Port source (READ-ONLY reference — the production worker):**
`/Users/jackcauthen/iCloud Drive (Archive)/Documents/Vibe Coding/FLOCK/NAVIGATION/FLOCKHOPPER/CLOUDFLARE REPO/FlockHopper/worker/`
- `src/fetchers/cameras.ts` — tiling, direction parsing, transform, subtraction, orchestration
- `src/lib/overpass.ts` — endpoints, UA, timeout, allowEmpty
- `src/lib/retry.ts` — retryWithBackoff
- `src/lib/guards.ts` — tileIntegrityFailed, belowMinimum
- `tests/fetchers/cameras.test.ts`, `tests/lib/retry.test.ts`, `tests/lib/guards.test.ts`, `tests/lib/overpass.test.ts` — test suites to port

## Global Constraints

- NEVER modify anything under the port-source path above (read-only reference).
- No Cloudflare deploys, no wrangler writes. GitHub Actions uploading to R2 is the only allowed prod surface.
- Output contract frozen: `cameras-us-hourly.geojson` / `cameras-ca-hourly.geojson` + `meta.json` (same keys/fields) into `--out` dir; upload.sh and build.sh unchanged.
- Constants copied verbatim from prod: SPLIT_THRESHOLD 5000, MIN_TILE_SPAN 0.05, TILE_CONCURRENCY 5, TILE_RETRIES 3, TILE_RETRY_DELAY_MS 1000, TILE_FETCH_TOLERANCE 0.10, RAW_MIN_TOTAL 50000, TIMEOUT_MS 55000.
- Floors unchanged: US 50,000 (post-subtraction), CA 300 (stricter than prod's 250 — keep ours). MX is subtract-only, may be 0.
- User-Agent: `FlockHopper-Data/1.0 (+https://dontgetflocked.com; alerts@dontgetflocked.com)`; `Accept: application/json` header on all Overpass requests.
- Endpoint order: deflock.org, overpass-api.de, kumi.systems, maps.mail.ru (keep 4th as extra fallback).
- Area queries gain `["admin_level"="2"]`.
- All JS stays dependency-free ESM; tests use `node:test` + `node:assert/strict` like the existing suite.

---

### Task 1: Overpass client upgrade + full direction parsing (lib.mjs)

**Files:**
- Modify: `data/cameras/lib.mjs`
- Test: `data/cameras/lib.test.mjs`

**Interfaces (Produces — Task 2 depends on these exact signatures):**
- `queryOverpass(query, fetchImpl = fetch, { allowEmpty = false } = {})` → parsed JSON body; throws on empty `elements` unless allowEmpty; falls through endpoints; keeps the non-JSON body-head diagnostics.
- `retryWithBackoff(fn, maxRetries, baseDelayMs)` — port of prod `retry.ts` (exponential: base·2^(attempt−1)).
- `tileIntegrityFailed(produced, probed, tolerance)`, `belowMinimum(count, min)` — port of prod `guards.ts`.
- `parseDirections(value) → number[]`, `parseDirection(value) → number|null` (first result; back-compat) — port of prod `cameras.ts` lines 163–242 (CARDINALS, SPELLED_CARDINALS, BOUND_DIRECTIONS, normalizeDegrees, resolveSimple, rangeMidpoint, parseSingleToken). Note `Number()` not `parseFloat` in resolveSimple.
- `transformOverpassToGeoJSON(data)` unchanged signature, but internals updated to match prod: `directions` array property when >1, `direction` = first, `directionCardinal` = first token when it's a 16-point cardinal. Export new `addElementsToFeatures(elements, featureMap)` (keyed `${type}/${id}`) and reimplement transform on top of it.
- `OVERPASS_USER_AGENT` exported; `TIMEOUT_MS` = 55_000.

- [ ] **Step 1: Port the direction-parsing tests first (TDD).** Adapt every direction case from prod `tests/fetchers/cameras.test.ts` into `lib.test.mjs` `describe('parseDirections')` / extend `describe('parseDirection')`: spelled-out (`NORTH`→0, `SOUTHWEST`→225), bounds (`NB`→0, `WB`→270), numeric normalization (`450`→90, `-90`→270), ranges (`338-23`→0.5, `WSW-ESE` midpoint, full-circle `0-360`→180), multi-value `"90;270"`→[90,270] and comma `"N,S"`→[0,180], garbage→[], empty/undefined→[]. Also `queryOverpass` allowEmpty behavior and `retryWithBackoff`/guards tests ported from prod `tests/lib/*.test.ts`.
- [ ] **Step 2: Run tests — new ones FAIL** (`node --test data/cameras/lib.test.mjs`).
- [ ] **Step 3: Implement the ports in lib.mjs** (transcribe from the source files listed above, TS types stripped). Keep existing exports working; existing tests that encode the OLD narrow behavior (e.g. "takes first of semicolon list" still holds; but `parseDirection('90;270')` stays 90 — verify each old test against prod semantics and update only where prod semantics legitimately differ, noting each in the report).
- [ ] **Step 4: All tests green; commit** `git commit -m "Port prod Overpass client settings and full direction parsing"`

### Task 2: Adaptive tiling fetcher + fetch.mjs rewrite

**Files:**
- Create: `data/cameras/tiled-fetch.mjs`
- Modify: `data/cameras/fetch.mjs`
- Test: `data/cameras/tiled-fetch.test.mjs`

**Interfaces:**
- Consumes Task 1's exports from `./lib.mjs`.
- `tiled-fetch.mjs` exports (ported from prod `cameras.ts`, TS→JS): `buildSeedTiles()`, `tileSelector(t)`, `countTile(t, fetchImpl)`, `planLeafTiles(seed, fetchImpl)`, `fetchTileInto(t, featureMap, fetchImpl)`, `fetchCountryArea(iso, minCount, fetchImpl)` (query includes `["admin_level"="2"]`), `subtractForeign(merged, foreignKeys)`, and the orchestrator `fetchAllCameras(fetchImpl = fetch)` → `{ us: FeatureCollection, ca: FeatureCollection }` implementing prod's flow: plan tiles → parallel fetch (concurrency 5, retries) → RAW_MIN_TOTAL check → CA (`areaMinCount` 300) + MX (`areaMinCount` 0) area fetches → subtract both from US → sorted FeatureCollections. Every network-touching function takes `fetchImpl` for testability.
- `fetch.mjs`: same CLI (`--out`), calls `fetchAllCameras()`, writes `cameras-us-hourly.geojson` (floor 50,000) and `cameras-ca-hourly.geojson` (floor 300) + `meta.json` exactly as today. The per-country COUNTRIES loop and its separate Overpass calls are removed.

- [ ] **Step 1: Port the tiling tests first.** Adapt prod `tests/fetchers/cameras.test.ts` tiling cases to `node:test` with a scripted mock `fetchImpl` (count-probe responses driving splits; tile fetches; integrity-failure case; subtraction case; seed-grid coverage assertions).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement `tiled-fetch.mjs`** by transcribing prod `cameras.ts` (strip types; import client/guards/retry/transform helpers from `./lib.mjs`).
- [ ] **Step 4: Rewrite `fetch.mjs`** per interface above (keep console output informative: planned tile count, merged raw total, subtraction summary, per-dataset counts).
- [ ] **Step 5: All tests green** (`node --test data/cameras/*.test.mjs`); `node --check` both files; commit `git commit -m "Fetch US via adaptive tiling + CA/MX subtraction, CA via area query"`

### Task 3: Lots of testing (local E2E, then CI, then prod verification)

**Files:** none (operational; controller-run)

- [ ] **Step 1: Full unit suite** — `node --test data/cameras/*.test.mjs` green, output pristine.
- [ ] **Step 2: Local E2E against real deflock.org** (works from this machine): `node data/cameras/fetch.mjs --out <scratchpad>/e2e-out`. Expect: planned-tiles log, US ≈ 116–118K, CA ≈ 516, runtime a few minutes.
- [ ] **Step 3: Data-quality comparison vs prod daily.** Download `https://data.dontgetflocked.com/cameras.geojson.gz` (daily US) + `cameras-ca.geojson.gz`; compare against the E2E output: feature-count deltas (expect small — datasets are a few hours apart), per-property presence stats (operator/brand/direction/directions/directionCardinal), and count of features whose `direction` differs — every difference must be explained by the richer parsing (spot-check ≥5 concrete examples: NB/range/negative values). US id-set overlap should be ≳99%.
- [ ] **Step 4: Endpoint verification.** Confirm from the E2E logs that no fallback fired (all requests served by deflock.org); rerun one `countTile`-sized query with the new UA via curl as a sanity check.
- [ ] **Step 5: Commit any fixes, push, dispatch `Fetch Camera Data`.** Watch logs: did deflock.org accept GitHub runner IPs with the new UA? If it rejects: capture the logged body-head, report to user with the exact WAF allowance needed (this is the one open risk; fallback keeps the pipeline green either way).
- [ ] **Step 6: Prod pipeline verification.** Run conclusion green; R2 objects updated (`wrangler r2 object get --remote` byte sizes sane); counts in workflow log match E2E ballpark; dispatch `Build Tiles`, confirm build + mirror + `tiles.dontgetflocked.com/cameras-us-hourly.json` 200 and a Toronto z10 tile 200; second Build Tiles run skips.
- [ ] **Step 7: Watch one scheduled (cron) fetch run** complete green without manual dispatch.
