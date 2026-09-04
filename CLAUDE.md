# deflock-data — working rules

## Hard rule: no pushes, no Cloudflare, without permission

Never run `git push`, and never deploy to or upload to Cloudflare (Workers,
R2, Pages, `wrangler`, `aws s3 … --endpoint-url …r2.cloudflarestorage.com`)
unless the owner has explicitly said to in the current conversation. Local
commits on a branch are fine. A yes covers that one action, not the session.

## Layout

- `data/` — hourly camera ingestion (Overpass → R2)
- `tiles/` — PMTiles builds (cameras, boundaries)
- `eyesonflock/` — sharing-network pipeline (EyesOnFlock → GeoJSON + adjacency)

Each hub is independent. Don't share code across them.
