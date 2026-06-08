# Map Layer Styling Reference

All styles defined in `src/components/map/MapLibreContainer.tsx`.

---

## Dot Density Layer (`dot-density`)

PMTiles source. The primary visualization at national through city zoom.

| Property | Value |
|----------|-------|
| Source | `cameras` (PMTiles vector) |
| Source layer | `cameras` |
| Max zoom | 14 (layer not rendered past this) |

### circle-radius

Exponential interpolation (base 1.5):

| Zoom | Radius (px) |
|------|-------------|
| 3 | 1.5 |
| 6 | 2.5 |
| 8 | 4 |
| 10 | 6 |
| 12 | 8 |
| 13 | 7 (taper for crossfade) |

### circle-opacity

Linear interpolation:

| Zoom | Opacity |
|------|---------|
| 0 | 0.55 |
| 7 | 0.6 |
| 10 | 0.85 |
| 12 | 0.95 |
| 13 | 0.2 |
| 14 | 0 (fully transparent — layer ends) |

### circle-blur

Linear interpolation:

| Zoom | Blur |
|------|------|
| 0 | 0.3 |
| 10 | 0.1 |
| 12 | 0 |

### circle-color

`#ef4444` (red-500)

---

## Unclustered Point Layer (`unclustered-point`)

GeoJSON source. Interactive camera markers at close zoom.

| Property | Value |
|----------|-------|
| Source | `cameras-detail` (GeoJSON) |
| Min zoom | 11 |

### Paint

| Property | Value |
|----------|-------|
| circle-color | `#dc2626` (red-600) |
| circle-radius | 6px (fixed) |
| circle-stroke-width | 2px |
| circle-stroke-color | `#fca5a5` (red-300) |
| circle-stroke-opacity | 0 at z11 → 1 at z13 (linear) |
| circle-opacity | 0 at z11 → 1 at z13 (linear) |

---

## Unclustered Glow Layer (`unclustered-glow`)

GeoJSON source. Soft halo behind each camera marker.

| Property | Value |
|----------|-------|
| Source | `cameras-detail` (GeoJSON) |
| Min zoom | 11 |

### circle-radius

Linear interpolation:

| Zoom | Radius (px) |
|------|-------------|
| 11 | 6 |
| 14 | 14 |
| 16 | 18 |

### Paint

| Property | Value |
|----------|-------|
| circle-color | `#ef4444` (red-500) |
| circle-opacity | 0 at z11 → 0.3 at z13 (linear) |
| circle-blur | 0.6 (fixed) |

---

## Direction Cones (`direction-cones`)

GeoJSON source (generated client-side). Only for cameras with a `direction` property.

| Property | Value |
|----------|-------|
| Source | `direction-cones` (GeoJSON) |
| Min zoom | 12 |

### Cone Geometry Parameters

| Parameter | Value |
|-----------|-------|
| Radius | 150 ft (45.72m) |
| Field of view | 50 degrees |
| Arc segments | 8 |
| Origin | Camera lat/lon |

### Fill Paint

| Property | Value |
|----------|-------|
| fill-color | `#ef4444` (red-500) |
| fill-opacity | 0.35 |

### Outline Paint (`direction-cones-outline`)

| Property | Value |
|----------|-------|
| line-color | `#dc2626` (red-600) |
| line-width | 2px |
| line-opacity | 0.7 |

---

## Pulse Animation Rings

GeoJSON source. Animated via `requestAnimationFrame`.

### Outer Ring (`pulse-ring-outer`)

| Property | Value |
|----------|-------|
| Min zoom | 12 |
| circle-color | `#ef4444` |
| circle-radius | 12px (static) / 16–20px (animated) |
| circle-opacity | 0 (static) / 0.15–0.5 (animated) |
| circle-blur | 0.5 |

### Inner Ring (`pulse-ring-inner`)

| Property | Value |
|----------|-------|
| Min zoom | 12 |
| circle-color | `#ef4444` |
| circle-radius | 8px (static) / 10–12px (animated) |
| circle-opacity | 0 (static) / 0.3–0.75 (animated) |
| circle-blur | 0.3 |

Animation cycle: 1800ms, sinusoidal breathing.

---

## Crossfade Zones (Zoom Transitions)

```
z0 ─────────── z10 ── z11 ── z12 ── z13 ── z14 ── z16+
                              │             │
dot-density    ████████████████░░░░░░░░░░░░░│      (fades z12→14)
glow                          ░░░░░░████████████   (fades in z11→13)
unclustered-point             ░░░░░░████████████   (fades in z11→13)
pulse rings                          ██████████    (appears z12+)
direction cones                      ██████████    (appears z12+)

█ = fully visible   ░ = transitioning
```

---

## Route Lines

### Privacy Route (avoidance)

| Property | Value |
|----------|-------|
| line-color | `#3b82f6` (blue-500) |
| line-width | 6px |
| line-opacity | 0.95 |
| Outline | black, 9px, 0.3 opacity |

### Direct Route (normal)

| Property | Value |
|----------|-------|
| line-color | `#f97316` (orange-500) |
| line-width | 5px |
| line-opacity | 0.95 |
| line-dasharray | [2, 1.5] |
| Outline | black, 8px, 0.25 opacity |

---

## Color Palette

| Token | Hex | Usage |
|-------|-----|-------|
| red-500 | `#ef4444` | Dot density, glow, cones, pulse |
| red-600 | `#dc2626` | Unclustered point, cone outline |
| red-300 | `#fca5a5` | Point stroke |
| blue-500 | `#3b82f6` | Privacy/avoidance route |
| orange-500 | `#f97316` | Direct/normal route |
| green-500 | `#22c55e` | Origin marker |
| red-500 | `#ef4444` | Destination marker |
| white | `#ffffff` | Marker strokes |

---

## Tuning Notes

- **Dot density radius at z10 controls the "wow" factor** — larger values make dense cities (Houston, LA) appear blanketed. Currently 6px.
- **Dot density opacity at z0–z7 controls dot-density effect** — lower values (0.4–0.5) let overlapping dots stack into brighter spots. Higher values (0.8+) give solid individual dots.
- **Glow radius ramp** should start near the dot-density radius at the crossover point (z11) to avoid a size jump during transition.
- **Cone length (150ft)** is display-only and intentionally smaller than the routing avoidance radius (75m base + multipliers). Don't couple these.
