# tiles/

Tile pipelines — one subfolder per tileset, plus a shared local preview harness.

| Folder | What it is |
|--------|------------|
| [`cameras/`](cameras/) | ALPR camera tileset: build script + reference MapLibre style. Built hourly by [`build-tiles.yml`](../.github/workflows/build-tiles.yml). |
| [`local-dev/`](local-dev/) | Local tile server + preview/benchmark pages for tuning styles before deploying. |

## Adding a new tileset

1. Create `tiles/<name>/` with a `build.sh` (fetch → validate → tippecanoe → upload, mirroring `cameras/build.sh`) and a `layers.json` reference style
2. Add a job or step for it in `.github/workflows/build-tiles.yml`
3. Upload to the tiles bucket under its own filename — everything in that bucket is public

## Local preview

```bash
cd tiles/local-dev
npm install
node server.js
# open http://localhost:3000/heatmap-preview.html
```

The server serves any `.pmtiles` file in `tiles/` or `tiles/local-dev/` as `/tiles/{name}/{z}/{x}/{y}.mvt` with TileJSON at `/tiles/{name}.json` — no range-request setup needed.
