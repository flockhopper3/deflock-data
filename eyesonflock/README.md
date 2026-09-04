# eyesonflock/

The sharing-network pipeline: turns an [EyesOnFlock](https://eyesonflock.com) snapshot of Flock Safety transparency portals into the two files the network map consumes.

| Output | What it is |
|--------|------------|
| `sharing-network-nodes.geojson` | One `Point` per law-enforcement agency (~6.5k), with portal metrics, flags, and `connectionCount` |
| `sharing-network-adjacency.json` | `{ portalSlug: [slug, …] }` — outbound sharing edges declared on each portal (~270k directed) |
| `meta.json` | Run summary: counts, geocode-method breakdown, snapshot size, `generatedAt`, `runId` |

The output schema is frozen — the website's `networkStore.ts` depends on the exact 18 property keys on every feature, and step 06 fails the run if they drift. How the data is derived and what to trust about it is in [`METHODOLOGY.md`](METHODOLOGY.md).

This hub is independent of `data/` and `tiles/`: Python, standard library only, no shared code.

## Status

| Phase | What | State |
|-------|------|-------|
| 1 | Pipeline in this repo, unit-tested, verified outputs published as **workflow artifacts** | built, not yet run on GitHub |
| 2 | Upload verified outputs to the `flockhopper-tiles` R2 bucket | not started — after the owner reviews phase-1 artifacts |
| 3 | Website fetches from the CDN instead of bundled static files | not started |

## Running locally

Python 3.10+; nothing to `pip install` for the pipeline itself.

```bash
python eyesonflock/scripts/run_pipeline.py                # fetch a fresh snapshot and rebuild everything
python eyesonflock/scripts/run_pipeline.py --skip-fetch   # rebuild from the snapshot already in the work dir
```

Generated files land in `eyesonflock/work/` (gitignored): `raw/` snapshot, `intermediate/` parse and geocode stages plus the audit report, `output/` the three deliverables. Set `EYESONFLOCK_WORK_DIR` to put them somewhere else — that is the only knob, and it is the one the workflow uses.

Optional: a Google Geocoding key in `eyesonflock/.env` (see `.env.example`) or `GOOGLEMAPSAPI` in the environment lets step 02a resolve agencies the Census gazetteers could only place at a state centroid. Without it the step still applies the committed cache (`google_geocode_cache.json`, ~1.5k entries) and makes no network calls. With it, new lookups are written back to that file — commit them when convenient.

Tests:

```bash
pip install -r eyesonflock/requirements-dev.txt   # pytest
python -m pytest eyesonflock/tests -q
```

## The workflow

[`.github/workflows/eyesonflock-pipeline.yml`](../.github/workflows/eyesonflock-pipeline.yml) runs Mondays 06:00 UTC and on manual dispatch:

1. Unit tests — a red suite never reaches the fetch.
2. Restore the previous run's raw snapshot from the Actions cache (best-effort; feeds step 00's ≥50%-of-prior guard and diff summary).
3. `run_pipeline.py` with `EYESONFLOCK_WORK_DIR=/tmp/eyesonflock-work`. `GOOGLEMAPSAPI` is read from the repo secret of the same name if one exists; otherwise cache-only.
4. `meta.json` and the geocode audit go into the job summary.
5. Artifacts (30-day retention): `sharing-network-outputs` (the deliverables + audit), `eyesonflock-snapshot` (raw JSON, kept even on failure), `google-geocode-cache`.

No R2 credentials are used. Nothing is uploaded anywhere but the run's own artifacts.

### Fail-closed points

| Where | Refuses when |
|-------|--------------|
| step 00 | non-JSON / missing `portals` or `summary`; fewer than 500 portals (reference: 908); or fewer than 50% of the prior snapshot's portals when a prior exists |
| step 02a | a Google result lands outside the declared state's bbox (cached entry is dropped so a later run can retry) |
| step 06 | any output invariant: not a FeatureCollection; < 5,000 features or < 700 portals; bad or out-of-range coordinates; property keys differ from the 18-key schema; duplicate or empty `id`; `portalSlug` present without `isPortal` or vice versa; adjacency key/target that isn't a node; list not sorted/deduped or containing a self-edge; < 100,000 directed edges; any node whose `connectionCount ≠ |outbound ∪ inbound|`; more than 2% of features at the DC default fallback |

The floors sit far below the 2026-04 reference (6,461 features / 906 portals / 272,290 edges); they catch a truncated or broken build, not a real decline.

## Reference runs

| Run | in: portals | featureCount | portalCount | adjacencyKeys | directedEdges | geocode methods |
|---|---|---|---|---|---|---|
| 2026-04-24 snapshot, offline rebuild with this code | 908 | 6,461 | 906 | 528 | 272,290 | place 3,712 · county 1,014 · google 1,399 (cache) · place_variant 279 · junk 37 · state 19 · manual 1 |
| 2026-09-04 live fetch, cache-only, no prior | 1,046 | 6,793 | 1,044 | 608 | 289,794 | place 3,900 · county 1,062 · google 1,367 (cache) · place_variant 291 · state 132 · junk 28 · default 12 · manual 1 |

The April rebuild is identical to the research repo's last run except that `connectionCount` is now consistent with the adjacency file for all nodes (66 disagreed before — see METHODOLOGY, step 3). The September run passed every step-06 invariant on a first-run path (no prior snapshot). Its 144 `state`/`default` orgs are agencies added since April that the committed Google cache has never seen; a run with `GOOGLEMAPSAPI` set would resolve most of them and grow the cache. Step 00's diff against the April snapshot: 138 portals added, 0 removed, 536 sharing lists changed.

## Refreshing the gazetteers

The three Census gazetteer files in `gazetteer/` are the offline geocoder and change about once a year. To move to a new vintage: bump `GAZETTEER_VINTAGE` in `scripts/paths.py`, delete the old files, run `python eyesonflock/scripts/00_download_gazetteer.py`, run the pipeline offline to check the geocode-method breakdown didn't regress, and commit.

## Phase 2 plan (not built)

- Keys in `flockhopper-tiles`: `sharing-network-nodes.geojson`, `sharing-network-adjacency.json`, `sharing-network-meta.json`; `Cache-Control: public, max-age=86400`; `x-generated-at` / `x-feature-count` / `x-source=eyesonflock` metadata from `meta.json`, mirroring `data/cameras/upload.sh`.
- Upload step runs only after step 06 passes; same `R2_*` secrets as the camera workflows.
- The prior-snapshot guard moves from the Actions cache to an R2 object (`pipeline/eyesonflock-snapshot.json`).
- Serving the keys through the tiles Worker is a Worker change in the research repo — owner deploys.

Phase 3 is a URL swap in `networkStore.ts`; the schema does not change.
