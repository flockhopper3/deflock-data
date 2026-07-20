# Paste-ready frontend implementation prompt — boundaries layer

Copy everything below the line into a Claude Code session in the frontend
repo (DeFlock Maps / FlockHopper app).

---

Implement a US boundaries overlay in this app using a new vector tileset I
have already built and deployed. The tileset is live and verified; this task
is 100% frontend.

## What exists (the data contract — do not change it, build against it)

Vector source TileJSON: `https://tiles.dontgetflocked.com/boundaries-us.json`
(tiles are `…/boundaries-us/{z}/{x}/{y}.mvt` — the `.mvt` extension is
required, but always consume via the TileJSON URL, never hardcode tiles).
One PMTiles archive, three source-layers, all polygons:

| source-layer | zooms | attributes on every feature |
|---|---|---|
| `states` | z0–12 | `name` ("Illinois"), `abbrev` ("IL"), `fips` ("17") |
| `counties` | z2–12 | `name` ("Cook"), `state` ("IL"), `fips` ("17031") |
| `municipalities` | z5–12 | `name` ("Chicago"), `type`, `state` ("IL"), `county` ("Cook"), `fips` ("1714000") |

- Attributes are present at ALL zooms in each layer's range — filter
  expressions work everywhere. MapLibre overzooms past z12 automatically
  (maxzoom comes from the TileJSON); geometry is 1:500k and looks fine.
- `municipalities.type` values: lowercase LSAD descriptors — `city`,
  `town`, `township`, `charter township`, `village`, `borough`,
  `plantation`, etc — plus uppercase `CDP` (census-designated place =
  unincorporated community). Rare prefix forms yield e.g. `"town of"`, so
  use `type` only for coarse show/hide classes, never for identity.
- `municipalities.county` is the county NAME containing the largest share
  of the municipality's area; it CAN be `null`. Guard group-by/picker code.
- `fips` is unique within each layer (2-digit state, 5-digit county,
  place/cousub GEOID for municipalities) — it is the identity key.
- NY villages overlap the towns that contain them by design (both are real
  municipalities) — expect stacked polygons unless filtered by `type`.
- CDPs are included deliberately; most "real municipal government only"
  views should filter them out with `['!=', ['get', 'type'], 'CDP']`.

## What to build

1. **Source + render layers.** Add the vector source once (with
   `promoteId: { states: 'fips', counties: 'fips', municipalities: 'fips' }`
   so feature-state works), and fill+line layer pairs for each of the three
   source-layers. Insert them BELOW the existing camera heat/dot layers —
   boundaries are context, cameras stay on top. Style to match the app's
   existing map aesthetic (subtle fills ~0.1 opacity, thin lines; pick
   colors from the app's existing palette/theme system rather than
   inventing new ones).

2. **Boundary controls in the UI**, following the app's existing control
   patterns (wherever layer toggles / the pending camera-filter UI live):
   - A master toggle for the boundaries overlay (default OFF — zero tile
     requests until enabled; add the source lazily or set layer visibility
     `none`).
   - Level selector: States / Counties / Municipalities (single or multi,
     whichever fits the existing UI language).
   - A "hide CDPs" toggle for the municipalities level (default: hidden).
   - Free-text filter: typing a state abbreviation or county name filters
     the visible polygons via `setFilter` expressions on `state` /
     `county` / `name`. Case-insensitive compare: build the expression
     with `['downcase', ['get', 'county']]` etc.

3. **Hover + click.** Hover highlight via feature-state (promoteId makes
   `fips` the feature id). Click shows a small popup: name, type (for
   municipalities), county, state — reuse the app's existing popup
   component/styling.

4. **Enumeration for pickers (if the design needs dropdowns rather than
   free text):** tiles only contain the viewport, so DO NOT try to
   enumerate all counties from `queryRenderedFeatures`. Ship a static
   states list (56 entries) in the bundle; for counties either ship a
   static list (~3,235 rows, a few KB gzipped) or scope the picker to the
   viewport via `querySourceFeatures` deduped by `fips`. Choose the
   simplest that fits the design and say which you chose.

## Constraints

- MapLibre GL expressions and `setFilter`/`setPaintProperty` only — no new
  server code, no proxy, no re-requesting tiles when filters change (that
  is the entire point of the attribute-rich tiles).
- Follow the repo's existing conventions for state management, component
  structure, and styling — read how the camera layers and any existing
  layer toggles are wired before writing code, and mirror that.
- Dedupe any `queryRenderedFeatures`/`querySourceFeatures` results by
  `fips` (one polygon spans many tiles).
- Keep the boundaries overlay's default state OFF so users who never open
  it pay zero cost.

## Acceptance criteria

- With the overlay off: no requests to `boundaries-us*` at all.
- Toggle on + Counties level: county outlines render nationwide from z2,
  cameras still render on top, hover highlights a county, click pops its
  name/state.
- Type "Cook" + "IL" (or select from picker): only Cook County renders.
- Municipalities level at z10 over Boston: New England towns (type
  `town`) appear; toggling "hide CDPs" removes `CDP` features without any
  network activity (verify in devtools).
- Municipality popup over Chicago shows: Chicago · city · Cook · IL.
- No console errors; filters respond instantly (no tile refetch).

Verify in the running app (devtools network tab for the zero-refetch
claims), not just by code reading.
