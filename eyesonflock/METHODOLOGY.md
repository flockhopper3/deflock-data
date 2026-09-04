# Sharing Network — Methodology

## Research question

Which law-enforcement agencies declare data-sharing relationships with one another on the Flock Safety transparency portals, and what does the resulting national network look like geographically?

## Source

`<work>/raw/eyesonflock_full_data.json` — a snapshot of [EyesOnFlock](https://eyesonflock.com)'s scrape of Flock Safety transparency portals. Each entry in the `portals` array represents one agency that publishes a transparency dashboard, and each portal carries an `organizations_shared_with` list naming the other agencies it shares ALPR data with.

The snapshot is refreshed on every pipeline run by step 00 below, which GETs EyesOnFlock's public API. The "as of" of the resulting network is the time of the most recent successful step-00 fetch; `meta.json` records it as `generatedAt`.

`<work>` is the work directory: `eyesonflock/work/` by default, or `$EYESONFLOCK_WORK_DIR`. Every path below comes from `scripts/paths.py`.

## Pipeline

Run end-to-end with `python eyesonflock/scripts/run_pipeline.py` (add `--skip-fetch` to rebuild from an existing snapshot). Step 00 refreshes the raw snapshot and steps 01–04 always rebuild from it, so the pipeline is safe to run on a schedule. Step 02a retains its per-query Google Maps cache (`eyesonflock/google_geocode_cache.json`, committed) so no paid API calls are repeated. Step 06 refuses to let a broken build through.

### 0. `00_fetch_eyesonflock.py`

GETs `https://eyesonflock.com/api/v1/data` (public, no auth) and overwrites `<work>/raw/eyesonflock_full_data.json` with the response. Validates the payload (must be JSON with a non-empty `portals` list plus a `summary` key), refuses a snapshot with fewer than 500 portals (the 2026-04 reference had 908), refuses one with fewer than 50% of the prior snapshot's portal count when a prior exists (guards against a corrupt partial response nuking a good snapshot), and writes atomically via tempfile-plus-rename so a crash mid-download can't leave the pipeline reading a truncated file.

On every run, prints a one-line diff summary: portals added, removed, sharing-list changed, and metadata changed vs the prior snapshot. The existing snapshot is preserved untouched if anything in the fetch or validation path fails.

### Manual tool: `00_download_gazetteer.py`

Pulls three Census Bureau gazetteer files into `eyesonflock/gazetteer/`:

- `<vintage>_Gaz_place_national.txt` (cities/CDPs)
- `<vintage>_Gaz_counties_national.txt` (counties)
- `<vintage>_Gaz_state_national.txt` (states; the Census Bureau doesn't ship a state file directly — this script derives one)

These are the offline geocoder for step 02. **Not invoked by `run_pipeline.py`** — the gazetteer files are committed and refreshed manually about once a year when the Census Bureau publishes a new vintage: bump `GAZETTEER_VINTAGE` in `paths.py`, delete the old files, run this script, commit.

### 1. `01_parse_orgs.py`

Reads the raw snapshot and writes `<work>/intermediate/parsed_orgs.json`.

For each unique org name found in any portal's `organizations_shared_with` list, calls `parse_org_name()` (in `parse_orgs_lib.py`) to extract structured fields. The parser handles a long list of name patterns:

1. `[Federal] X` — federal agency (state defaulted to `DC` if not parsed)
2. `XX - Name` — state-prefixed
3. `Name -XX` / `Name (XX)` — state-suffixed
4. `City ST PD` / `City PD (ST)` — police department
5. `County ST SO` / `County SO (ST)` — sheriff's office
6. `X County Const ST Pct N` — Texas-style constable precincts (precinct is preserved in the city field so distinct precincts don't collapse)
7. `City of X ST` — city government
8. School/university/college (type=`school`, no city extracted)
9. Last-resort: scan for any 2-letter state code, with safeguards for ambiguous codes that double as English words (`IN`, `OR`, `OK`, `ME`, `HI`, `AL`, `PA`, `ID`)
10. Last-last-resort: full state-name scan in the text

Before pattern matching, administrative tags (`[Inactive]`, `(DNU)`, `- DO NOT USE`, `(Dead/Old)`, etc.) are stripped from the working name so that `Brooklyn Park MN PD [Inactive]` and `Brooklyn Park MN PD` parse identically.

**Canonical slugging.** The entry is keyed by a canonical slug derived from `(city, state, type)` via `canonical_slug()`, not by slugifying the raw name. Multiple raw forms that refer to the same agency — `"Allen Park MI PD"` / `"Allen Park PD MI"`, `"Coconino AZ SO"` / `"Coconino County AZ SO"`, any variant with a status tag — resolve to the same slug and merge into a single entry. Collapsed forms are recorded in each entry's `aliases` list. Unparseable names (no state, no recognized type) fall back to slugifying the tag-stripped raw name.

Each entry also carries `is_junk: bool`, set by `junk_filter.is_junk()` — True for obvious non-agency entries (`"DNU"`, `"265"`, `"Example PD"`, `"DO NOT USE"`, etc.). Junk entries still flow through the rest of the pipeline but are flagged so the frontend can hide them.

After parsing, each org is matched against the portals list via `portal_canonical_slug()` to flag `is_portal=True` and pull in portal-level metrics (cameras, searches, vehicles_captured, population, hotlist_hits, data_retention). Portals that aren't named in any sharing list are still added as standalone nodes.

### 2. `02_geocode_orgs.py`

Reads `parsed_orgs.json` and writes `<work>/intermediate/geocoded_orgs.json`. For each org, attaches `lat`, `lng`, and `geocode_method`.

The geocoder cascade in `geocode_lib.py`:

1. **`place`** — exact `(city.lower(), state)` match against the place gazetteer. Also tries `+ " city"`, `+ " town"`, `+ " village"`, `+ " CDP"` suffixes (Census place names usually carry one).
2. **`place_variant`** — `_city_variations()` tries: St./Saint swaps, append " city", strip Township/Borough/Village/Town suffix, strip "County" to retry as place, and for constable-precinct cities fall back to the parent county's centroid.
3. **`county`** — match against the county gazetteer (also indexed both with and without the trailing " County"). Constable-precinct cities are additionally routed to the county lookup here.
4. **`state`** — fall back to the state's centroid (from the derived state gazetteer).
5. **`default`** — Washington DC (`38.8816, -77.0910`).

Orgs flagged `is_junk=True` by step 01 short-circuit this cascade: they receive the state centroid (or the DC default if state is unknown) and are tagged `geocode_method="junk"` so the frontend can filter them out.

The script prints a method breakdown at the end so you can see how many orgs landed at the state-centroid fallback vs. resolved to a real place.

### 2a. `02a_google_geocode.py`

Upgrade pass. Reads `geocoded_orgs.json` and rewrites the same file in place. For every org currently at `geocode_method in {state, default}`, looks up a structured query in the Google Maps Geocoding cache (`eyesonflock/google_geocode_cache.json`) and — if a plausible result is available — upgrades the lat/lng and re-tags `geocode_method="google"`.

**Two modes.** With `GOOGLEMAPSAPI` set (environment or `eyesonflock/.env`), cache misses are sent to the Google API and the answers written back to the cache file. Without it, the step runs **cache-only**: cached results are still applied, misses are left at their state-centroid coordinates, no network call is made, and the cache file is not touched. This is how the scheduled GitHub run works unless a `GOOGLEMAPSAPI` repo secret is configured; the committed cache covers every candidate in the reference snapshot, so the two modes produced identical output there.

**Plausibility check.** Before accepting a Google result, the step verifies the returned coordinates fall inside the declared state's bounding box (shared with step 5 via `state_bbox.STATE_BBOX`). Results that fail the check are rejected, the offending cache entry is invalidated so the next paid run can retry, and the org stays at its prior state-centroid coordinates. This prevents Google's occasional garbage results (e.g., returning a generic Rocky-Mountain centroid for queries with no city) from poisoning the cache. Junk-tagged entries are never upgraded. Step 05 re-runs the bbox check on the final output as a belt-and-suspenders audit.

### 3. `03_build_nodes_geojson.py`

Reads the geocoded orgs, the raw snapshot, and `parsed_orgs.json`, and writes `<work>/output/sharing-network-nodes.geojson`. Each org becomes a `Point` feature with these properties:

`id, name, city, state, type, isPortal, portalSlug, isJunk, isInactive, isLikelyAggregator, cameras, searches, vehiclesCaptured, connectionCount, population, hotlistHits, geocodeMethod, aliases`

**`portalSlug`** is the original EyesOnFlock portal slug (e.g., `allen-park-mi`), preserved from the raw data so the frontend can build `eyesonflock.com/portal/<slug>` deep-links. Populated only when `isPortal=true`; `null` otherwise.

**`connectionCount`** is `|outbound ∪ inbound|` — the number of distinct neighbours in either direction. It is computed by resolving every sharing-list name through the **same** name→slug resolver step 04 uses (`parse_orgs_lib.make_shared_name_resolver`, seeded with the alias map from `parsed_orgs.json`), so the count always equals what a consumer would derive from the adjacency file. Before the two steps shared a resolver, names that only resolve via aliases (the bare `"Berkeley"` manual alias, rescued stateless sheriff's-office forms) counted in one file and not the other; 66 nodes disagreed. Step 06 now enforces the equality.

**`isLikelyAggregator`** uses a deliberate heuristic to flag aggregator/template accounts that would otherwise dominate centrality-based visualization: `connectionCount >= 500 AND population < 50_000`. Real metropolitan sheriffs (Shelby County TN SO, etc.) have high degree but large populations and are not flagged. Treat the flag as an alert, not a factual claim about organizational type. **`isInactive`** flags any node whose `raw_name` or any alias contains inactive/DNU/deactivated markers, even when the canonical slug collapsed it into an active portal's node. **`isJunk`** mirrors the `is_junk` flag from step 01.

### 4. `04_build_adjacency.py`

Reads the raw snapshot again (plus `parsed_orgs.json` for the alias map) and writes `<work>/output/sharing-network-adjacency.json`. For each portal, emits **outbound-only** edges from the portal node to every org in its sharing list — both ends resolved through the shared resolver so edges always terminate at the same nodes that step 03 emits. Self-edges introduced when an alias canonicalizes back to the portal's own slug are dropped. The output maps each slug to a sorted, deduplicated array of connected slugs.

### 5. `05_audit_geocoding.py`

QC report. Reads `geocoded_orgs.json` and writes `<work>/intermediate/geocode_audit.txt` with:

- Method-breakdown counts and percentages
- A list of orgs that fell to `state` or `default` (manual review candidates)
- Per-state bounding-box check: any geocoded result outside its declared state's approximate lat/lng box is flagged

The bounding boxes use generous (~1°) margins to avoid false positives. The report is informational; it never fails the run.

### 6. `06_verify_outputs.py`

The gate. Reads both output files and exits non-zero — failing the pipeline and the GitHub run — if any invariant is violated:

- `nodes.geojson` is a `FeatureCollection` with ≥ 5,000 features, every one a `Point` with two finite coordinates in range.
- Every feature carries exactly the 18 property keys above (the website contract), a non-empty unique `id`, and a `portalSlug` if and only if `isPortal`.
- ≥ 700 portal features. ≤ 2% of features at the DC `default` fallback (the parse-failure signal).
- `adjacency.json` is an object whose every key and target is a node `id`; lists are sorted, deduplicated, and free of self-edges; ≥ 100,000 directed edges.
- For every node, `connectionCount == |outbound ∪ inbound|` derived from the adjacency file.

On success it writes `<work>/output/meta.json` (`generatedAt`, `snapshotPortals`, `featureCount`, `portalCount`, `adjacencyKeys`, `directedEdges`, `geocodeMethods`, `junk`, `inactive`, `likelyAggregators`, `runId`).

## Auxiliary modules

These libraries are used by the numbered pipeline scripts above but are also safe to import and call directly:

- **`paths.py`** — every file location; the only module that reads `EYESONFLOCK_WORK_DIR`.
- **`parse_orgs_lib.py`** — exports `parse_org_name`, `slugify`, `canonical_slug`, `portal_canonical_slug`, `portal_as_org_name`, `_strip_status_tags`, plus `build_alias_map` / `make_shared_name_resolver` (the resolver shared by steps 03 and 04).
- **`geocode_lib.py`** — gazetteer lookup builders plus `geocode_org` for the Census cascade.
- **`junk_filter.py`** — `is_junk(raw_name)` returns True for obvious non-agency entries (numeric-only, very short, `"DNU"`, `"test"`, `"demo"`, `"do not use"` substrings). Called by `01_parse_orgs.py` to stamp the `is_junk` flag.
- **`google_geocode.py`** — the `GoogleGeocoder` client (on-disk JSON cache, 50-QPS rate limit, cache-only mode when constructed without a key) used by `02a_google_geocode.py`.
- **`state_bbox.py`** — approximate per-state bounding boxes for the plausibility checks in 02a and 05.

## Outputs

In `<work>/output/`:

- `sharing-network-nodes.geojson` — node layer with metadata
- `sharing-network-adjacency.json` — edge list keyed by slug
- `meta.json` — run summary

## Caveats

- **Self-reported relationships.** Sharing is what the transparency portal exposes. Agencies may share via channels not reflected here (e.g., direct DB peering, federal task forces).
- **Snapshot-bound.** The output reflects the EyesOnFlock scrape fetched by step 00 on that run; `meta.json` carries `generatedAt` and `snapshotPortals`.
- **Geocoding precision.** Census place matches resolve to the place's internal point (`INTPTLAT`/`INTPTLONG`), not the actual agency address. State-centroid fallback can produce visible clustering — `05_audit_geocoding.py` flags this.
- **Default fallback (DC) is suspicious.** Any node geocoded to (38.8816, -77.0910) is almost certainly a parse failure, not a real DC agency. Audit the `default`-method orgs; step 06 fails the run if they exceed 2% of features.
- **Edges are directional (outbound-only).** `adj[A] = [B]` means "A declares sharing outbound data to B" and only that. If B shares back, `adj[B]` independently contains A; if B doesn't (or B isn't a portal), only the one direction appears. Web-app consumers build the reverse index themselves in one O(E) pass at load. `connectionCount` on each node is `|outbound ∪ inbound|` — the total distinct neighbors in either direction.
- **Canonical slugging may over-merge.** Two genuinely distinct agencies with the same canonical `(city, state, type)` (e.g., two different "Jackson Township OH PD" in different counties) will currently merge into one node. The `aliases` list on each node exposes the raw forms so such merges can be caught in review. The `05_audit_geocoding.py` bbox check also flags same-slug geocodes that land inconsistently.
- **Many portals contribute no outgoing edges.** In the 2026-04 reference snapshot, 380 of 908 portals had an empty `organizations_shared_with` list (528 adjacency keys). These agencies still appear as nodes (portal metadata intact) but only receive edges from portals that reference them.
- **Aggregator detection is heuristic.** `isLikelyAggregator=True` means `connectionCount >= 500 AND population < 50_000` — good enough to flag Pittsboro-type aggregator accounts, but a genuinely high-degree small-town agency would also trip this flag. Treat as an alert, not a judgment.

## Run

```bash
python eyesonflock/scripts/run_pipeline.py
```

Total runtime: about a minute. Census cascade geocoding over all ~6.5k orgs dominates; parsing is fast. If `GOOGLEMAPSAPI` is set, step 02a adds a few seconds for any uncached candidates.
