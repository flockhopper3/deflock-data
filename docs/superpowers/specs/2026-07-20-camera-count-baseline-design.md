# Silent-failure hardening + national count baseline — Design

**Date:** 2026-07-20
**Status:** Approved

## Goal

Close the hourly camera pipeline's silent-corruption paths, where Overpass returns HTTP 200 with a failed query and the pipeline reads the result as legitimate data.

Two independent parts:

1. **Correctness** — teach the fetcher to distinguish "genuinely empty" from "the query failed", so those cases fail closed through machinery that already exists.
2. **Baseline** — record national feature counts over time and refuse to publish a run that falls well below the recent norm, replacing hardcoded floors that were never revisited against a growing dataset.

Part 1 is where the real correctness win is. Part 2 catches what Part 1 structurally cannot: gradual drift, mass OSM imports/reverts, and tagging-schema changes.

## Background: the failure

`data/cameras/tiled-fetch.mjs` covers the US with an adaptive tile grid. Each tile is first probed with a cheap `out count;` query to decide whether to subdivide it:

```js
// tiled-fetch.mjs:56-62
export async function countTile(t, fetchImpl = fetch) {
  const query = `[out:json][timeout:60];(${tileSelector(t)});out count;`;
  const data = await queryOverpass(query, fetchImpl, { allowEmpty: true });
  const countEl = data.elements?.[0];
  const total = countEl?.tags?.total;
  return total ? Number(total) : 0;
}
```

```js
// tiled-fetch.mjs:77
if (count === 0) return; // empty tile — drop it
```

When Overpass times out server-side it responds **HTTP 200** with `{"elements": [], "remark": "runtime error: Query timed out..."}`. `countTile` maps that to `0`, and `planLeafTiles` drops the tile from the work plan entirely. Because the tile is never fetched, `fetchTileInto`'s integrity check — the mechanism that exists specifically to catch silent holes (`lib.mjs:121-124`) — never runs on it.

Nothing in the repo reads `remark`. Confirmed by grep across `data/` and `tiles/`.

**Blast radius.** Losing the Southeast seed tile removes roughly 25K cameras. The result (~82K) clears `RAW_MIN_TOTAL` (50,000), clears `US_MIN_FEATURES` (50,000), and clears the duplicate floor in `tiles/cameras/build.sh:51-57`. Tiles rebuild and publish. Every check reports green while users across an entire region see an empty map.

Two related bugs in the same file share the root cause — a failed Overpass response being read as data:

- **`tiled-fetch.mjs:61`** — a malformed truthy `total` yields `NaN`. `NaN === 0` is false and `NaN <= SPLIT_THRESHOLD` is false, so the tile subdivides unconditionally until `MIN_TILE_SPAN`: ~65,000 requests per affected seed tile against free public Overpass mirrors, until the 45-minute job timeout.
- **`tiled-fetch.mjs:30`** — `MX_AREA_MIN_COUNT = 0` means `belowMinimum` can never fire, so an empty MX response is indistinguishable from a failed one. Mexican border cameras picked up by the US tile grid are never subtracted and ship as US cameras. Counts go *up*, so no floor fires.

## Part 1 — Failure discrimination

### The primitive

One helper in `data/cameras/lib.mjs`, used by every caller that passes `allowEmpty: true`:

```js
/**
 * Overpass signals a server-side failure (query timeout, out of memory, rate
 * limit) as HTTP 200 with a top-level `remark` and absent/partial elements.
 * Callers passing allowEmpty:true bypass the empty-response guard, so they
 * must use this to tell "genuinely empty" from "the query failed".
 */
export function overpassFailed(data) { ... }
```

Returns true when `data.remark` matches a known error pattern (`/timed out|runtime error|out of memory|too many requests/i`).

Remarks are logged verbatim whether or not they match, so an unrecognized failure remark shows up in CI logs and the pattern can be widened.

### `countTile` — structural check is primary

`out count;` returns exactly one count element. A genuinely empty bbox yields `total: '0'`; a failed query yields no element at all. That distinction is structural and needs no string matching, so it is the primary check. `overpassFailed` is defense in depth.

New behavior, in order:

1. `overpassFailed(data)` → throw
2. no count element present → throw (a real empty region returns `total: '0'`, not nothing)
3. `total` present but not finite → throw (NaN guard)
4. otherwise return the parsed integer, `0` included

Throwing is the whole point: `planLeafTiles` already wraps `countTile` in `retryWithBackoff` (`tiled-fetch.mjs:72`), and a persistent failure rejects `Promise.all`, which rejects `fetchAllCameras`, which fails the job. `build-tiles.yml:17` then skips the tile build and the previous tiles keep serving. No new failure machinery is needed — the fix just routes this case into the machinery that already works.

> **Verify before relying on it.** That `out count;` always returns a count element for an empty bbox is load-bearing and unproven against the live API. The implementation plan must confirm it empirically against a known-empty bbox (mid-ocean) as its first step. If it turns out Overpass returns `elements: []` for a genuinely empty region, drop check 2 and rely on `overpassFailed` alone.

### `fetchCountryArea` — makes a 0 floor safe

Add the `overpassFailed` check before the `belowMinimum` floor. This is what makes `MX_AREA_MIN_COUNT = 0` correct rather than dangerous: "0 because Mexico is quiet" and "0 because the query died" become distinguishable, so no arbitrary MX floor has to be invented. `CA_AREA_MIN_COUNT` is unchanged.

### `fetchTileInto`

Same check, before the integrity comparison. A remark-bearing response should be a clean retry rather than a confusing integrity-check failure message.

### Test changes

`tiled-fetch.test.mjs:137` (`'drops a tile whose probe count is zero'`) currently asserts the buggy behavior is correct. It must be split:

- a tile probing `total: '0'` is still dropped (unchanged behavior for real empty regions)
- a tile whose probe returns `elements: []` + `remark` **throws**
- a tile whose probe returns a malformed `total` **throws**

Plus a truth table for `overpassFailed`, and an `fetchCountryArea` case proving an empty-with-remark MX response throws while an empty-without-remark one returns an empty map.

## Part 2 — National count baseline

### What is recorded

Three numbers per run, all already computed today:

| Field | Source | Currently |
|-------|--------|-----------|
| `us` | `fetch.mjs:37` feature count | written to `meta.json` |
| `ca` | `fetch.mjs:37` feature count | written to `meta.json` |
| `rawTotal` | `tiled-fetch.mjs:187` pre-subtraction total | discarded |

`fetchAllCameras` returns `rawTotal` alongside `{ us, ca }` so `fetch.mjs` can record it. No changes to `planLeafTiles` or tile-level bookkeeping.

Per-seed-tile history was considered and dropped as unnecessary complexity. With Part 1 in place, a dropped seed tile is a hard failure rather than a silent one, so per-tile history only covers regional holes small enough to hide inside the national band *and* that bypass the discriminator. The JSONL format below can carry per-tile fields later without a migration.

### Storage

`s3://deflock-data/pipeline/counts-history.jsonl` — one JSON object per line, appended, capped at the most recent 720 entries (~30 days at hourly cadence).

```json
{"ts":"2026-07-20T06:05:00Z","us":117432,"ca":538,"rawTotal":118004,"status":"accepted","runId":"29721490530"}
```

`status` is `accepted` or `rejected`. The `concurrency: data-fetch` group guarantees a single writer, so read-modify-write is safe without locking.

### The check

For each of `us`, `ca`, `rawTotal`:

- baseline = median of the last **24 `accepted`** entries for that field
- **block** if the observed value is below **95%** of that baseline
- **observe-only** until at least **6 accepted samples** exist for that field

Median over accepted-only entries is what stops a bad run from becoming the new normal: rejected observations are recorded for forensics but never enter the baseline. Without that, repeated failures would walk the baseline down until the guard accepts anything.

The 95% band and the 24/6 sample counts are starting values, not measurements. They should be revisited once real history exists — the first days of data are the first opportunity to see actual hour-to-hour churn.

Warm-up means this ships inert and begins enforcing a few hours later. That is deliberate: it also means a legitimate future change to the fetch grid or country set degrades to observe-only rather than blocking the pipeline on a baseline that no longer applies.

The existing absolute floors (`US_MIN_FEATURES`, `CA_MIN_FEATURES`, `RAW_MIN_TOTAL`, and the mirrored floors in `tiles/cameras/build.sh`) all stay. Defense in depth — the baseline is relative and needs history; the floors are absolute and work on run one.

### Where it runs

`fetch.mjs` runs at `fetch-data.yml:27`, **before** `aws configure` at `:29`. That order is deliberate: R2 write credentials are not on disk while untrusted Overpass data is parsed. The design preserves it by splitting the work — `fetch.mjs` only writes numbers to `meta.json`, and a separate module does the R2 round-trip after credentials exist.

New step order in `fetch-data.yml`:

1. unit tests *(unchanged)*
2. `fetch.mjs` → `/tmp/data-out` — GeoJSON + `meta.json` including `rawTotal` *(no R2 access)*
3. `aws configure` *(unchanged)*
4. **new** — download `counts-history.jsonl` from R2; treat a missing object as empty history
5. **new** — `node data/cameras/baseline.mjs --meta /tmp/data-out/meta.json --history /tmp/counts-history.jsonl` — compares, writes the updated history file with the new entry's `status`, exits non-zero to block
6. `upload.sh` — only reached when step 5 passes
7. **new** — upload the updated history, `if: always()`, so rejected runs are recorded too
8. **new** — on failure, create or update a GitHub issue

Step 7 runs even when step 5 blocked; step 6 does not. A blocked run therefore leaves the previous hour's data serving and a rejected entry in the history.

### Alerting

Step 8 opens a GitHub issue titled `Camera pipeline blocked: count below baseline`, or comments on the existing open one, with the observed values, the baselines, and a run link. This needs `permissions: issues: write` on the job — the repo's `default_workflow_permissions` is currently `read`.

An issue is chosen over email because it is a visible, persistent artifact with a state that can be closed, rather than a notification that depends on a per-user setting nobody can verify from the repo.

### `baseline.mjs`

Pure functions plus a thin CLI, dependency-free, matching the existing `node:test` style:

- `parseHistory(text)` — JSONL → entries, tolerating a trailing newline and skipping malformed lines rather than throwing
- `baselineFor(entries, field, window)` — median over the last N `accepted` values
- `evaluate(observed, entries, config)` → `{ status, failures[] }`
- `appendCapped(entries, entry, cap)`

CLI wiring is a thin shell over these so the logic is testable without I/O.

## Testing

**Part 1** — extends `data/cameras/tiled-fetch.test.mjs` and `lib.test.mjs` as described above. All injected via the existing `fetchImpl` parameter; no network.

**Part 2** — new `data/cameras/baseline.test.mjs`:

- median over an even and an odd number of samples
- `rejected` entries excluded from the baseline
- **poison resistance**: a run of rejected entries does not move the baseline
- warm-up: fewer than 6 accepted samples → observe-only regardless of how far off the observed value is
- boundary: exactly at 95% passes, one below blocks
- a field absent from history (newly added) → observe-only
- malformed JSONL lines skipped, not fatal
- `appendCapped` drops oldest beyond the cap

Both suites run at `fetch-data.yml:24` before any fetch, so a broken guard fails the job rather than shipping.

## Out of scope

- **Dead-man's-switch / freshness monitoring.** A blocked run creates an issue; a *dropped cron run* creates nothing, and measured cron reliability is ~35% drops with gaps up to 3h21m. Detecting "the pipeline stopped running" is separate work and is not covered here.
- Per-seed-tile or per-state history.
- Rollback / retention for published R2 objects — there is still no previous version to roll back to.
- The `tiles/cameras/build.sh` floors that duplicate `fetch.mjs`'s constants. Unchanged here; deduplicating them is its own change.
