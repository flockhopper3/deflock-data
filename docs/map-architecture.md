# Map Rendering Architecture

## Overview

The map system renders ~90K ALPR camera locations using a two-source strategy: PMTiles for lightweight dot-density at wide zoom, and GeoJSON for interactive detail layers at close zoom.

## File Locations

| File | Purpose |
|------|---------|
| `src/components/map/MapLibreContainer.tsx` | All map rendering: layers, styles, interactions, cone generation |
| `src/store/cameraStore.ts` | Camera data management, spatial grid, filtering |
| `src/store/mapStore.ts` | Map viewport state (center, zoom, bounds) |
| `src/services/cameraDataService.ts` | Loads bundled camera JSON, retry logic |
| `src/utils/geo.ts` | Spatial grid for O(1) bounding-box lookups |
| `src/types/camera.ts` | `ALPRCamera`, `CameraOnRoute`, filter types |
| `public/cameras.pmtiles` | Pre-built vector tiles (coordinates only, no properties) |

## Data Sources

### 1. PMTiles (dot-density, national → city zoom)

- **Source ID**: `cameras`
- **Type**: `vector` (protocol: `pmtiles://`)
- **URL**: `pmtiles://{origin}/cameras.pmtiles`
- **Source layer**: `cameras`
- **Contains**: Point coordinates only — no camera properties (lightweight)
- **Used by**: `dot-density` layer
- **Zoom range**: z0–z14 (maxzoom on the layer)

The PMTiles protocol is registered once at module load via `maplibre-gl`'s `addProtocol`. Tiles are fetched via HTTP range requests — only tiles in the current viewport are loaded.

### 2. GeoJSON (detail layers, close zoom)

- **Source ID**: `cameras-detail`
- **Type**: `geojson`
- **Contains**: Full camera objects with all properties (operator, brand, direction, etc.)
- **Built from**: `useCameraStore().cameras` array (loaded from bundled JSON)
- **Used by**: `unclustered-point`, `unclustered-glow`, `pulse-ring-outer`, `pulse-ring-inner` layers
- **Zoom range**: z11+ (minzoom on layers)

This source provides the properties needed for popups, direction cones, and interactive features that PMTiles can't deliver (since the tiles contain coordinates only).

### 3. Direction Cones (generated client-side)

- **Source ID**: `direction-cones`
- **Type**: `geojson`
- **Contains**: Polygon features generated from camera `direction` field
- **Built by**: `updateDirectionCones()` — queries rendered features from `unclustered-point`, generates cone polygons for any with a `direction` property
- **Zoom range**: z12+ (minzoom on layers)
- **Regenerated on**: every map move/zoom event

## Layer Stack (render order, bottom to top)

```
direction-cones          (fill, z12+)  — cone polygons
direction-cones-outline  (line, z12+)  — cone outlines
dot-density              (circle, z0–z14) — PMTiles dots
pulse-ring-outer         (circle, z12+) — animated glow
pulse-ring-inner         (circle, z12+) — animated glow
unclustered-glow         (circle, z11+) — static outer glow
unclustered-point        (circle, z11+) — interactive marker (click target)
routes / markers         (line/circle)  — route lines and origin/dest markers
```

## How the Transition Works (dot-density → detail)

The two source systems overlap between z11–z14:

- **z0–z11**: Only PMTiles dot-density visible (no detail layers loaded)
- **z11–z13**: Both visible — dot-density fading out, detail layers fading in
- **z14+**: Only detail layers visible (dot-density layer has maxzoom: 14)

The crossfade is controlled by opacity interpolation on each layer (see styling doc).

## Direction Cone Generation

Cones are built client-side in `createDirectionCone()`:

1. On every map move at z12+, `queryRenderedFeatures` gets all visible `unclustered-point` features
2. For each feature with a `direction` property, generate a polygon:
   - Apex at camera location
   - Arc of 8 segments at 150ft radius
   - 50-degree field of view spread
3. Set the resulting FeatureCollection as the `direction-cones` source data

Parameters:
- **Length**: 150 ft (45.72m) — display only, not tied to routing avoidance radius
- **Spread**: 50 degrees
- **Arc segments**: 8

## Pulse Animation

A `requestAnimationFrame` loop runs continuously after map load, breathing the `pulse-ring-outer` and `pulse-ring-inner` layers:

- Duration: 1800ms per cycle
- Outer: radius 16–20px, opacity 0.15–0.5
- Inner: radius 10–12px, opacity 0.3–0.75
- Uses `setPaintProperty` directly on the map instance

## Interactions

- **Click on `unclustered-point`**: Shows popup with camera details (operator, brand, direction, mount type, etc.)
- **Click in waypoint mode**: Adds a custom route waypoint
- **Click in pick-location mode**: Sets origin/destination with reverse geocoding
- **Interactive layer IDs**: `['unclustered-point']` — only the detail marker is clickable

## Key Dependencies

- `react-map-gl/maplibre` — React wrapper for MapLibre GL
- `maplibre-gl` — WebGL map renderer
- `pmtiles` — Protocol handler for range-request tile access
- `zustand` — State management (camera, map, route stores)
