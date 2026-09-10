# EyesOnFlock sharing-network pipeline — Design

**Date:** 2026-09-04
**Status:** Phases 1–2 live on `main`; phase 3 pending

## Goal

Bring the EyesOnFlock "sharing network" data pipeline into this repo as its own hub and run it unattended on GitHub Actions, so the two artifacts the website consumes — `sharing-network-nodes.geojson` and `sharing-network-adjacency.json` — are rebuilt on a schedule from a fresh EyesOnFlock snapshot instead of by hand.

This is phase 1 of three:

| Phase | Deliverable | Status |
|-------|-------------|--------|
| 1 | Pipeline ported into `eyesonflock/`, unit-tested, verified outputs published as **workflow artifacts** | this spec |
| 2 | Publish verified outputs to a dedicated public R2 bucket | built 2026-09-10 — bucket `deflock`, custom domain `deflockdata.dontgetflocked.com`, own token; see `eyesonflock/README.md` *Public data*. The owner chose a fresh bucket + bucket-level custom domain over touching the live data Worker or the tiles bucket |
| 3 | Website `networkStore` fetches from the CDN instead of bundled static files | later |

Nothing in phase 1 touches Cloudflare. Nothing is pushed to GitHub without the owner's say-so (see `CLAUDE.md`).

## Background

The pipeline today lives in the research repo (`FLOCKHOPPER DATA RESEARCH/sharing-network/`). It is seven numbered Python scripts plus five library modules, standard library only, Python 3.10+:

| Step | Script | Reads → Writes |
|------|--------|----------------|
| 00 | `00_fetch_eyesonflock.py` | `GET eyesonflock.com/api/v1/data` → `raw/eyesonflock_full_data.json` (atomic, with a ≥50%-of-prior portal-count guard) |
| 01 | `01_parse_orgs.py` | raw snapshot → `intermediate/parsed_orgs.json` (canonical slugs, aliases, junk flags, portal metadata) |
| 02 | `02_geocode_orgs.py` | parsed orgs + 3 Census gazetteer files → `intermediate/geocoded_orgs.json` |
| 02a | `02a_google_geocode.py` | upgrades `state`/`default` fallbacks via Google Geocoding (cached; optional key) |
| 03 | `03_build_nodes_geojson.py` | geocoded orgs + raw snapshot → `output/sharing-network-nodes.geojson` |
| 04 | `04_build_adjacency.py` | raw snapshot + parsed orgs → `output/sharing-network-adjacency.json` |
| 05 | `05_audit_geocoding.py` | geocoded orgs → `intermediate/geocode_audit.txt` (report only) |

Reference run (2026-04-24 snapshot): 6,461 node features, 906 portals, 528 adjacency keys, 272,290 directed edges, zero dangling references. Property keys on every feature:

```
aliases cameras city connectionCount geocodeMethod hotlistHits id isInactive
isJunk isLikelyAggregator isPortal name population portalSlug searches state
type vehiclesCaptured
```

The website (`src/store/networkStore.ts` in the FlockHopper forks) loads both files from `/public/` and depends on exactly this shape. **The output schema is frozen by this design** — phase 3 swaps the URL, not the format.

Two things do not travel well from a laptop to CI:

1. Every script computes its data directories as `Path(__file__).parent.parent.parent / "data" / …`. Dropped into this repo that would resolve to the top-level `data/` hub, colliding with the camera ingestion code.
2. Step 02a skips entirely when no Google key is present — *before* consulting its on-disk cache — so a CI run without the key would lose ~1,400 precise geocodes that the cache already holds.

## Design

### Layout

A new top-level hub, fully independent of `data/` (cameras) and `tiles/`:

```
eyesonflock/
  README.md                    how to run, what the workflow does, phase 2/3 plan
  METHODOLOGY.md               ported from the research repo, paths updated
  .env.example                 GOOGLEMAPSAPI=
  .gitignore                   work/  .env
  requirements-dev.txt         pytest (tests only; the pipeline itself needs no packages)
  gazetteer/                   3 Census gazetteer .txt files (~7 MB, committed, ~annual refresh)
  google_geocode_cache.json    committed seed cache (1,552 query → lat/lng entries)
  scripts/
    paths.py                   NEW — the single source of truth for every path
    run_pipeline.py            orchestrator (adds step 06, --skip-fetch)
    00_fetch_eyesonflock.py    + absolute portal-count floor
    00_download_gazetteer.py   manual tool, not run by the orchestrator
    01_parse_orgs.py
    02_geocode_orgs.py
    02a_google_geocode.py      + cache-only mode when no key
    03_build_nodes_geojson.py
    04_build_adjacency.py
    05_audit_geocoding.py
    06_verify_outputs.py       NEW — hard gate + meta.json
    parse_orgs_lib.py  geocode_lib.py  junk_filter.py  google_geocode.py  state_bbox.py
  tests/                       5 ported test files + tests for the new/changed code
  work/                        gitignored default workspace: raw/ intermediate/ output/
```

### `paths.py`

```python
EOF_ROOT          = eyesonflock/
GAZETTEER_VINTAGE = "2023"
GAZETTEER_DIR     = EOF_ROOT / "gazetteer"
GOOGLE_CACHE_FILE = EOF_ROOT / "google_geocode_cache.json"
ENV_FILE          = EOF_ROOT / ".env"
WORK_DIR          = $EYESONFLOCK_WORK_DIR or EOF_ROOT / "work"
RAW_DIR, INT_DIR, OUTPUT_DIR = WORK_DIR / raw | intermediate | output
```

Committed inputs (gazetteers, cache seed) live in the repo. Everything generated lives under `WORK_DIR`, which CI points at `/tmp`. Scripts import these names instead of computing them; the `mkdir` side effects at import time move into `main()`.

### Step 00: absolute floor

The existing ≥50%-of-prior guard needs a prior snapshot. In CI the prior is restored best-effort from the Actions cache (see Workflow) and may be missing. Add `MIN_PORTAL_COUNT_ABS = 500` (reference: 908) as a second, always-on guard: a response with fewer portals is refused regardless of prior. Both guards stay; the relative one still catches a large drop when a prior exists.

### Step 02a: cache-only mode

`GoogleGeocoder(api_key=None)` becomes legal: cache lookups work, cache misses return `None` without a network call, `api_calls_made` stays 0. Step 02a always loads the cache and applies hits; it only warns when the key is missing (uncached candidates stay at state centroids). The bbox plausibility check applies to cached results too, exactly as before.

The cache file is read and written in place at its committed path. Locally that means a run may dirty `google_geocode_cache.json`; commit it when convenient. In CI the updated cache is uploaded as an artifact so new entries aren't lost.

**Added 2026-09-05 after the first keyed run.** (a) Refusals are not answers: only `OK`/`ZERO_RESULTS` are cached; `REQUEST_DENIED`, quota statuses, or three consecutive network failures open a per-API circuit for the rest of the run, and step 02a writes `google_geocode_run.json` (surfaced in the job summary). This came out of a run whose key pointed at a deleted Cloud project — 143 refusals would otherwise have been cached as permanent negatives. (b) A second source: Places API (New) text search with the raw agency name, tried when Geocoding yields nothing plausible, tagged `geocode_method="google_places"`. The bbox check applies to both; a rejected answer is skipped, not invalidated, so the next run re-checks it for free. On the September snapshot: Geocoding 127/144, Places 12/17, 5 left (parser limits).

### Step 06: verify + meta

A fail-closed gate, in the spirit of `tiles/cameras/verify.sh`. Exit 1 on any violation:

- `nodes.geojson` is a `FeatureCollection` with ≥ 5,000 features (reference 6,461).
- Every feature is a `Point` with two finite coordinates inside `[-180,180] × [-90,90]`.
- Every feature's property key set equals the frozen 18-key schema above — the website contract.
- `id` is non-empty and unique; `isPortal` features have a non-null `portalSlug`, non-portals have `null`.
- ≥ 700 portal features (reference 906).
- `adjacency.json` is an object; every key and every target is a known node `id`; each list is sorted, deduplicated, and free of self-edges.
- ≥ 100,000 directed edges (reference 272,290).
- For every node, `connectionCount == |outbound ∪ inbound|` computed from the adjacency file.
- `default`-method (Washington DC fallback) share ≤ 2% — the parse-failure signal.

On success it writes `output/meta.json`:

```json
{ "generatedAt": "...Z", "source": "https://eyesonflock.com/api/v1/data",
  "snapshotPortals": 908, "featureCount": 6461, "portalCount": 906,
  "adjacencyKeys": 528, "directedEdges": 272290,
  "geocodeMethods": {"place": 3712, ...}, "junk": 37, "inactive": 0,
  "likelyAggregators": 0, "runId": "<GITHUB_RUN_ID or null>" }
```

Phase 2's uploader reads `meta.json` for object metadata, mirroring `data/cameras/upload.sh`.

### Steps 03/04: one name→slug resolver

Discovered while building step 06: the documented invariant `connectionCount == |outbound ∪ inbound|` did not hold. Step 03 resolved sharing-list names with the bare `canonical_slug()` while step 04 consulted the `parsed_orgs.json` alias map first, so names that only resolve through aliases (the bare `"Berkeley"` manual alias, rescued stateless sheriff's-office forms) counted in one file and not the other — 66 nodes disagreed on the April snapshot (`berkeley-ca-pd`: 71 vs 148). `parse_orgs_lib` gains `build_alias_map()` and `make_shared_name_resolver()`; both steps use them. This is the one behaviour change in the port beyond the three additions above; it makes the outputs self-consistent and METHODOLOGY truthful.

### Orchestrator

`run_pipeline.py` runs 00 → 01 → 02 → 02a → 03 → 04 → 05 → 06, stopping at the first non-zero exit. `--skip-fetch` skips 00 for offline iteration against an existing snapshot. `--work-dir` is not a flag; `EYESONFLOCK_WORK_DIR` is the one knob, so the workflow and the scripts agree by construction.

### Workflow: `.github/workflows/eyesonflock-pipeline.yml`

- **Triggers:** `workflow_dispatch`, and `schedule: "0 6 * * 1"` (Mondays 06:00 UTC). Weekly is gentle on EyesOnFlock's public API and keeps the Actions cache below its 7-day idle eviction.
- **Concurrency:** group `eyesonflock-pipeline`, no cancel-in-progress.
- **Permissions:** `contents: read`.
- **Steps:**
  1. checkout; `setup-python` 3.12; `pip install -r eyesonflock/requirements-dev.txt`
  2. `pytest eyesonflock/tests` — a red test suite never reaches the fetch
  3. `actions/cache` restore of `/tmp/eyesonflock-work/raw/eyesonflock_full_data.json` with `restore-keys: eyesonflock-snapshot-` (best-effort prior for the relative guard and the diff summary)
  4. `python eyesonflock/scripts/run_pipeline.py` with `EYESONFLOCK_WORK_DIR=/tmp/eyesonflock-work` and `GOOGLEMAPSAPI: ${{ secrets.GOOGLEMAPSAPI }}` (optional secret; absent → cache-only)
  5. Append `meta.json` and the audit report to `$GITHUB_STEP_SUMMARY`
  6. Upload artifacts: `sharing-network-outputs` (nodes, adjacency, meta, audit), `eyesonflock-snapshot` (raw JSON), `google-geocode-cache` (updated cache). 30-day retention.
  7. Cache save of the new snapshot under `eyesonflock-snapshot-${{ github.run_id }}` (automatic post-step).

No R2 credentials, no upload. That is phase 2.

### `CLAUDE.md`

Created at the repo root with the owner's rule: never `git push`, never deploy or upload to Cloudflare (Workers, R2, Pages, wrangler) without explicit permission in the current conversation. Local commits on a branch are fine. Plus a two-line map of the hubs.

## Phase 2 contract (not built here)

- R2 bucket `flockhopper-tiles`, keys `sharing-network-nodes.geojson`, `sharing-network-adjacency.json`, `sharing-network-meta.json`.
- `Cache-Control: public, max-age=86400`; `x-generated-at`, `x-feature-count`, `x-source=eyesonflock` metadata from `meta.json`.
- Upload runs only after step 06 passes. Same `R2_*` secrets as the camera workflows.
- The prior-snapshot guard moves from the Actions cache to an R2 object (`pipeline/eyesonflock-snapshot.json`), matching how `counts-history.jsonl` is handled.
- Serving the keys through the tiles Worker is a Worker change in the research repo; the owner deploys it.

## Phase 3 contract (not built here)

`networkStore.ts` fetches the two files from the CDN URL instead of `/public/`. Schema unchanged, so the store's parsing code does not change.

## Testing

- All 115 original tests ported (path fix only) and passing; 171 total with the new ones.
- New tests: `paths.py` env override; `GoogleGeocoder` cache-only mode (hit, miss, zero API calls, no negative caching, invalidate); step 00 absolute floor; shared resolver + alias-aware `connectionCount`; step 06 — every invariant has a failing fixture, the happy path writes `meta.json`, `main()` returns 1 on violation or missing files.
- Offline rebuild of the 2026-04-24 snapshot reproduces the reference exactly (6,461 / 906 / 528 / 272,290) and passes step 06.
- Live run 2026-09-04, cache-only, no prior: 1,046 portals in → 6,793 features / 1,044 portals / 608 keys / 289,794 edges; schema identical to the reference; step 06 passes; 12 `default` (0.18%) and 132 `state` orgs are new agencies the cache has never seen.
- Workflow YAML parsed and structurally checked. It cannot be executed until the branch is pushed, which is the owner's call.

## Out of scope

- Any Cloudflare change, any push.
- Committing raw snapshots or generated outputs to git (repo policy: generated data is not versioned).
- Changing parsing, geocoding, or slugging behaviour. The port is behaviour-preserving apart from the three additions above and the step 03/04 resolver fix.
