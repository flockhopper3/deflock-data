# Sharing Network — Methodology

## Research question

Which law-enforcement agencies declare data-sharing relationships with one another on the Flock Safety transparency portals, and what does the resulting national network look like geographically?

## Source

`data/raw/eyesonflock_full_data.json` — a snapshot of [EyesOnFlock](https://eyesonflock.com)'s scrape of Flock Safety transparency portals. Each entry in the `portals` array represents one agency that publishes a transparency dashboard, and each portal carries an `organizations_shared_with` list naming the other agencies it shares ALPR data with.

The snapshot is refreshed on every pipeline run by step 00 below, which GETs EyesOnFlock's public API. The "as of" of the resulting network is the time of the most recent successful step-00 fetch.

## Pipeline

Run end-to-end with `python sharing-network/scripts/run_pipeline.py`. Step 00 refreshes the raw snapshot and steps 01–04 always rebuild from it, so the pipeline is safe to run on a schedule (e.g. biweekly cron). Step 02a retains its per-query Google Maps cache (`data/intermediate/google_geocode_cache.json`) so no paid API calls are repeated.

### 0. `00_fetch_eyesonflock.py`

GETs `https://eyesonflock.com/api/v1/data` (public, no auth) and overwrites `data/raw/eyesonflock_full_data.json` with the response. Validates the payload (must be JSON with a non-empty `portals` list plus a `summary` key), aborts if the new snapshot has fewer than 50% of the prior portal count (guards against a corrupt partial response nuking a good snapshot), and writes atomically via tempfile-plus-rename so a crash mid-download can't leave the pipeline reading a truncated file.

On every run, prints a one-line diff summary: portals added, removed, sharing-list changed, and metadata changed vs the prior snapshot. The existing snapshot is preserved untouched if anything in the fetch or validation path fails.

### Manual tool: `00_download_gazetteer.py`

Pulls three Census Bureau gazetteer files into `data/raw/`:

- `2023_Gaz_place_national.txt` (cities/CDPs)
- `2023_Gaz_counties_national.txt` (counties)
- `2023_Gaz_state_national.txt` (states; the Census Bureau doesn't ship a state file directly — this script derives one)

These are the offline geocoder for step 02. **Not invoked by `run_pipeline.py`** — the gazetteer files are committed to `data/raw/` and refreshed manually about once a year when the Census Bureau publishes a new vintage. Run `python sharing-network/scripts/00_download_gazetteer.py` by hand at that time.

### 1. `01_parse_orgs.py`

Reads `data/raw/eyesonflock_full_data.json` and writes `data/intermediate/parsed_orgs.json`.

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

Reads `data/intermediate/parsed_orgs.json` and writes `data/intermediate/geocoded_orgs.json`. For each org, attaches `lat`, `lng`, and `geocode_method`.

The geocoder cascade in `geocode_lib.py`:

1. **`place`** — exact `(city.lower(), state)` match against the place gazetteer. Also tries `+ " city"`, `+ " town"`, `+ " village"`, `+ " CDP"` suffixes (Census place names usually carry one).
2. **`place_variant`** — `_city_variations()` tries: St./Saint swaps, append " city", strip Township/Borough/Village/Town suffix, strip "County" to retry as place, and for constable-precinct cities fall back to the parent county's centroid.
3. **`county`** — match against the county gazetteer (also indexed both with and without the trailing " County"). Constable-precinct cities are additionally routed to the county lookup here.
4. **`state`** — fall back to the state's centroid (from the derived state gazetteer).
5. **`default`** — Washington DC (`38.8816, -77.0910`).

Orgs flagged `is_junk=True` by step 01 short-circuit this cascade: they receive the state centroid (or the DC default if state is unknown) and are tagged `geocode_method="junk"` so the frontend can filter them out.

The script prints a method breakdown at the end so you can see how many orgs landed at the state-centroid fallback vs. resolved to a real place.

### 2a. `02a_google_geocode.py`

Optional upgrade pass. Reads `data/intermediate/geocoded_orgs.json` and rewrites the same file in place. For every org currently at `geocode_method in {state, default}`, queries the Google Maps Geocoding API (cached to `data/intermediate/google_geocode_cache.json`) and — if Google returns a plausible result — upgrades the lat/lng and re-tags `geocode_method="google"`.

**Plausibility check.** Before accepting a Google result, the step verifies the returned coordinates fall inside the declared state's bounding box (shared with step 5 via `state_bbox.STATE_BBOX`). Results that fail the check are rejected, the offending cache entry is invalidated so the next paid run can retry, and the org stays at its prior state-centroid coordinates. This prevents Google's occasional garbage results (e.g., returning a generic Rocky-Mountain centroid for queries with no city) from poisoning the cache.

Requires `GOOGLEMAPSAPI` in `.env` or environment. If the key is missing, the step logs a warning and exits cleanly so the pipeline remains runnable without paid API access. Junk-tagged entries are never upgraded. Step 05 re-runs the bbox check on the final output as a belt-and-suspenders audit.

### 3. `03_build_nodes_geojson.py`

Reads the geocoded orgs and writes `output/sharing-network-nodes.geojson`. Each org becomes a `Point` feature with these properties:

`id, name, city, state, type, isPortal, portalSlug, isJunk, isInactive, isLikelyAggregator, cameras, searches, vehiclesCaptured, connectionCount, population, hotlistHits, geocodeMethod, aliases`

**`portalSlug`** is the original EyesOnFlock portal slug (e.g., `allen-park-mi`), preserved from the raw data so the frontend can build `eyesonflock.com/portal/<slug>` deep-links. Populated only when `isPortal=true`; `null` otherwise.

`connectionCount` is computed by building the same bidirectional set-based adjacency that step 04 produces, then reading `len(adjacency[slug])`. This guarantees `connectionCount` always equals the node's adjacency-list length.

**`isLikelyAggregator`** uses a deliberate heuristic to flag aggregator/template accounts that would otherwise dominate centrality-based visualization: `connectionCount >= 500 AND population < 50_000`. Real metropolitan sheriffs (Shelby County TN SO, etc.) have high degree but large populations and are not flagged. Treat the flag as an alert, not a factual claim about organizational type. **`isInactive`** flags any node whose `raw_name` or any alias contains inactive/DNU/deactivated markers, even when the canonical slug collapsed it into an active portal's node. **`isJunk`** mirrors the `is_junk` flag from step 01.

### 4. `04_build_adjacency.py`

Reads `data/raw/eyesonflock_full_data.json` again and writes `output/sharing-network-adjacency.json`. For each portal, creates **bidirectional** edges between the portal node and every org in its sharing list — both ends are slugged via `canonical_slug()` so edges always terminate at the same nodes that step 03 emits. Self-edges introduced when an alias canonicalizes back to the portal's own slug are dropped. The output maps each slug to a sorted, deduplicated array of connected slugs.

### 5. `05_audit_geocoding.py`

Optional QC pass. Reads `data/intermediate/geocoded_orgs.json` and writes `data/intermediate/geocode_audit.txt` with:

- Method-breakdown counts and percentages
- A list of orgs that fell to `state` or `default` (manual review candidates)
- Per-state bounding-box check: any geocoded result outside its declared state's approximate lat/lng box is flagged

The bounding boxes use generous (~1°) margins to avoid false positives.

## Auxiliary modules

These libraries are used by the numbered pipeline scripts above but are also safe to import and call directly:

- **`parse_orgs_lib.py`** — exports `parse_org_name`, `slugify`, `canonical_slug`, `portal_canonical_slug`, `portal_as_org_name`, `_strip_status_tags`.
- **`geocode_lib.py`** — gazetteer lookup builders plus `geocode_org` for the Census cascade.
- **`junk_filter.py`** — `is_junk(raw_name)` returns True for obvious non-agency entries (numeric-only, very short, `"DNU"`, `"test"`, `"demo"`, `"do not use"` substrings). Called by `01_parse_orgs.py` to stamp the `is_junk` flag.
- **`google_geocode.py`** — the `GoogleGeocoder` client (on-disk JSON cache, 50-QPS rate limit) used by `02a_google_geocode.py`. Requires `GOOGLEMAPSAPI` env var.

## Outputs

In `output/`:

- `sharing-network-nodes.geojson` — node layer with metadata
- `sharing-network-adjacency.json` — edge list keyed by slug

## Caveats

- **Self-reported relationships.** Sharing is what the transparency portal exposes. Agencies may share via channels not reflected here (e.g., direct DB peering, federal task forces).
- **Snapshot-bound.** The output reflects the date of the EyesOnFlock scrape feeding `eyesonflock_full_data.json`. Refreshing the source is out of scope for this repo.
- **Geocoding precision.** Census place matches resolve to the place's internal point (`INTPTLAT`/`INTPTLONG`), not the actual agency address. State-centroid fallback can produce visible clustering — `05_audit_geocoding.py` flags this.
- **Default fallback (DC) is suspicious.** Any node geocoded to (38.8816, -77.0910) is almost certainly a parse failure, not a real DC agency. Audit the `default`-method orgs.
- **Edges are directional (outbound-only).** `adj[A] = [B]` means "A declares sharing outbound data to B" and only that. If B shares back, `adj[B]` independently contains A; if B doesn't (or B isn't a portal), only the one direction appears. Web-app consumers build the reverse index themselves in one O(E) pass at load. `connectionCount` on each node is `|outbound ∪ inbound|` — the total distinct neighbors in either direction.
- **Canonical slugging may over-merge.** Two genuinely distinct agencies with the same canonical `(city, state, type)` (e.g., two different "Jackson Township OH PD" in different counties) will currently merge into one node. The `aliases` list on each node exposes the raw forms so such merges can be caught in review. The `05_audit_geocoding.py` bbox check also flags same-slug geocodes that land inconsistently.
- **41% of portals contribute no outgoing edges.** Per the source snapshot, 347/841 portals have an empty `organizations_shared_with` list. These agencies still appear as nodes (portal metadata intact) but only receive edges from portals that reference them.
- **Aggregator detection is heuristic.** `isLikelyAggregator=True` means `connectionCount >= 500 AND population < 50_000` — good enough to flag Pittsboro-type aggregator accounts, but a genuinely high-degree small-town agency would also trip this flag. Treat as an alert, not a judgment.

## Run

```bash
python sharing-network/scripts/run_pipeline.py
```

Total runtime: a few minutes. Census cascade geocoding over all ~6k orgs dominates; parsing is fast. If `GOOGLEMAPSAPI` is set, step 02a adds a few seconds to upgrade state/default fallbacks (cached per-query, so subsequent runs are near-free).
