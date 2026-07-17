# Hourly Camera Data (GeoJSON + Tiles) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish hourly-updated per-country camera GeoJSONs (`cameras-us-hourly.geojson.gz`, `cameras-ca-hourly.geojson.gz`) and PMTiles (`cameras-us-hourly.pmtiles`, `cameras-ca-hourly.pmtiles`) for a new app, without touching the daily files FlockHopper uses.

**Architecture:** Re-enable the two existing (currently disabled) GitHub Actions workflows in this repo with `-hourly` output names so nothing collides with the daily Cloudflare Worker cron's keys. The existing `flockhopper-data` worker (data.dontgetflocked.com) serves any `.geojson(.gz)` key generically — it only needs a shorter cache TTL for `-hourly` keys. The `flockhopper-tiles` worker needs zero changes.

**Tech Stack:** Node 22 (`node --test`), bash + tippecanoe + aws CLI, GitHub Actions, Cloudflare Workers (vitest, wrangler).

**Spec:** `docs/superpowers/specs/2026-07-16-hourly-camera-data-design.md`

## Global Constraints

- The daily keys must NEVER be written by this pipeline: `cameras.geojson.gz`, `cameras-ca.geojson.gz` (bucket `flockhopper-data`), `cameras-us.pmtiles`, `cameras-ca.pmtiles`, `cameras.pmtiles` (bucket `flockhopper-tiles`).
- New object names, exactly: `cameras-us-hourly.geojson.gz`, `cameras-ca-hourly.geojson.gz`, `cameras-us-hourly.pmtiles`, `cameras-ca-hourly.pmtiles`, hash keys `cameras-<cc>-hourly.geojson.sha256`.
- `.geojson.gz` keys hold **uncompressed** JSON bodies (bucket convention; Cloudflare compresses at the edge, `build.sh` sniffs magic bytes).
- Feature floors: US 50,000 / CA 300. Size floors: US 10 MB / CA 80 KB (unchanged).
- Hourly keys get `Cache-Control: public, max-age=300, s-maxage=3600`; all other keys keep `public, max-age=3600, s-maxage=86400`.
- CORS: no allowlist change (new app develops against already-allowed `http://localhost:3000`; production origin added later).
- The `flockhopper 3` repo has unrelated uncommitted app changes — commit ONLY `worker/` files there, never `git add -A`.

## Repos involved

| Repo | Path | Role |
|------|------|------|
| Data (this repo) | `/Users/jackcauthen/Documents/Developer/FLOCK/Data` | fetch.mjs, build.sh, workflows, docs |
| flockhopper 3 | `/Users/jackcauthen/Documents/Developer/FLOCK/DEFLOCK Website/DEFLOCK MAPS/FOGGED LENS/flockhopper 3` | data worker (`worker/`), serves data.dontgetflocked.com |

---

### Task 1: fetch.mjs — `-hourly` output names, drop the merged output

**Files:**
- Modify: `data/cameras/fetch.mjs`
- Modify: `data/cameras/upload.sh` (comment only)

**Interfaces:**
- Produces: output dir containing `cameras-us-hourly.geojson`, `cameras-ca-hourly.geojson`, `meta.json` keyed by those same names. `upload.sh` (unchanged logic) uploads each `cameras*.geojson` as `<name>.geojson.gz`.
- The merged `cameras.geojson` output is REMOVED — via upload.sh it would upload to `cameras.geojson.gz`, the daily worker's US key (clobber hazard).

- [ ] **Step 1: Edit `data/cameras/fetch.mjs`**

Change the header comment (lines 2–4):

```js
// Fetches ALPR camera data from Overpass for the US and Canada and writes
// per-country GeoJSON (hourly naming: cameras-<cc>-hourly.geojson) to an
// output directory, plus meta.json with the metadata upload.sh attaches
// to each R2 object.
```

Remove `mergeFeatureCollections` from the import (it stays in `lib.mjs`, still exported and unit-tested):

```js
import {
  buildCamerasQuery,
  queryOverpass,
  transformOverpassToGeoJSON,
} from './lib.mjs';
```

In `main()`, change the per-country output name (line 56):

```js
    const name = `cameras-${country.slug}-hourly`;
```

Delete the `collections` array (declaration on line 37, `collections.push(fc)` on line 59) and the entire merged-output block (lines 62–69):

```js
  const merged = mergeFeatureCollections(collections);
  console.log(`==> Merged: ${merged.features.length} features`);
  await writeFile(join(outDir, 'cameras.geojson'), JSON.stringify(merged));
  meta['cameras'] = {
    featureCount: merged.features.length,
    lastUpdated,
    source: 'overpass',
  };
```

- [ ] **Step 2: Update the `upload.sh` header comment**

Replace the first comment paragraph (keep the env var docs and the uncompressed-body convention note):

```bash
# Uploads the GeoJSON files produced by fetch.mjs to the R2 data bucket.
# fetch.mjs emits hourly-suffixed names (cameras-<cc>-hourly.geojson), so
# uploads land on the -hourly keys and can never touch the daily Worker
# cron's keys (cameras.geojson.gz / cameras-ca.geojson.gz).
```

No logic change: the `for file in "${OUT_DIR}"/cameras*.geojson` loop and `${name}.geojson.gz` key construction already produce the right keys from the renamed files.

- [ ] **Step 3: Verify syntax and existing tests**

Run:
```bash
cd /Users/jackcauthen/Documents/Developer/FLOCK/Data
node --check data/cameras/fetch.mjs
node --test data/cameras/lib.test.mjs
bash -n data/cameras/upload.sh
```
Expected: `node --check` silent; all lib tests PASS (none touch fetch.mjs); `bash -n` silent.

Note: fetch.mjs has no unit harness (it is Overpass+filesystem orchestration); its end-to-end verification is the `workflow_dispatch` in Task 4.

- [ ] **Step 4: Grep guard — no remaining merged-output references**

Run: `grep -n "cameras.geojson'" data/cameras/fetch.mjs; grep -n "mergeFeatureCollections" data/cameras/fetch.mjs`
Expected: no output from either (exit 1).

- [ ] **Step 5: Commit**

```bash
git add data/cameras/fetch.mjs data/cameras/upload.sh
git commit -m "Emit hourly-suffixed geojson, drop merged output that clobbered daily US key"
```

---

### Task 2: build.sh — read hourly geojson, write hourly pmtiles

**Files:**
- Modify: `tiles/cameras/build.sh`

**Interfaces:**
- Consumes: R2 keys `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz` in `$R2_DATA_BUCKET` (written by Task 1's pipeline).
- Produces: `cameras-us-hourly.pmtiles`, `cameras-ca-hourly.pmtiles`, `cameras-<cc>-hourly.geojson.sha256` in `$R2_TILES_BUCKET`. The tiles worker serves them automatically as `/cameras-<cc>-hourly.json` + `/cameras-<cc>-hourly/{z}/{x}/{y}.mvt`.

- [ ] **Step 1: Edit `tiles/cameras/build.sh`**

Replace `country_source_key()` (lines 52–61), updating the comment:

```bash
# Source keys in R2_DATA_BUCKET — the hourly GitHub Actions ingestion's
# outputs. The daily Worker cron's keys (cameras.geojson.gz /
# cameras-ca.geojson.gz) are a separate dataset this pipeline never reads
# or writes.
country_source_key() {
  case "$1" in
    us) echo "cameras-us-hourly.geojson.gz" ;;
    ca) echo "cameras-ca-hourly.geojson.gz" ;;
    *) return 1 ;;
  esac
}
```

In the `--country` block, change the three name constructions (lines 128–130):

```bash
  OUTPUT_FILE="cameras-${CC}-hourly.pmtiles"
  GEOJSON_FILE="cameras-${CC}-hourly.geojson"
  HASH_FILE="cameras-${CC}-hourly.geojson.sha256"
```

Update the file header comment (line 13):

```bash
#   R2_DATA_BUCKET   — bucket holding cameras-<cc>-hourly.geojson.gz (read)
```

Everything else (floors, two-pass tippecanoe, verify.sh, skip-check, failure isolation, `--local` mode, `BUILD_CONFIG`) is unchanged. `BUILD_CONFIG` needs no bump: the hourly hash keys are new, so first runs find `OLD_HASH=none` and build.

- [ ] **Step 2: Lint**

Run:
```bash
cd /Users/jackcauthen/Documents/Developer/FLOCK/Data
bash -n tiles/cameras/build.sh
shellcheck tiles/cameras/build.sh
```
Expected: both clean (shellcheck was clean before this change; no new warnings).

- [ ] **Step 3: Behavior checks without R2**

Unknown-arg guard still works:
```bash
bash tiles/cameras/build.sh --countr us; echo "exit=$?"
```
Expected: `ERROR: unknown argument '--countr' ...`, `exit=1`.

Local mode still works (CA is small; requires tippecanoe + jq installed, as used for previous builds):
```bash
bash tiles/cameras/build.sh --local tiles/local-dev/cameras-ca.geojson /tmp/ca-check.pmtiles
```
Expected: two tippecanoe passes, tile-join, `verify.sh` passes, `==> Done (local)`.

- [ ] **Step 4: Commit**

```bash
git add tiles/cameras/build.sh
git commit -m "Build hourly pmtiles from hourly geojson keys"
```

---

### Task 3: Data worker — short cache TTL for `-hourly` keys (TDD)

**Files:**
- Modify: `/Users/jackcauthen/Documents/Developer/FLOCK/DEFLOCK Website/DEFLOCK MAPS/FOGGED LENS/flockhopper 3/worker/src/index.ts:59-67`
- Test: `/Users/jackcauthen/Documents/Developer/FLOCK/DEFLOCK Website/DEFLOCK MAPS/FOGGED LENS/flockhopper 3/worker/tests/index.test.ts`

**Interfaces:**
- Produces: `GET /cameras-<cc>-hourly.geojson.gz` responds with `Cache-Control: public, max-age=300, s-maxage=3600`; every other dataset keeps `public, max-age=3600, s-maxage=86400`. No route/CORS changes.

- [ ] **Step 1: Write the failing test**

In `tests/index.test.ts`, add `'cameras-us-hourly.geojson.gz'` to the mock bucket (line 43):

```ts
  const mockBucket = createMockBucket({
    'cameras.geojson.gz': {
      body: 'gzipped-data',
      etag: '"abc123"',
      metadata: { 'x-last-updated': '2026-03-20T08:00:00Z', 'x-feature-count': '62000' },
    },
    'cameras-us-hourly.geojson.gz': {
      body: 'hourly-data',
      etag: '"hourly1"',
      metadata: { 'x-last-updated': '2026-07-17T14:05:00Z', 'x-feature-count': '114000' },
    },
  });
```

Add the test after the `'serves dataset from R2 with correct headers'` test:

```ts
  it('serves hourly datasets with a short cache TTL', async () => {
    const req = new Request('https://data.dontgetflocked.com/cameras-us-hourly.geojson.gz', {
      headers: { Origin: 'https://dontgetflocked.com' },
    });

    const res = await handleFetchRequest(req, env);

    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toBe('public, max-age=300, s-maxage=3600');
  });
```

Note: the existing `'returns dataset index at /'` test asserts `toHaveLength(1)` — adding a second mock object makes it 2. Update it:

```ts
    expect(body.datasets).toHaveLength(2);
    expect(body.datasets.map((d: { name: string }) => d.name).sort()).toEqual([
      'cameras',
      'cameras-us-hourly',
    ]);
```

- [ ] **Step 2: Run tests to verify the new one fails**

Run:
```bash
cd "/Users/jackcauthen/Documents/Developer/FLOCK/DEFLOCK Website/DEFLOCK MAPS/FOGGED LENS/flockhopper 3/worker"
npm test
```
Expected: `serves hourly datasets with a short cache TTL` FAILS (got `public, max-age=3600, s-maxage=86400`); index test passes after its update; rest pass.

- [ ] **Step 3: Implement in `src/index.ts`**

Replace the final response (lines 59–67):

```ts
  // Hourly datasets are republished every hour by the Actions pipeline —
  // don't let the edge hold them for the daily datasets' 24h.
  const isHourly = key.includes('-hourly');
  return new Response(obj.body, {
    status: 200,
    headers: {
      'Content-Type': 'application/geo+json',
      'Cache-Control': isHourly
        ? 'public, max-age=300, s-maxage=3600'
        : 'public, max-age=3600, s-maxage=86400',
      ETag: obj.etag,
      ...cors,
    },
  });
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `npm test` (same directory)
Expected: all tests PASS.

- [ ] **Step 5: Deploy and verify the daily endpoint is unchanged**

```bash
npx wrangler deploy
curl -sI https://data.dontgetflocked.com/cameras.geojson.gz | grep -i cache-control
```
Expected: deploy succeeds (wrangler is authenticated on this machine); header is `cache-control: public, max-age=3600, s-maxage=86400`. (The hourly URL still 404s — no data yet; verified in Task 4.)

- [ ] **Step 6: Commit (worker files only — repo has unrelated uncommitted app changes)**

```bash
cd "/Users/jackcauthen/Documents/Developer/FLOCK/DEFLOCK Website/DEFLOCK MAPS/FOGGED LENS/flockhopper 3"
git add worker/src/index.ts worker/tests/index.test.ts
git commit -m "feat(worker): short cache TTL for -hourly datasets"
```

---

### Task 4: Enable + run hourly ingestion, verify no daily clobber

**Files:** none (operational: git push, `gh`, `curl`)

**Interfaces:**
- Consumes: Task 1's code on the repo's main branch; Task 3's deployed worker.
- Produces: live `https://data.dontgetflocked.com/cameras-us-hourly.geojson.gz` and `.../cameras-ca-hourly.geojson.gz`; hourly cron (`5 * * * *`) active.

- [ ] **Step 1: Record the daily datasets' pre-run state**

```bash
curl -s https://data.dontgetflocked.com/ | jq '.datasets[] | select(.name == "cameras" or .name == "cameras-ca") | {name, lastUpdated}' | tee /tmp/daily-before.json
```
Expected: two entries with the daily cron's timestamps (08:0x UTC).

- [ ] **Step 2: Push and enable the workflow**

```bash
cd /Users/jackcauthen/Documents/Developer/FLOCK/Data
git push
gh workflow enable "Fetch Camera Data"
gh workflow run "Fetch Camera Data"
```

- [ ] **Step 3: Watch the run to completion**

```bash
gh run watch $(gh run list --workflow "Fetch Camera Data" --limit 1 --json databaseId --jq '.[0].databaseId')
```
Expected: green. Takes minutes (two Overpass queries + upload). If Overpass times out, the run fails without uploading anything (floors) — re-dispatch once before debugging.

- [ ] **Step 4: Verify the new endpoints and headers**

```bash
curl -sI https://data.dontgetflocked.com/cameras-us-hourly.geojson.gz | grep -iE "HTTP|cache-control|etag"
curl -s https://data.dontgetflocked.com/cameras-ca-hourly.geojson.gz | jq '.features | length'
```
Expected: US → `HTTP/2 200`, `cache-control: public, max-age=300, s-maxage=3600`; CA → a number ≥ 300.

- [ ] **Step 5: Clobber check — daily datasets untouched**

```bash
curl -s https://data.dontgetflocked.com/ | jq '.datasets[] | select(.name == "cameras" or .name == "cameras-ca") | {name, lastUpdated}' | diff /tmp/daily-before.json -
```
Expected: no diff (daily `lastUpdated` values unchanged by the hourly run). If the daily 08:00 UTC cron happened to fire between Steps 1 and 5, timestamps legitimately move to 08:0x of today — anything else is a clobber; stop and investigate.

---

### Task 5: Enable + run hourly tile build, verify tiles and skip path

**Files:** none (operational: `gh`, `curl`)

**Interfaces:**
- Consumes: Task 2's code on main; Task 4's hourly geojson objects in R2.
- Produces: live `https://tiles.dontgetflocked.com/cameras-us-hourly.json` / `-ca-hourly.json` + tile URLs; hourly cron (`:23`) active.

- [ ] **Step 1: Enable and dispatch**

```bash
cd /Users/jackcauthen/Documents/Developer/FLOCK/Data
gh workflow enable "Build Tiles"
gh workflow run "Build Tiles"
gh run watch $(gh run list --workflow "Build Tiles" --limit 1 --json databaseId --jq '.[0].databaseId')
```
Expected: green; log shows both countries building (`OLD_HASH=none` on first run), verify.sh passing, uploads of `cameras-us-hourly.pmtiles` (~30 MB) and `cameras-ca-hourly.pmtiles` (~100–200 KB).

- [ ] **Step 2: Verify tile endpoints**

```bash
curl -s https://tiles.dontgetflocked.com/cameras-us-hourly.json | jq '{minzoom, maxzoom, tiles}'
curl -s -o /dev/null -w "%{http_code} %{size_download} bytes\n" https://tiles.dontgetflocked.com/cameras-us-hourly/0/0/0.mvt
curl -s -o /dev/null -w "%{http_code} %{size_download} bytes\n" https://tiles.dontgetflocked.com/cameras-ca-hourly/10/286/373.mvt
```
Expected: TileJSON with `minzoom: 0, maxzoom: 14` and `-hourly` tile URLs; both tiles `200` with nonzero bytes (the second is Toronto z10 — CA data present).

- [ ] **Step 3: Daily endpoints still healthy**

```bash
curl -s -o /dev/null -w "us.json %{http_code}\n"  https://tiles.dontgetflocked.com/cameras-us.json
curl -s -o /dev/null -w "ca.json %{http_code}\n"  https://tiles.dontgetflocked.com/cameras-ca.json
curl -s -o /dev/null -w "merged  %{http_code}\n"  https://tiles.dontgetflocked.com/cameras/0/0/0.mvt
curl -s -o /dev/null -w "daily-geojson %{http_code}\n" https://data.dontgetflocked.com/cameras.geojson.gz
```
Expected: all `200`.

- [ ] **Step 4: Skip-path check — second run with unchanged data**

```bash
gh workflow run "Build Tiles"
gh run watch $(gh run list --workflow "Build Tiles" --limit 1 --json databaseId --jq '.[0].databaseId')
```
Expected: green, fast; log shows `Source unchanged (…) — skipping build` for both countries. (If the hourly fetch cron landed new data in between, a real build instead of a skip is also correct — check the log's stated reason.)

---

### Task 6: Documentation

**Files:**
- Modify: `README.md`
- Modify: `data/README.md`
- Modify: `tiles/cameras/README.md`
- Modify: `docs/setup-guide.md`

**Interfaces:** none (docs only). Core message everywhere: two datasets exist — **hourly** (`-hourly` names, GitHub Actions, for the new app) and **daily** (legacy names, Worker cron, for FlockHopper) — and this repo's pipelines produce only the hourly one.

- [ ] **Step 1: Update `README.md`**

Replace the endpoint table (lines ~17–18) and transition note with a two-dataset table:

```markdown
| Dataset | Cadence | Producer | GeoJSON | TileJSON |
|---------|---------|----------|---------|----------|
| Hourly (new app) | hourly | This repo's GitHub Actions | `https://data.dontgetflocked.com/cameras-us-hourly.geojson.gz` / `…-ca-hourly…` | `https://tiles.dontgetflocked.com/cameras-us-hourly.json` / `…-ca-hourly.json` |
| Daily (FlockHopper) | daily 08:00 UTC | Cloudflare Worker cron | `https://data.dontgetflocked.com/cameras.geojson.gz` / `…-ca…` | `https://tiles.dontgetflocked.com/cameras-us.json` / `…-ca.json` (frozen: `cameras.pmtiles`) |

> The daily tiles (`cameras-us.pmtiles`, `cameras-ca.pmtiles`) and the legacy merged `cameras.pmtiles` are frozen — no pipeline rebuilds them; they keep serving FlockHopper until it migrates.
```

Update the pipeline paragraphs (lines ~64–72): ingestion "uploads `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz`" (no merged upload); tile build "builds from `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz`, uploads `cameras-us-hourly.pmtiles` / `cameras-ca-hourly.pmtiles`". Update the example source names in the MapLibre snippet (lines ~44–50) to `cameras-us-hourly` / `cameras-ca-hourly` with the `-hourly.json` URLs.

- [ ] **Step 2: Update `data/README.md`**

Line 12: "uploads three datasets" → "uploads the two per-country datasets". Replace the dataset table (lines 16–18) with:

```markdown
| `cameras-us-hourly.geojson.gz` | US only |
| `cameras-ca-hourly.geojson.gz` | Canada only |
```

Below the table, add: "There is no merged output — the merged upload key (`cameras.geojson.gz`) is the daily Worker cron's US key, and this pipeline must never write the daily dataset." Line 22: the tile build "picks up the fresh `cameras-<cc>-hourly.geojson.gz` keys the same hour."

- [ ] **Step 3: Update `tiles/cameras/README.md`**

Line 3: archives are `cameras-us-hourly.pmtiles` and `cameras-ca-hourly.pmtiles`. Line 7: source keys `cameras-<cc>-hourly.geojson.gz`, hash `cameras-<cc>-hourly.geojson.sha256`.

- [ ] **Step 4: Update `docs/setup-guide.md`**

Swap object names throughout: sources `cameras-us-hourly.geojson.gz` / `cameras-ca-hourly.geojson.gz` (lines ~9, 17), outputs `cameras-us-hourly.pmtiles` / `cameras-ca-hourly.pmtiles` (lines ~3, 18, 39, 91, 102), verification URLs `…/cameras-us-hourly.json`, `…/cameras-us-hourly/0/0/0.mvt`, `…/cameras-ca-hourly/10/286/373.mvt` (lines ~113–127). Add one sentence to the intro noting the daily dataset (legacy names, Worker cron) coexists in the same buckets and is out of scope.

- [ ] **Step 5: Verify no stale references, then commit**

```bash
cd /Users/jackcauthen/Documents/Developer/FLOCK/Data
grep -rn "cameras-us\.\|cameras-ca\.\|cameras\.geojson" README.md data/README.md tiles/cameras/README.md docs/setup-guide.md | grep -v hourly | grep -v -i "daily\|frozen\|legacy\|worker cron"
```
Expected: no output — every non-hourly name that remains is in a line explicitly describing the daily/legacy dataset.

```bash
git add README.md data/README.md tiles/cameras/README.md docs/setup-guide.md
git commit -m "Document hourly vs daily camera datasets"
git push
```
