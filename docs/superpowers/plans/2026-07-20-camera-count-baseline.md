# Silent-Failure Hardening + National Count Baseline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the hourly camera pipeline from reading a failed Overpass query as legitimate data, and refuse to publish a run whose national counts fall well below the recent norm.

**Architecture:** Part 1 adds one predicate (`overpassFailed`) to `data/cameras/lib.mjs` and applies it at the three sites that pass `allowEmpty: true`, routing silent failures into the retry/fail-closed machinery that already exists. Part 2 adds `data/cameras/baseline.mjs`, which compares this run's national counts against a median of recent accepted runs stored as JSONL in R2, and gates `upload.sh` on the result.

**Tech Stack:** Node 22 ESM, `node:test` + `node:assert/strict`, bash, AWS CLI against Cloudflare R2, GitHub Actions.

## Global Constraints

- **No new dependencies.** `data/cameras/` is deliberately dependency-free (`data/README.md`); there is no `package.json` in this directory and none may be added.
- **Node 22 ESM only** — `.mjs`, `import`/`export`, matching every existing file here.
- **Tests use `node:test`** with `describe`/`it` and `node:assert/strict`, matching `data/cameras/*.test.mjs`.
- **All network calls go through an injected `fetchImpl` parameter** so tests never touch the network. Production callers pass nothing and get global `fetch`.
- **Credential ordering in `fetch-data.yml` must not change.** `fetch.mjs` runs *before* `aws configure` so R2 write credentials are not on disk while untrusted Overpass data is parsed. New R2 access goes in steps *after* the configure step.
- **The existing absolute floors stay.** `US_MIN_FEATURES`, `CA_MIN_FEATURES` (`fetch.mjs:15-16`), `RAW_MIN_TOTAL` (`tiled-fetch.mjs:24`) and the mirrored floors in `tiles/cameras/build.sh:51-57` are unchanged. The baseline is additive defense in depth.
- **Commit after every task.** Work happens on branch `camera-count-baseline`.

## Verified Overpass behavior (do not re-litigate)

Confirmed live against `overpass-api.de` 0.7.62.11 on 2026-07-20:

- An **empty** bbox returns a count element with a zero total:
  ```json
  {"elements":[{"type":"count","id":0,"tags":{"nodes":"0","ways":"0","relations":"0","total":"0"}}]}
  ```
  It does **not** return `elements: []`. So a missing count element is unambiguously a failed query.
- Server overload returns an **HTML** error body (`runtime error: ... Dispatcher_Client::request_read_and_idx::timeout`), and rate limiting returns **HTTP 429**. Both are already handled — `lib.mjs:65-67` throws on non-2xx and `lib.mjs:74-80` throws on non-JSON.
- The gap this plan closes is the **JSON-with-`remark`** shape and the **missing-count-element** shape.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `data/cameras/lib.mjs` | Overpass transport + pure helpers | Add `overpassFailed`; log any remark in `queryOverpass` |
| `data/cameras/lib.test.mjs` | Tests for the above | Add `overpassFailed` truth table |
| `data/cameras/tiled-fetch.mjs` | Tiling, probing, orchestration | Harden `countTile`, `fetchTileInto`, `fetchCountryArea`; return `rawTotal` |
| `data/cameras/tiled-fetch.test.mjs` | Tests for the above | Rewrite the empty-probe test; add failure cases |
| `data/cameras/fetch.mjs` | CLI entry; writes GeoJSON + `meta.json` | Record `totals` in `meta.json` |
| `data/cameras/baseline.mjs` | **new** — history parsing, median, verdict, CLI | Create |
| `data/cameras/baseline.test.mjs` | **new** — tests for the above | Create |
| `.github/workflows/fetch-data.yml` | Hourly ingest job | Add history download, gate, history upload, issue-on-block |

Tasks 1–3 are Part 1 and are independently shippable. Tasks 4–7 are Part 2.

---

### Task 1: `overpassFailed` predicate

**Files:**
- Modify: `data/cameras/lib.mjs` (add export after `belowMinimum`, ~line 133; edit `queryOverpass` ~line 81)
- Test: `data/cameras/lib.test.mjs`

**Interfaces:**
- Consumes: nothing
- Produces: `overpassFailed(data: object) => boolean` — true when the response carries a top-level `remark` matching a known server-failure pattern. Used by Tasks 2 and 3.

- [ ] **Step 1: Write the failing test**

Append to `data/cameras/lib.test.mjs` (add `overpassFailed` to the existing import from `./lib.mjs`):

```js
describe('overpassFailed', () => {
  it('is false for a normal response with no remark', () => {
    assert.equal(overpassFailed({ elements: [] }), false);
    assert.equal(overpassFailed({ elements: [{ type: 'count', tags: { total: '0' } }] }), false);
  });

  it('detects a server-side query timeout', () => {
    assert.equal(
      overpassFailed({ elements: [], remark: 'runtime error: Query timed out in "query" at line 1' }),
      true
    );
  });

  it('detects out-of-memory and rate-limit remarks', () => {
    assert.equal(overpassFailed({ elements: [], remark: 'runtime error: Out of memory' }), true);
    assert.equal(overpassFailed({ elements: [], remark: 'Too many requests, please wait' }), true);
  });

  it('detects a failure remark even when elements are present (partial result)', () => {
    assert.equal(
      overpassFailed({ elements: [{ type: 'node', id: 1 }], remark: 'runtime error: Query timed out' }),
      true
    );
  });

  it('ignores a benign remark so an unrecognized notice cannot halt the pipeline', () => {
    assert.equal(overpassFailed({ elements: [], remark: 'Data generated at 2026-07-20' }), false);
  });

  it('tolerates null, undefined, and non-string remarks', () => {
    assert.equal(overpassFailed(undefined), false);
    assert.equal(overpassFailed(null), false);
    assert.equal(overpassFailed({}), false);
    assert.equal(overpassFailed({ remark: 42 }), false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test data/cameras/lib.test.mjs`
Expected: FAIL — `SyntaxError: The requested module './lib.mjs' does not provide an export named 'overpassFailed'`

- [ ] **Step 3: Write minimal implementation**

In `data/cameras/lib.mjs`, add after `belowMinimum` (~line 133):

```js
// Overpass reports a server-side failure (query timeout, out of memory, rate
// limit) as HTTP 200 with a top-level `remark` and absent or partial elements.
// Non-JSON and non-2xx failures are already caught in queryOverpass; this is
// the remaining shape.
const OVERPASS_ERROR_REMARK = /timed out|runtime error|out of memory|too many requests/i;

/**
 * True if an Overpass response carries a server-failure remark. Callers passing
 * `allowEmpty: true` bypass the empty-response guard in queryOverpass, so they
 * must use this to tell "genuinely empty" from "the query failed".
 */
export function overpassFailed(data) {
  const remark = data?.remark;
  if (typeof remark !== 'string') return false;
  return OVERPASS_ERROR_REMARK.test(remark);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test data/cameras/lib.test.mjs`
Expected: PASS, all existing tests still green.

- [ ] **Step 5: Log every remark in `queryOverpass`**

So an unrecognized failure remark is visible in CI and the pattern can be widened. In `data/cameras/lib.mjs`, immediately after the `JSON.parse` try/catch block and **before** the `if (!allowEmpty && ...)` guard (~line 81):

```js
      // Surface every remark, matched or not — an unrecognized failure remark
      // would otherwise pass overpassFailed() silently.
      if (typeof data.remark === 'string') {
        console.warn(`Overpass remark from ${endpoint}: ${data.remark}`);
      }

```

- [ ] **Step 6: Run the full data suite**

Run: `node --test data/cameras/*.test.mjs`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add data/cameras/lib.mjs data/cameras/lib.test.mjs
git commit -m "Add overpassFailed predicate and log all Overpass remarks"
```

---

### Task 2: Harden `countTile`

**Files:**
- Modify: `data/cameras/tiled-fetch.mjs:56-62`
- Test: `data/cameras/tiled-fetch.test.mjs:80-105` (rewrite the empty-probe case)

**Interfaces:**
- Consumes: `overpassFailed` from Task 1
- Produces: `countTile(t, fetchImpl)` now **throws** instead of returning `0` for a failed probe. Return type is unchanged (`Promise<number>`) for genuine counts including `0`.

**Context:** `planLeafTiles` already wraps `countTile` in `retryWithBackoff` (`tiled-fetch.mjs:72`), and a persistent throw rejects `Promise.all`, which rejects `fetchAllCameras` and fails the job. No new failure machinery is needed.

- [ ] **Step 1: Write the failing tests**

In `data/cameras/tiled-fetch.test.mjs`, **replace** the existing `it('returns 0 when the tile is empty (allowEmpty tolerated)', ...)` block (lines 87-91) with the following, and keep the other two `countTile` tests as they are:

```js
  it('returns 0 for a genuinely empty bbox (count element with total 0)', async () => {
    // Verified live 2026-07-20: overpass-api.de returns a count element with
    // total "0" for an empty bbox — never an empty elements array.
    const fetchImpl = async () =>
      jsonResponse({ elements: [{ type: 'count', id: 0, tags: { nodes: '0', ways: '0', relations: '0', total: '0' } }] });
    const n = await countTile({ s: 0, w: 0, n: 1, e: 1 }, fetchImpl);
    assert.equal(n, 0);
  });

  it('throws when the probe returns no count element (failed query, not an empty region)', async () => {
    const fetchImpl = async () => jsonResponse({ elements: [] });
    await assert.rejects(
      () => countTile({ s: 0, w: 0, n: 1, e: 1 }, fetchImpl),
      /no count element/
    );
  });

  it('throws when the probe carries a server-failure remark', async () => {
    const fetchImpl = async () =>
      jsonResponse({ elements: [], remark: 'runtime error: Query timed out in "query" at line 1' });
    await assert.rejects(
      () => countTile({ s: 0, w: 0, n: 1, e: 1 }, fetchImpl),
      /probe failed/
    );
  });

  it('throws on a non-numeric total instead of subdividing forever', async () => {
    // NaN is neither === 0 nor <= SPLIT_THRESHOLD, so planLeafTiles would split
    // this tile until MIN_TILE_SPAN — ~65K requests against public mirrors.
    const fetchImpl = async () => jsonResponse({ elements: [{ type: 'count', tags: { total: 'many' } }] });
    await assert.rejects(
      () => countTile({ s: 0, w: 0, n: 1, e: 1 }, fetchImpl),
      /non-numeric total/
    );
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test data/cameras/tiled-fetch.test.mjs`
Expected: FAIL — the three new `assert.rejects` cases fail because `countTile` currently resolves to `0` for all of them.

- [ ] **Step 3: Write the implementation**

In `data/cameras/tiled-fetch.mjs`, replace `countTile` (lines 56-62) with:

```js
/** Cheap (<1s) count probe used to decide whether a tile needs splitting. */
export async function countTile(t, fetchImpl = fetch) {
  const bbox = `${t.s},${t.w},${t.n},${t.e}`;
  const query = `[out:json][timeout:60];(${tileSelector(t)});out count;`;
  const data = await queryOverpass(query, fetchImpl, { allowEmpty: true });

  if (overpassFailed(data)) {
    throw new Error(`Count probe (${bbox}) failed: ${data.remark}`);
  }

  // `out count;` always returns exactly one count element — a genuinely empty
  // bbox yields total "0", not an absent element (verified against
  // overpass-api.de 0.7.62.11, 2026-07-20). A missing element therefore means
  // the query failed, and dropping the tile would silently delete the region
  // from the work plan.
  const total = data.elements?.[0]?.tags?.total;
  if (total === undefined) {
    throw new Error(`Count probe (${bbox}) returned no count element — treating as a failed query`);
  }

  const parsed = Number(total);
  if (!Number.isFinite(parsed) || parsed < 0) {
    throw new Error(`Count probe (${bbox}) returned a non-numeric total ${JSON.stringify(total)}`);
  }

  return parsed;
}
```

Add `overpassFailed` to the existing import at `tiled-fetch.mjs:10`:

```js
import { queryOverpass, retryWithBackoff, tileIntegrityFailed, belowMinimum, addElementsToFeatures, overpassFailed } from './lib.mjs';
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test data/cameras/tiled-fetch.test.mjs`
Expected: PASS.

If `planLeafTiles` or `fetchAllCameras` tests now fail, it is because their mocks return `{ elements: [{ tags: { total: 'N' } }] }` without a `type` field — that is fine and still passes, since the implementation reads `elements[0].tags.total` and ignores `type`. A mock returning `{ elements: [] }` for a probe must be updated to a zero-count element.

- [ ] **Step 5: Commit**

```bash
git add data/cameras/tiled-fetch.mjs data/cameras/tiled-fetch.test.mjs
git commit -m "Fail closed when a count probe fails instead of dropping the tile

An Overpass query that times out server-side returns HTTP 200 with an
empty elements array and a remark. countTile read that as a count of 0
and planLeafTiles dropped the tile, so the region was never fetched and
the per-tile integrity check never ran on it."
```

---

### Task 3: Harden `fetchTileInto` and `fetchCountryArea`

**Files:**
- Modify: `data/cameras/tiled-fetch.mjs` — `fetchTileInto` (~line 104), `fetchCountryArea` (~line 130)
- Test: `data/cameras/tiled-fetch.test.mjs`

**Interfaces:**
- Consumes: `overpassFailed` from Task 1
- Produces: both functions now throw on a remark-bearing response. Signatures unchanged.

**Context:** This is what makes `MX_AREA_MIN_COUNT = 0` (`tiled-fetch.mjs:30`) safe. Today "0 because Mexico is quiet" and "0 because the query died" are indistinguishable, and an empty MX response silently leaks Mexican border cameras into the US dataset — counts go *up*, so no floor fires.

- [ ] **Step 1: Write the failing tests**

Append inside the existing `describe('fetchCountryArea', ...)` block:

```js
  it('throws on a failure remark even when minCount is 0 (MX subtract-only)', async () => {
    const fetchImpl = async () =>
      jsonResponse({ elements: [], remark: 'runtime error: Query timed out in "area" at line 1' });
    await assert.rejects(() => fetchCountryArea('MX', 0, fetchImpl), /area query failed/i);
  });

  it('still returns an empty map for a genuinely empty area when minCount is 0', async () => {
    const fetchImpl = async () => jsonResponse({ elements: [] });
    const map = await fetchCountryArea('MX', 0, fetchImpl);
    assert.equal(map.size, 0);
  });
```

And a new describe block for `fetchTileInto`:

```js
describe('fetchTileInto failure discrimination', () => {
  it('throws on a failure remark rather than reporting an integrity mismatch', async () => {
    const fetchImpl = async () =>
      jsonResponse({ elements: [], remark: 'runtime error: Query timed out in "query" at line 1' });
    const map = new Map();
    await assert.rejects(
      () => fetchTileInto({ s: 0, w: 0, n: 1, e: 1, probed: 100 }, map, fetchImpl),
      /tile query failed/i
    );
    assert.equal(map.size, 0);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test data/cameras/tiled-fetch.test.mjs`
Expected: FAIL — the MX case currently resolves to an empty map; the `fetchTileInto` case currently throws the integrity-check message, not `tile query failed`.

- [ ] **Step 3: Write the implementation**

In `fetchTileInto`, immediately after the `queryOverpass` call and **before** `addElementsToFeatures`:

```js
  if (overpassFailed(data)) {
    throw new Error(`Tile query failed (${t.s},${t.w},${t.n},${t.e}): ${data.remark}`);
  }

```

In `fetchCountryArea`, immediately after the `queryOverpass` call and **before** `addElementsToFeatures`:

```js
  if (overpassFailed(data)) {
    throw new Error(`Area query failed (${iso}): ${data.remark}`);
  }

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test data/cameras/*.test.mjs`
Expected: PASS.

- [ ] **Step 5: Update the stale comment on `MX_AREA_MIN_COUNT`**

Replace the comment at `tiled-fetch.mjs:29-30`:

```js
// MX is subtract-only and may legitimately be 0. That is only safe because
// overpassFailed() now distinguishes an empty response from a failed one — a
// failed MX query throws rather than silently skipping the subtraction, which
// would leak Mexican border cameras into the US dataset with the count going up.
const MX_AREA_MIN_COUNT = 0;
```

- [ ] **Step 6: Commit**

```bash
git add data/cameras/tiled-fetch.mjs data/cameras/tiled-fetch.test.mjs
git commit -m "Reject remark-bearing responses in tile and area queries"
```

---

### Task 4: Record national totals in `meta.json`

**Files:**
- Modify: `data/cameras/tiled-fetch.mjs` — `fetchAllCameras` return (~line 228)
- Modify: `data/cameras/fetch.mjs:28,35-52`
- Test: `data/cameras/tiled-fetch.test.mjs`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `fetchAllCameras()` now resolves to `{ us, ca, rawTotal }` where `rawTotal: number` is the merged pre-subtraction tile total.
  - `meta.json` gains a top-level `totals` key: `{ us: number, ca: number, rawTotal: number }`. Task 6's CLI reads this.

**Context:** `rawTotal` is already computed at `tiled-fetch.mjs:187` and discarded. `upload.sh:20-22` iterates *files* matching `cameras-*-hourly.geojson` and looks up `meta[name].featureCount`, so adding a sibling `totals` key is safe and needs no change to `upload.sh`.

- [ ] **Step 1: Write the failing test**

Append inside the existing `describe('fetchAllCameras', ...)` block:

```js
  it('returns the pre-subtraction raw total alongside the datasets', async () => {
    const fetchImpl = async (endpoint, init) => {
      const query = init.body.get('data');
      if (query.includes('out count;')) {
        return jsonResponse({ elements: [{ type: 'count', tags: { total: '60000' } }] });
      }
      if (query.includes('area["ISO3166-1"="CA"]')) {
        return jsonResponse({ elements: [alprNode(9_000_001)] });
      }
      if (query.includes('area["ISO3166-1"="MX"]')) {
        return jsonResponse({ elements: [] });
      }
      // Tile fetch: return exactly the probed count so the integrity check passes.
      return jsonResponse({ elements: Array.from({ length: 60_000 }, (_, i) => alprNode(i + 1)) });
    };

    const { us, ca, rawTotal } = await fetchAllCameras(fetchImpl);
    assert.equal(typeof rawTotal, 'number');
    assert.equal(rawTotal, 60_000);
    // rawTotal is pre-subtraction, so it is >= the published US count.
    assert.ok(rawTotal >= us.features.length);
    assert.equal(ca.features.length, 1);
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test data/cameras/tiled-fetch.test.mjs`
Expected: FAIL — `AssertionError: 'undefined' == 'number'`

- [ ] **Step 3: Implement the return change**

In `data/cameras/tiled-fetch.mjs`, change the final return of `fetchAllCameras` from `return { us, ca };` to:

```js
  return { us, ca, rawTotal };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test data/cameras/tiled-fetch.test.mjs`
Expected: PASS.

- [ ] **Step 5: Record totals in `meta.json`**

In `data/cameras/fetch.mjs`, change line 28 from `const { us, ca } = await fetchAllCameras();` to:

```js
  const { us, ca, rawTotal } = await fetchAllCameras();
```

Then, after the `for` loop and **before** the `writeFile` of `meta.json` (between lines 49 and 51), insert:

```js
  // National totals for the count baseline (data/cameras/baseline.mjs). Kept as
  // a sibling of the per-object metadata; upload.sh iterates files, not meta
  // keys, so this extra key is inert to it.
  meta.totals = {
    us: us.features.length,
    ca: ca.features.length,
    rawTotal,
  };

```

- [ ] **Step 6: Verify the meta shape by hand**

Run:
```bash
node -e '
const m = {};
m["cameras-us-hourly"] = { featureCount: 1, lastUpdated: "x", source: "overpass" };
m.totals = { us: 1, ca: 2, rawTotal: 3 };
console.log(JSON.stringify(m));
' | jq -r '."cameras-us-hourly".featureCount, .totals.rawTotal'
```
Expected output:
```
1
3
```
This confirms the `jq` expression `upload.sh:22` uses is unaffected by the new key.

- [ ] **Step 7: Commit**

```bash
git add data/cameras/tiled-fetch.mjs data/cameras/fetch.mjs data/cameras/tiled-fetch.test.mjs
git commit -m "Record national totals in meta.json for the count baseline"
```

---

### Task 5: `baseline.mjs` pure core

**Files:**
- Create: `data/cameras/baseline.mjs`
- Create: `data/cameras/baseline.test.mjs`

**Interfaces:**
- Consumes: nothing
- Produces, all used by Task 6:
  - `DEFAULT_CONFIG: { fields: string[], window: number, minSamples: number, floorRatio: number, historyCap: number }`
  - `parseHistory(text: string) => object[]`
  - `serializeHistory(entries: object[]) => string`
  - `median(values: number[]) => number | null`
  - `baselineFor(entries, field, window) => { baseline: number | null, samples: number }`
  - `evaluate(observed: object, entries: object[], config) => { status: 'accepted' | 'rejected', checks: Check[] }` where `Check = { field, observed, baseline, samples, floor, verdict }` and `verdict` is `'ok' | 'below-floor' | 'observing'`
  - `appendCapped(entries, entry, cap) => object[]`

- [ ] **Step 1: Write the failing tests**

Create `data/cameras/baseline.test.mjs`:

```js
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_CONFIG,
  parseHistory,
  serializeHistory,
  median,
  baselineFor,
  evaluate,
  appendCapped,
} from './baseline.mjs';

/** Build `n` accepted history entries all holding the same value for every field. */
const accepted = (n, value) =>
  Array.from({ length: n }, (_, i) => ({
    ts: `2026-07-${String(i + 1).padStart(2, '0')}T00:00:00Z`,
    us: value,
    ca: value,
    rawTotal: value,
    status: 'accepted',
  }));

describe('median', () => {
  it('returns null for no values', () => {
    assert.equal(median([]), null);
  });

  it('returns the middle value for an odd count', () => {
    assert.equal(median([3, 1, 2]), 2);
  });

  it('averages the two middle values for an even count', () => {
    assert.equal(median([1, 2, 3, 4]), 2.5);
  });

  it('does not mutate its input', () => {
    const values = [3, 1, 2];
    median(values);
    assert.deepEqual(values, [3, 1, 2]);
  });
});

describe('parseHistory', () => {
  it('parses newline-delimited JSON and tolerates a trailing newline', () => {
    const entries = parseHistory('{"us":1}\n{"us":2}\n');
    assert.equal(entries.length, 2);
    assert.equal(entries[1].us, 2);
  });

  it('returns an empty array for empty, whitespace, or nullish input', () => {
    assert.deepEqual(parseHistory(''), []);
    assert.deepEqual(parseHistory('  \n \n'), []);
    assert.deepEqual(parseHistory(undefined), []);
  });

  it('skips malformed lines rather than throwing', () => {
    const entries = parseHistory('{"us":1}\nNOT JSON\n{"us":3}\n');
    assert.equal(entries.length, 2);
    assert.deepEqual(entries.map((e) => e.us), [1, 3]);
  });

  it('round-trips through serializeHistory', () => {
    const entries = [{ us: 1, status: 'accepted' }, { us: 2, status: 'rejected' }];
    assert.deepEqual(parseHistory(serializeHistory(entries)), entries);
  });
});

describe('baselineFor', () => {
  it('reports zero samples for empty history', () => {
    assert.deepEqual(baselineFor([], 'us', 24), { baseline: null, samples: 0 });
  });

  it('ignores rejected entries', () => {
    const entries = [
      ...accepted(3, 100),
      { ts: 'x', us: 1, ca: 1, rawTotal: 1, status: 'rejected' },
    ];
    assert.deepEqual(baselineFor(entries, 'us', 24), { baseline: 100, samples: 3 });
  });

  it('uses only the most recent `window` accepted values', () => {
    const entries = [...accepted(5, 100), ...accepted(3, 200)];
    assert.deepEqual(baselineFor(entries, 'us', 3), { baseline: 200, samples: 3 });
  });

  it('ignores entries missing the field or holding a non-finite value', () => {
    const entries = [
      { ts: 'a', us: 100, status: 'accepted' },
      { ts: 'b', status: 'accepted' },
      { ts: 'c', us: null, status: 'accepted' },
      { ts: 'd', us: 'oops', status: 'accepted' },
    ];
    assert.deepEqual(baselineFor(entries, 'us', 24), { baseline: 100, samples: 1 });
  });
});

describe('evaluate', () => {
  const config = { ...DEFAULT_CONFIG, fields: ['us'], minSamples: 6, window: 24, floorRatio: 0.95 };

  it('is observe-only below minSamples, however far off the value is', () => {
    const result = evaluate({ us: 1 }, accepted(5, 100_000), config);
    assert.equal(result.status, 'accepted');
    assert.equal(result.checks[0].verdict, 'observing');
  });

  it('accepts a value exactly at the floor', () => {
    const result = evaluate({ us: 95_000 }, accepted(6, 100_000), config);
    assert.equal(result.status, 'accepted');
    assert.equal(result.checks[0].verdict, 'ok');
    assert.equal(result.checks[0].floor, 95_000);
  });

  it('rejects a value one below the floor', () => {
    const result = evaluate({ us: 94_999 }, accepted(6, 100_000), config);
    assert.equal(result.status, 'rejected');
    assert.equal(result.checks[0].verdict, 'below-floor');
  });

  it('accepts a value above baseline (growth is never blocked)', () => {
    const result = evaluate({ us: 200_000 }, accepted(6, 100_000), config);
    assert.equal(result.status, 'accepted');
  });

  it('rejects when any one of several fields is below its floor', () => {
    const multi = { ...config, fields: ['us', 'ca', 'rawTotal'] };
    const result = evaluate({ us: 100_000, ca: 1, rawTotal: 100_000 }, accepted(6, 100_000), multi);
    assert.equal(result.status, 'rejected');
    assert.deepEqual(
      result.checks.map((c) => c.verdict),
      ['ok', 'below-floor', 'ok']
    );
  });

  it('is observe-only for a field with no history, even when others have history', () => {
    const multi = { ...config, fields: ['us', 'newField'] };
    const result = evaluate({ us: 100_000, newField: 5 }, accepted(6, 100_000), multi);
    assert.equal(result.status, 'accepted');
    assert.equal(result.checks[1].verdict, 'observing');
  });

  it('resists poisoning: a run of rejected entries does not move the baseline', () => {
    const poisoned = [
      ...accepted(6, 100_000),
      ...Array.from({ length: 20 }, () => ({ ts: 'bad', us: 1_000, status: 'rejected' })),
    ];
    const result = evaluate({ us: 94_999 }, poisoned, config);
    assert.equal(result.status, 'rejected', 'rejected entries must never become the new normal');
    assert.equal(result.checks[0].baseline, 100_000);
  });

  it('treats a missing observed value as a rejection, not a pass', () => {
    const result = evaluate({}, accepted(6, 100_000), config);
    assert.equal(result.status, 'rejected');
  });
});

describe('appendCapped', () => {
  it('appends within the cap', () => {
    assert.deepEqual(appendCapped([{ a: 1 }], { a: 2 }, 5), [{ a: 1 }, { a: 2 }]);
  });

  it('drops the oldest entries beyond the cap', () => {
    const entries = Array.from({ length: 5 }, (_, i) => ({ a: i }));
    const out = appendCapped(entries, { a: 99 }, 3);
    assert.deepEqual(out, [{ a: 3 }, { a: 4 }, { a: 99 }]);
  });

  it('does not mutate its input', () => {
    const entries = [{ a: 1 }];
    appendCapped(entries, { a: 2 }, 5);
    assert.equal(entries.length, 1);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test data/cameras/baseline.test.mjs`
Expected: FAIL — `Cannot find module '.../data/cameras/baseline.mjs'`

- [ ] **Step 3: Write the implementation**

Create `data/cameras/baseline.mjs`:

```js
#!/usr/bin/env node
// National count baseline for the hourly camera pipeline.
//
// Compares this run's national feature counts against the median of recent
// ACCEPTED runs and blocks the upload when a count falls below the floor. The
// absolute floors in fetch.mjs stay as a first line of defence; this catches
// the case they cannot — a dataset that is plausible in absolute terms but far
// below what the previous runs actually produced.
//
// Usage:
//   node data/cameras/baseline.mjs --meta <meta.json> --history <history.jsonl> [--report <out.md>] [--run-id <id>]
//
// Exits 0 when the run is accepted, 1 when it is rejected. The history file is
// rewritten with the new entry in BOTH cases, so a rejected run is recorded.

export const DEFAULT_CONFIG = {
  fields: ['us', 'ca', 'rawTotal'],
  window: 24, // accepted runs considered for the median (~1 day at hourly cadence)
  minSamples: 6, // observe-only until this many accepted samples exist for a field
  floorRatio: 0.95, // block below 95% of the median
  historyCap: 720, // ~30 days of hourly entries
};

/** Parse newline-delimited JSON, skipping malformed lines rather than throwing. */
export function parseHistory(text) {
  const entries = [];
  for (const line of String(text ?? '').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === 'object') entries.push(parsed);
    } catch {
      // A truncated or corrupt line must not take the pipeline down. Losing a
      // sample degrades the baseline toward observe-only, which is safe.
      console.warn(`Skipping malformed history line: ${trimmed.slice(0, 120)}`);
    }
  }
  return entries;
}

export function serializeHistory(entries) {
  return entries.map((e) => JSON.stringify(e)).join('\n') + '\n';
}

export function median(values) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

/**
 * Median of the last `window` ACCEPTED values for `field`. Accepted-only is what
 * stops a bad run from becoming the new normal: without it, repeated failures
 * would walk the baseline down until the guard accepts anything.
 */
export function baselineFor(entries, field, window) {
  const values = entries
    .filter((e) => e?.status === 'accepted')
    .map((e) => e?.[field])
    .filter((v) => typeof v === 'number' && Number.isFinite(v));
  const recent = values.slice(-window);
  return { baseline: median(recent), samples: recent.length };
}

export function evaluate(observed, entries, config = DEFAULT_CONFIG) {
  const checks = [];

  for (const field of config.fields) {
    const value = observed?.[field];
    const { baseline, samples } = baselineFor(entries, field, config.window);

    if (typeof value !== 'number' || !Number.isFinite(value)) {
      checks.push({ field, observed: value ?? null, baseline, samples, floor: null, verdict: 'below-floor' });
      continue;
    }

    if (samples < config.minSamples) {
      checks.push({ field, observed: value, baseline, samples, floor: null, verdict: 'observing' });
      continue;
    }

    const floor = baseline * config.floorRatio;
    checks.push({
      field,
      observed: value,
      baseline,
      samples,
      floor,
      verdict: value < floor ? 'below-floor' : 'ok',
    });
  }

  const status = checks.some((c) => c.verdict === 'below-floor') ? 'rejected' : 'accepted';
  return { status, checks };
}

export function appendCapped(entries, entry, cap) {
  return [...entries, entry].slice(-cap);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test data/cameras/baseline.test.mjs`
Expected: PASS, 24 tests.

- [ ] **Step 5: Commit**

```bash
git add data/cameras/baseline.mjs data/cameras/baseline.test.mjs
git commit -m "Add national count baseline core (median over accepted runs)"
```

---

### Task 6: `baseline.mjs` CLI and report

**Files:**
- Modify: `data/cameras/baseline.mjs` (append CLI section)
- Modify: `data/cameras/baseline.test.mjs` (add `formatReport` and `parseArgs` tests)

**Interfaces:**
- Consumes: everything from Task 5
- Produces:
  - `parseArgs(argv: string[]) => { meta, history, report, runId }` — throws on an unknown flag or a missing required value
  - `formatReport(result, runId) => string` — Markdown for the GitHub issue body (observed values are already carried on `result.checks`)
  - CLI behavior: exit 0 accepted / 1 rejected; history file rewritten in both cases

- [ ] **Step 1: Write the failing tests**

Append to `data/cameras/baseline.test.mjs` (add `formatReport` and `parseArgs` to the import):

```js
describe('parseArgs', () => {
  it('parses the required flags', () => {
    const args = parseArgs(['--meta', '/tmp/meta.json', '--history', '/tmp/h.jsonl']);
    assert.equal(args.meta, '/tmp/meta.json');
    assert.equal(args.history, '/tmp/h.jsonl');
    assert.equal(args.report, null);
    assert.equal(args.runId, null);
  });

  it('parses the optional flags', () => {
    const args = parseArgs([
      '--meta', 'm', '--history', 'h', '--report', 'r.md', '--run-id', '123',
    ]);
    assert.equal(args.report, 'r.md');
    assert.equal(args.runId, '123');
  });

  it('rejects an unknown flag rather than ignoring it', () => {
    assert.throws(() => parseArgs(['--meta', 'm', '--history', 'h', '--oops']), /unknown argument/i);
  });

  it('rejects a missing required flag', () => {
    assert.throws(() => parseArgs(['--meta', 'm']), /--history/);
  });

  it('rejects a flag with no value', () => {
    assert.throws(() => parseArgs(['--meta', '--history', 'h']), /--meta/);
  });
});

describe('formatReport', () => {
  it('names every below-floor field and includes the numbers', () => {
    const result = {
      status: 'rejected',
      checks: [
        { field: 'us', observed: 94_000, baseline: 100_000, samples: 24, floor: 95_000, verdict: 'below-floor' },
        { field: 'ca', observed: 540, baseline: 538, samples: 24, floor: 511.1, verdict: 'ok' },
        { field: 'rawTotal', observed: 95_000, baseline: 100_000, samples: 24, floor: 95_000, verdict: 'ok' },
      ],
    };
    const md = formatReport(result, '29721490530');
    assert.match(md, /\bus\b/);
    assert.match(md, /94,?000/);
    assert.match(md, /100,?000/);
    assert.match(md, /29721490530/);
    assert.match(md, /below-floor/);
  });

  it('renders an observing check without inventing a floor', () => {
    const result = {
      status: 'accepted',
      checks: [{ field: 'us', observed: 100, baseline: null, samples: 2, floor: null, verdict: 'observing' }],
    };
    const md = formatReport(result, null);
    assert.match(md, /observing/);
    assert.doesNotMatch(md, /NaN|null%/);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test data/cameras/baseline.test.mjs`
Expected: FAIL — no export named `parseArgs`.

- [ ] **Step 3: Write the implementation**

Append to `data/cameras/baseline.mjs`:

```js
const FLAGS = { '--meta': 'meta', '--history': 'history', '--report': 'report', '--run-id': 'runId' };

export function parseArgs(argv) {
  const out = { meta: null, history: null, report: null, runId: null };

  for (let i = 0; i < argv.length; i++) {
    const key = FLAGS[argv[i]];
    if (!key) throw new Error(`Unknown argument: ${argv[i]}`);
    const value = argv[i + 1];
    if (value === undefined || FLAGS[value]) throw new Error(`Missing value for ${argv[i]}`);
    out[key] = value;
    i++;
  }

  for (const required of ['meta', 'history']) {
    if (!out[required]) throw new Error(`Missing required argument: --${required}`);
  }

  return out;
}

const fmt = (n) => (typeof n === 'number' && Number.isFinite(n) ? Math.round(n).toLocaleString('en-US') : '—');

export function formatReport(result, runId) {
  const lines = [
    `**Status:** ${result.status}`,
    '',
    '| field | observed | baseline (median) | floor | samples | verdict |',
    '|---|---|---|---|---|---|',
  ];

  for (const c of result.checks) {
    lines.push(
      `| \`${c.field}\` | ${fmt(c.observed)} | ${fmt(c.baseline)} | ${fmt(c.floor)} | ${c.samples} | ${c.verdict} |`
    );
  }

  lines.push('');
  if (result.status === 'rejected') {
    lines.push(
      'The upload was **blocked**. The previously published data is still serving.',
      '',
      'Either the upstream fetch lost data, or the dataset genuinely changed and the',
      'baseline needs to catch up. Check the run log for Overpass remarks first.'
    );
  }
  if (runId) {
    lines.push('', `Run: https://github.com/${process.env.GITHUB_REPOSITORY ?? 'flockhopper3/deflock-data'}/actions/runs/${runId}`);
  }

  return lines.join('\n');
}

async function main() {
  const { readFile, writeFile } = await import('node:fs/promises');
  const args = parseArgs(process.argv.slice(2));

  const meta = JSON.parse(await readFile(args.meta, 'utf8'));
  const observed = meta.totals;
  if (!observed) throw new Error(`${args.meta} has no "totals" key — is fetch.mjs up to date?`);

  let historyText = '';
  try {
    historyText = await readFile(args.history, 'utf8');
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
    console.log('No history file yet — first run, observe-only.');
  }

  const entries = parseHistory(historyText);
  const result = evaluate(observed, entries, DEFAULT_CONFIG);

  for (const c of result.checks) {
    console.log(
      `${c.field}: observed=${fmt(c.observed)} baseline=${fmt(c.baseline)} floor=${fmt(c.floor)} samples=${c.samples} -> ${c.verdict}`
    );
  }

  const entry = {
    ts: new Date().toISOString(),
    ...observed,
    status: result.status,
    ...(args.runId ? { runId: args.runId } : {}),
  };
  // Written on BOTH paths so a rejected run is recorded for forensics. It is
  // excluded from future baselines by its status, not by its absence.
  await writeFile(args.history, serializeHistory(appendCapped(entries, entry, DEFAULT_CONFIG.historyCap)));

  const report = formatReport(result, args.runId);
  if (args.report) await writeFile(args.report, report);

  if (result.status === 'rejected') {
    console.error('\nBaseline check FAILED — refusing to publish.\n');
    console.error(report);
    process.exit(1);
  }

  console.log('\nBaseline check passed.');
}

// Only run as a CLI, never on import (the tests import this module).
if (process.argv[1] && process.argv[1].endsWith('baseline.mjs')) {
  main().catch((err) => {
    console.error(err.message ?? err);
    process.exit(1);
  });
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test data/cameras/baseline.test.mjs`
Expected: PASS.

- [ ] **Step 5: Smoke-test the CLI end to end**

```bash
cd /tmp && rm -f bl-meta.json bl-hist.jsonl
printf '{"totals":{"us":100000,"ca":500,"rawTotal":101000}}' > bl-meta.json
node ~/Documents/Developer/FLOCK/Data/data/cameras/baseline.mjs --meta bl-meta.json --history bl-hist.jsonl
echo "exit=$?"
```
Expected: three `-> observing` lines, `Baseline check passed.`, `exit=0`, and `bl-hist.jsonl` containing one accepted entry.

Now build up history and prove a drop is blocked:
```bash
cd /tmp && for i in 1 2 3 4 5; do node ~/Documents/Developer/FLOCK/Data/data/cameras/baseline.mjs --meta bl-meta.json --history bl-hist.jsonl >/dev/null; done
printf '{"totals":{"us":80000,"ca":500,"rawTotal":101000}}' > bl-meta.json
node ~/Documents/Developer/FLOCK/Data/data/cameras/baseline.mjs --meta bl-meta.json --history bl-hist.jsonl
echo "exit=$?"
```
Expected: `us: ... -> below-floor`, `Baseline check FAILED`, `exit=1`, and a final `"status":"rejected"` line appended to `bl-hist.jsonl`.

Confirm the rejected entry did not poison the baseline:
```bash
cd /tmp && tail -1 bl-hist.jsonl | jq -r .status
```
Expected: `rejected`

- [ ] **Step 6: Commit**

```bash
git add data/cameras/baseline.mjs data/cameras/baseline.test.mjs
git commit -m "Add baseline CLI: gate the upload and emit a Markdown report"
```

---

### Task 7: Wire the gate into `fetch-data.yml`

**Files:**
- Modify: `.github/workflows/fetch-data.yml`

**Interfaces:**
- Consumes: `data/cameras/baseline.mjs` CLI from Task 6; `meta.totals` from Task 4
- Produces: an hourly job that blocks `upload.sh` on a failed baseline check and files a GitHub issue

**Context:** `permissions` is absent from this workflow today, and the repo's `default_workflow_permissions` is `read`, so `issues: write` must be granted explicitly. Granting `permissions` also drops every other scope to `none`, which is what we want.

- [ ] **Step 1: Add the permissions block**

In `.github/workflows/fetch-data.yml`, after the `concurrency` block and before `jobs:`:

```yaml
permissions:
  contents: read
  issues: write # baseline blocks file/refresh an issue; default repo perms are read-only
```

- [ ] **Step 2: Add the history download step**

Insert **after** the `Configure AWS CLI for R2` step and **before** `Upload to R2`:

```yaml
      # Baseline history lives beside the data it describes. A missing object is
      # the first-run case, not an error — baseline.mjs treats empty history as
      # observe-only.
      - name: Download count history
        env:
          R2_DATA_BUCKET: deflock-data
          R2_ENDPOINT: ${{ secrets.R2_ENDPOINT }}
        run: |
          set -euo pipefail
          : > /tmp/counts-history.jsonl
          aws s3 cp "s3://${R2_DATA_BUCKET}/pipeline/counts-history.jsonl" /tmp/counts-history.jsonl \
            --endpoint-url "${R2_ENDPOINT}" || echo "No existing history — first run."
          wc -l < /tmp/counts-history.jsonl
```

- [ ] **Step 3: Add the gate step**

Immediately after the download step:

```yaml
      - name: Check national counts against baseline
        id: baseline
        run: |
          node data/cameras/baseline.mjs \
            --meta /tmp/data-out/meta.json \
            --history /tmp/counts-history.jsonl \
            --report /tmp/baseline-report.md \
            --run-id "${{ github.run_id }}"
```

The existing `Upload to R2` step follows unchanged. Because steps run sequentially and this one exits non-zero on rejection, the upload is skipped automatically — no `if:` needed on it.

- [ ] **Step 4: Add the history upload and the issue steps**

Append **after** the existing `Upload to R2` step:

```yaml
      # Runs even when the gate blocked, so a rejected run is recorded. Skipped
      # only when the gate never ran (e.g. the fetch itself failed).
      - name: Upload count history
        if: always() && steps.baseline.outcome != 'skipped'
        env:
          R2_DATA_BUCKET: deflock-data
          R2_ENDPOINT: ${{ secrets.R2_ENDPOINT }}
        run: |
          set -euo pipefail
          aws s3 cp /tmp/counts-history.jsonl "s3://${R2_DATA_BUCKET}/pipeline/counts-history.jsonl" \
            --endpoint-url "${R2_ENDPOINT}" \
            --content-type "application/x-ndjson" \
            --cache-control "no-store"

      - name: File or update the baseline issue
        if: failure() && steps.baseline.outcome == 'failure'
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          TITLE="Camera pipeline blocked: national count below baseline"
          BODY_FILE=/tmp/baseline-report.md
          [ -f "${BODY_FILE}" ] || echo "Baseline check failed before producing a report." > "${BODY_FILE}"

          EXISTING="$(gh issue list --state open --search "${TITLE} in:title" --json number --jq '.[0].number // empty')"
          if [ -n "${EXISTING}" ]; then
            gh issue comment "${EXISTING}" --body-file "${BODY_FILE}"
            echo "Commented on issue #${EXISTING}"
          else
            gh issue create --title "${TITLE}" --body-file "${BODY_FILE}"
          fi
```

- [ ] **Step 5: Validate the workflow YAML parses**

Run:
```bash
node -e '
const fs = require("fs");
const t = fs.readFileSync(".github/workflows/fetch-data.yml", "utf8");
for (const k of ["permissions:", "id: baseline", "Download count history", "Upload count history", "File or update the baseline issue"]) {
  if (!t.includes(k)) { console.error("MISSING: " + k); process.exit(1); }
}
console.log("all expected keys present");
'
```
Expected: `all expected keys present`

Then confirm step ordering is correct — the gate must sit between `Configure AWS CLI` and `Upload to R2`:
```bash
grep -n "name: \|- name: " .github/workflows/fetch-data.yml | grep -E "Configure AWS|baseline|Upload to R2|Download count"
```
Expected order: `Configure AWS CLI for R2`, `Download count history`, `Check national counts against baseline`, `Upload to R2`, `Upload count history`, `File or update the baseline issue`.

- [ ] **Step 6: Run the whole suite one final time**

Run: `node --test data/cameras/*.test.mjs`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/fetch-data.yml
git commit -m "Gate the hourly upload on the national count baseline

Blocks the upload when a national count falls below 95% of the median of
recent accepted runs, records every run to a JSONL history in R2, and
files a GitHub issue on a block. Observe-only until 6 samples exist."
```

---

## Rollout

The first six hourly runs after merge are **observe-only** for every field — `minSamples: 6` is not met, so nothing can block. That is intentional: it builds history before enforcing, and it means a merge cannot immediately break the pipeline.

After roughly a day, check the accumulated history and revisit the thresholds against real churn:

```bash
aws s3 cp s3://deflock-data/pipeline/counts-history.jsonl - --endpoint-url "$R2_ENDPOINT" | jq -s '
  map(select(.status=="accepted")) |
  { runs: length,
    us:  { min: (map(.us)  | min), max: (map(.us)  | max) },
    ca:  { min: (map(.ca)  | min), max: (map(.ca)  | max) } }'
```

`ca` is the field most likely to need loosening: 5% of ~538 cameras is a 27-camera swing, which may sit inside normal churn for a dataset that small. If it produces false blocks, raise `ca`'s tolerance rather than weakening `floorRatio` globally — that would require making `floorRatio` per-field, a small change to `DEFAULT_CONFIG` and `evaluate`.

## Out of scope

- **Dead-man's-switch / freshness monitoring.** A blocked run files an issue; a *dropped cron run* files nothing, and measured cron reliability is ~35% drops with gaps up to 3h21m. Detecting "the pipeline stopped running" is separate work.
- Per-seed-tile or per-state history. The JSONL format can carry extra fields later without migration.
- Rollback/retention for published R2 objects.
- Deduplicating the floors that `tiles/cameras/build.sh:51-57` copies from `fetch.mjs`.
