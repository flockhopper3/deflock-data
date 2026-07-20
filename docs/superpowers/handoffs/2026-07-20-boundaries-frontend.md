# Frontend implementation — US boundaries tileset

Consuming `boundaries-us.pmtiles` in the app (MapLibre GL). The tileset is
live; everything here is client-side only.

## 1. Add the source

```js
map.addSource('boundaries-us', {
  type: 'vector',
  url: 'https://tiles.dontgetflocked.com/boundaries-us.json',
  // fips is unique per feature in every layer — promote it so
  // feature-state (hover/selection) works without feature ids
  promoteId: { states: 'fips', counties: 'fips', municipalities: 'fips' },
});
```

The TileJSON carries the tile template (`…/boundaries-us/{z}/{x}/{y}.mvt`),
min/max zoom (0/12), and the three `vector_layers`. Don't hardcode tile
URLs — if you ever must, note the `.mvt` extension is required (bare
`/z/x/y` 404s).

## 2. Layers (source-layer → what's in it)

| source-layer | zooms | attributes |
|---|---|---|
| `states` | z0–12 | `name` ("Illinois"), `abbrev` ("IL"), `fips` ("17") |
| `counties` | z2–12 | `name` ("Cook"), `state` ("IL"), `fips` ("17031") |
| `municipalities` | z5–12 | `name` ("Chicago"), `type` ("city"/"town"/"township"/"village"/"borough"/"CDP"/…), `state` ("IL"), `county` ("Cook" — county *name*, may be `null`), `fips` |

Attributes exist at **all** zooms in each layer's range, so filter
expressions work everywhere. MapLibre overzooms past z12 automatically
(maxzoom comes from the TileJSON) — 1:500k geometry holds up fine.

Minimal render pair (repeat per layer you want visible):

```js
map.addLayer({
  id: 'muni-fill',
  type: 'fill',
  source: 'boundaries-us',
  'source-layer': 'municipalities',
  paint: { 'fill-color': '#7c9cff', 'fill-opacity': 0.12 },
});
map.addLayer({
  id: 'muni-line',
  type: 'line',
  source: 'boundaries-us',
  'source-layer': 'municipalities',
  paint: { 'line-color': '#7c9cff', 'line-width': 1 },
});
```

Add these **below** the camera dot/heat layers so cameras stay on top.

## 3. Filtering (the whole point)

Set the same `filter` on the fill + line pair:

```js
// Everything in one county
const cookCounty = ['all',
  ['==', ['get', 'county'], 'Cook'],
  ['==', ['get', 'state'], 'IL'],
];

// One municipality
const chicago = ['==', ['get', 'fips'], '1714000'];

// Real municipal governments only (hide unincorporated CDPs)
const noCdps = ['!=', ['get', 'type'], 'CDP'];

map.setFilter('muni-fill', ['all', noCdps, cookCounty]);
map.setFilter('muni-line', ['all', noCdps, cookCounty]);
```

For a county outline itself, filter the `counties` source-layer on
`name`+`state` (county names repeat across states) or on the 5-digit
`fips` (globally unique — prefer it when you have it).

## 4. Hover / selection

`promoteId` makes `fips` the feature id, so the standard pattern works:

```js
let hovered = null;
map.on('mousemove', 'muni-fill', (e) => {
  if (hovered !== null)
    map.setFeatureState({ source: 'boundaries-us', sourceLayer: 'municipalities', id: hovered }, { hover: false });
  hovered = e.features[0].id;
  map.setFeatureState({ source: 'boundaries-us', sourceLayer: 'municipalities', id: hovered }, { hover: true });
});
// paint: 'fill-opacity': ['case', ['boolean', ['feature-state', 'hover'], false], 0.3, 0.12]
```

## 5. Building a filter UI (picker lists)

Tiles only contain what's in the viewport, so `queryRenderedFeatures` can't
enumerate "all counties in Illinois". Options, simplest first:

1. Ship static lists in the app (state list is tiny; county list ~3,235
   rows compresses to a few KB) — generate once from the tileset build if
   needed.
2. `querySourceFeatures` after zooming to the region — fine for
   viewport-scoped pickers, wrong for global ones.

## 6. Gotchas

- `county` on a municipality is the **largest-overlap** county (straddlers
  get one value) and can be `null` — guard picker/group-by code.
- NY villages overlap their surrounding towns by design — both are real
  municipalities. If you render both, expect stacked polygons; filter by
  `type` if you want one level.
- `type` values are lowercase descriptors except `CDP`; rare prefix forms
  produce e.g. `"town of"`. Compare against `fips` for identity, use
  `type` only for coarse show/hide classes.
- One polygon can span many tiles — dedupe `queryRenderedFeatures` results
  by `fips` before showing counts/lists.
