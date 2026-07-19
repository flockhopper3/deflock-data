# Camera Positions Index (binary viewport-count artifact) — Design

**Date:** 2026-07-18
**Status:** Approved

## Goal

Emit a slim binary positions index alongside each hourly per-country tile archive, built from the same source snapshot in the same build run. The maps client (`deflockhopper_maps`, `camera-tiles-refactor` branch) fetches it once in the background and counts cameras in the viewport in-memory, instead of `queryRenderedFeatures` — which only sees loaded/rendered tiles, double-counts across tile boundaries, and deserializes ~112K MVT features per gesture on the main thread at national zoom.

Purely additive: the tile archives, filter tileset, and manifest are untouched.

## New outputs

| Artifact | R2 key / object | Served at |
|----------|-----------------|-----------|
| US positions index (binary) | `flockhopper-tiles:cameras-us-hourly-index.bin` | `https://tiles.dontgetflocked.com/cameras-us-hourly-index.bin` |
| US positions index (sidecar) | `flockhopper-tiles:cameras-us-hourly-index.json` | `https://tiles.dontgetflocked.com/cameras-us-hourly-index.json` |
| CA positions index (binary) | `flockhopper-tiles:cameras-ca-hourly-index.bin` | `https://tiles.dontgetflocked.com/cameras-ca-hourly-index.bin` |
| CA positions index (sidecar) | `flockhopper-tiles:cameras-ca-hourly-index.json` | `https://tiles.dontgetflocked.com/cameras-ca-hourly-index.json` |

Both are mirrored to `deflock-data` like the manifest. The `.bin` is uploaded gzipped (`Content-Encoding: gzip`, `application/octet-stream`); the `.json` gzipped as `application/json` — same upload pattern as `cameras-<cc>-hourly-manifest.json`.

## Binary format (v1)

Little-endian, columnar so the client views it as typed arrays with zero parsing. No delta encoding in v1.

**Header (16 bytes):**

| Bytes | Field | Value |
|-------|-------|-------|
| 0–3   | magic | `FHIX` (ASCII) |
| 4–7   | `uint32` version | `1` |
| 8–11  | `uint32` count | `N` (number of cameras) |
| 12–15 | `uint32` reserved | `0` |

**Columns (in order, no padding between):**

| Column | Type | Meaning |
|--------|------|---------|
| latitude  | `int32[N]` | `round(lat × 1e6)` microdegrees |
| longitude | `int32[N]` | `round(lng × 1e6)` microdegrees |
| brandId   | `uint8[N]` | brand id (see mapping below) |

Total file size = `16 + N×4 + N×4 + N×1` = `16 + 9N`.

Records are sorted by **latitude then longitude ascending** (helps gzip; the client relies on no order beyond this). Offsets are naturally aligned: `int32` columns start at 16 and `16 + 4N` (both 4-byte aligned), `uint8` at `16 + 8N` (no alignment constraint), so the client's typed-array views are valid with no padding.

**Endianness note:** the header is written/read little-endian explicitly (DataView). The columns are viewed as native-endian typed arrays, which is little-endian on every realistic target (x86/ARM). This is documented in the sidecar; a big-endian client would need to byte-swap, but none exists.

## Brand ID mapping (`uint8`, "reserve 255 = other")

The `brandId` column **is** the manifest's `b` filter code — the same brand ranking `enrich.mjs` already computes (0 = unknown/missing, 1..N by descending camera count, ties broken by label). Reused, not re-derived, so it is identical to the filter tileset/manifest by construction.

- `brandId 0` — unknown / missing brand.
- `brandId 1..254` — the manifest `b` code (brands ranked by camera count).
- `brandId 255` — `"other"`: **only materializes when the manifest has > 254 brands**, collapsing ranks 255+ into it. `uint8` caps at 255, and the manifest's brand list is unbounded (`normalizeBrand` passes unmatched strings through as their raw value, so long-tail junk each becomes a brand). Today US = 85 brands, CA = 10, so IDs stay dense and no `"other"` bucket appears.

This is the one place the index intentionally diverges from the manifest's unbounded IDs. The client maps by name via the sidecar `brands[]`, so the cap is transparent.

## JSON sidecar

```json
{
  "version": 1,
  "count": 117074,
  "build": "51c02d2fb717eeb1",
  "brands": ["unknown", "Flock Safety", "Motorola Solutions", "..."],
  "note": "brandId 0=unknown; 1..254 by camera count (== filter manifest 'b'); 255='other' only if >254 brands. Columns are little-endian native typed arrays."
}
```

- `brands[i]` is the display name for `brandId = i`; index `0` is reserved for unknown/missing. Dense array (`brands[id] = label`), appending `"other"` only in the overflow case.
- `build` = the manifest's `version` (the `sha256(input snapshot bytes).slice(0,16)`), computed from the identical input bytes so it matches `cameras-<cc>-hourly-manifest.json` exactly. Lets the client pair the index with the tile build it came from (exact-snapshot, stronger than the hour-level drift tolerance required).

## Data flow

```
Build Tiles GitHub Action (chained off each hourly fetch)
  └─ build.sh, per country, after the existing enrich.mjs + filter-tile step:
       positions-index.mjs cameras-<cc>-hourly.geojson  <expected_count>
         → cameras-<cc>-hourly-index.bin + cameras-<cc>-hourly-index.json
       gzip -9 the .bin; upload both to flockhopper-tiles (+ mirror deflock-data)
flockhopper-tiles Worker (tiles.dontgetflocked.com)
  └─ NEEDS a change to route *.bin (see component 4) — user deploys, separate repo
```

## Changes by component

### 1. `tiles/cameras/positions-index.mjs` (new, this repo)

Standalone script; CLI: `node positions-index.mjs <input.geojson> <bin-out> <json-out> <expected_count>`.

- Imports `enrichCollection` from `enrich.mjs` and runs it on the input → per-feature `b` code + `manifest.brands` + the input bytes for the version hash. (Runs `enrichCollection` a second time within the run; sub-second on 117K features and worth it to keep `enrich.mjs` untouched and the two artifacts consistent by shared code.)
- `version = sha256(inputBytes).slice(0,16)` — identical bytes as `enrich.mjs`, so identical value.
- Builds records `{ latµ° = round(lat*1e6), lngµ° = round(lng*1e6), brandId = b <= 254 ? b : 255 }` from `geometry.coordinates = [lng, lat]`.
- Sorts by lat then lng ascending; serializes the 16-byte header + three columns into one `Buffer`.
- Sidecar `brands = ["unknown", ...manifestBrands.map(b => b.label)]`, appending `"other"` iff any brand rank > 254.
- Runs the validation asserts (below), logs sizes, writes both files.

### 2. `tiles/cameras/positions-index.test.mjs` (new, this repo)

Node built-in test runner (CI already runs `tiles/cameras/*.test.mjs`). Round-trips a fixture through the writer + a reader mirroring the acceptance snippet:
- header magic/version/count; column offsets; `16 + 9N` size invariant.
- every lat ∈ `[-90e6, 90e6]`, lng ∈ `[-180e6, 180e6]`.
- sort order (lat then lng ascending).
- brand mapping: dense `brands[]`, `brandId == manifest b`, and the collapse at the 254/255 boundary (fixture with > 254 synthetic brands → rank 255+ become `brandId 255`, `brands[255] == "other"`).
- `count` mismatch vs expected → non-zero exit.

### 3. `tiles/cameras/build.sh` (this repo)

- After the existing `enrich.mjs` + `build_filter_tiles` + filter-verify block, per country: run `positions-index.mjs` on `${GEOJSON_FILE}` passing `${FEATURE_COUNT}` as expected count.
- `gzip -9 -c` the `.bin`; `aws s3 cp` both objects to `${R2_TILES_BUCKET}` with `--content-encoding gzip`, `.bin` `--content-type application/octet-stream`, `.json` `--content-type application/json`; mirror to `${R2_TILES_MIRROR_BUCKET}` when set (same as the manifest block).
- Confirm `positions-index.mjs`'s header count equals the script's `jq`-derived `FEATURE_COUNT` (the number fed to tippecanoe).
- Add the same steps to `--local` mode for preview/acceptance testing.
- **Bump `BUILD_CONFIG` → `v8-positions-index`** so countries with an unchanged source hash rebuild once to emit the index immediately, instead of waiting for the next data change.
- Tile / filter archives and their flags are untouched.

### 4. `flockhopper-tiles` Worker (repo: FLOCKHOPPER DATA RESEARCH — user deploys)

The Worker currently routes `*.pmtiles` (unpacked to `z/x/y`) and the manifest JSON; it does **not** serve `*.bin`. It needs a route that streams `cameras-*-index.bin` straight from R2 with the stored `Content-Encoding: gzip`, `Content-Type: application/octet-stream`, CORS `Access-Control-Allow-Origin: *`, and the same hourly-fresh + etag conditional-read policy as the manifest. This repo does R2 upload + validation only; the Worker edit is applied and deployed separately by the user (per the no-Cloudflare-deploys rule). A ready-to-paste snippet is provided at handoff.

## Validation (fail the build loudly)

`positions-index.mjs` asserts and exits non-zero on any failure:

1. Header `count` == sidecar `count` == `expected_count` passed in (== features fed to tippecanoe for that country in the same run).
2. Every `brandId < brands.length`.
3. File size == `16 + N×4 + N×4 + N×1`.
4. Logs final `.bin` and `.json` sizes (raw + gzipped).

`build.sh` additionally confirms the header count equals its own `jq '.features | length'` (`FEATURE_COUNT`). Any mismatch aborts the country build, so a bad index never uploads and the previous hour's index keeps serving (fail-closed).

## Acceptance check

This Node snippet must round-trip cleanly against the produced files (US and CA):

```js
const buf = new Uint8Array(await (await fetch(URL_BIN)).arrayBuffer()).buffer;
const dv = new DataView(buf);
const n = dv.getUint32(8, true);
const lat = new Int32Array(buf, 16, n);
const lng = new Int32Array(buf, 16 + n * 4, n);
const brand = new Uint8Array(buf, 16 + n * 8, n);
// every lat within [-90e6, 90e6], lng within [-180e6, 180e6];
// n === sidecar.count; a few known cameras appear at expected coords.
```

(`fetch().arrayBuffer()` transparently inflates the gzip content-encoding in both browsers and Node/undici, so the snippet sees the raw `16 + 9N` bytes.)

## Risks

- **Serving depends on the Worker change (component 4), in a separate repo the user deploys.** Until it ships, the `.bin` uploads to R2 but 404s at the edge. The upload and validation are independently verifiable via `aws s3` before the Worker lands.
- `uint8 brandId` ceiling: safe today (US 85 / CA 10 brands), and the "reserve 255 = other" collapse means the build never fails on brand count; the far tail (single-count junk brands) folds into `"other"` only past 254, losing nothing meaningful for viewport brand counts.
- Double-run of `enrichCollection` (once in `enrich.mjs`, once in `positions-index.mjs`) — accepted for isolation; sub-second cost.
- On any index-step failure the country build aborts before upload, so the tile/filter/manifest artifacts for that country also do not publish that hour (they share the per-country build). This matches existing per-country fail-closed behavior.
