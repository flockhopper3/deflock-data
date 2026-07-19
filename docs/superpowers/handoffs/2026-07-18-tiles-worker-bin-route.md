# flockhopper-tiles Worker: serve `*-index.bin` — handoff

The build pipeline (this repo) now uploads two objects per country to the
`flockhopper-tiles` bucket:

- `cameras-<cc>-hourly-index.bin`  — stored gzipped, `Content-Encoding: gzip`,
  `Content-Type: application/octet-stream`
- `cameras-<cc>-hourly-index.json` — stored gzipped, `Content-Encoding: gzip`,
  `Content-Type: application/json`

The Worker currently routes `*.pmtiles` (unpacked to `z/x/y`) and the manifest
JSON. It does **not** yet serve `.bin`. Add a passthrough route that streams the
stored R2 object with its metadata intact — same CORS + cache/etag policy as the
manifest. **You deploy this** (per the no-Cloudflare-deploys rule); the pipeline
side only uploads.

Reference route (adapt to the actual Worker structure — the key detail is
forwarding `httpMetadata.contentEncoding`, setting the etag, and CORS):

```js
// Inside the Worker fetch handler, alongside the manifest passthrough:
if (key.endsWith('-index.bin') || key.endsWith('-index.json')) {
  const obj = await env.TILES.get(key, {
    // hourly-fresh conditional read, same as manifest/pmtiles swaps
    onlyIf: request.headers,
  });
  if (!obj) return new Response('Not found', { status: 404 });
  const headers = new Headers();
  obj.writeHttpMetadata(headers);          // Content-Type + Content-Encoding: gzip
  headers.set('etag', obj.httpEtag);
  headers.set('Access-Control-Allow-Origin', '*');
  headers.set('Cache-Control', 'public, max-age=300, s-maxage=3600');
  if (obj.body === null) return new Response(null, { status: 304, headers });
  return new Response(obj.body, { headers });
}
```

Notes:
- `writeHttpMetadata` re-emits the stored `Content-Encoding: gzip`, so the
  browser/undici transparently inflates on `fetch().arrayBuffer()` — clients see
  the raw `16 + 9N` bytes. Do **not** decompress in the Worker.
- If the Worker already has a generic "any other key → R2 passthrough" branch
  that forwards `Content-Encoding` + etag + CORS, no change is needed; confirm it
  covers `.bin` and does not force `Content-Type`.

## Acceptance after deploy

```
curl -sI https://tiles.dontgetflocked.com/cameras-us-hourly-index.bin
# → 200, content-encoding: gzip, content-type: application/octet-stream,
#   access-control-allow-origin: *, etag present
```

Then the client acceptance snippet in
`docs/superpowers/specs/2026-07-18-camera-positions-index-design.md` must
round-trip against both US and CA `.bin` + `.json`.
