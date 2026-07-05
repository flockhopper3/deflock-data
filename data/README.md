# data/

Ingestion code — the scripts that pull raw camera data (OpenStreetMap surveillance tagging) and publish `cameras.geojson.gz` to the R2 data bucket.

**Status: not yet migrated.** The data currently lands in R2 from an external process; that code will move here so the whole pipeline lives in one repo. Until then, the tile pipeline in [`tiles/`](../tiles/) treats the R2 object as its source of truth.

Planned layout: one subfolder per source (e.g. `data/osm/`), each with its own fetch script and README documenting the source, update cadence, and output schema.
