# US Boundaries Tileset

`boundaries-us.pmtiles` — states, counties, and municipalities as polygons
with enough metadata to filter client-side. Built from Census Bureau
cartographic boundary files (1:500k, vintage pinned in `build.sh`),
uploaded to R2 by the manual **Build Boundaries Tiles** workflow, and
served like every other archive at
`https://tiles.dontgetflocked.com/boundaries-us.json` (TileJSON) /
`…/boundaries-us/{z}/{x}/{y}` (tiles).

## Layers & attributes (the filtering contract)

| Layer | Zooms | Attributes |
|-------|-------|------------|
| `states` | z0–12 | `name` ("Illinois"), `abbrev` ("IL"), `fips` ("17") |
| `counties` | z2–12 | `name` ("Cook"), `state` ("IL"), `fips` ("17031") |
| `municipalities` | z5–12 | `name` ("Chicago"), `type` ("city"), `state` ("IL"), `county` ("Cook"), `fips` (place or cousub GEOID) |

- Attributes exist at **all** zooms in each layer's range — filter
  expressions work everywhere. MapLibre overzooms past z12.
- `type` is derived from the Census LSAD descriptor: `city`, `town`,
  `village`, `borough`, `township`, `CDP`, … CDPs are unincorporated
  census-designated places — filter `['!=', ['get', 'type'], 'CDP']` to
  show only real municipal governments.
- `county` on a municipality is the county containing the largest share of
  its area (places can straddle counties); `null` if none.
- The municipalities layer is places nationwide **plus** functioning
  townships/towns in the 12 strong-MCD states (New England, NY, NJ, PA,
  MI, WI, MN). NY villages overlap their surrounding towns by design —
  filter by `type` if you want one level.

## Rebuilding

Boundaries change ~annually. Bump `VINTAGE` in `build.sh` when the Census
publishes a new vintage, then dispatch the **Build Boundaries Tiles**
workflow. Local preview: `bash build.sh --local` (needs tippecanoe + jq;
downloads ~120 MB of shapefiles, cache them with `BOUNDARIES_WORK_DIR`).
