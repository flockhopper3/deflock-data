const http = require('http');
const fs = require('fs');
const path = require('path');
const { PMTiles } = require('pmtiles');

const PORT = Number(process.env.PORT) || 3000;
const ROOT = path.join(__dirname, '..');
const STATIC = __dirname;

const MIME = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.geojson': 'application/geo+json',
  '.pmtiles': 'application/octet-stream',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
};

// Custom file-based source for PMTiles (Node's fetch doesn't support file://)
class FileSource {
  constructor(filePath) {
    this.filePath = filePath;
    this.fd = fs.openSync(filePath, 'r');
  }
  getKey() { return this.filePath; }
  async getBytes(offset, length) {
    const buf = Buffer.alloc(length);
    fs.readSync(this.fd, buf, 0, length, offset);
    return { data: buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) };
  }
}

const tileCache = new Map();

function getPMTiles(filename) {
  if (tileCache.has(filename)) return tileCache.get(filename);
  let fp = path.join(STATIC, filename);
  if (!fs.existsSync(fp)) {
    fp = path.join(ROOT, filename);
    if (!fs.existsSync(fp)) return null;
  }
  const p = new PMTiles(new FileSource(fp));
  tileCache.set(filename, p);
  return p;
}

function serve(res, filePath) {
  const ext = path.extname(filePath);
  const mime = MIME[ext] || 'application/octet-stream';
  const stat = fs.statSync(filePath);
  res.setHeader('Content-Type', mime);
  res.setHeader('Content-Length', stat.size);
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Cache-Control', 'no-cache');
  fs.createReadStream(filePath).pipe(res);
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', 'Range');
    res.setHeader('Access-Control-Expose-Headers', 'Content-Range, Content-Length');
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url, `http://localhost:${PORT}`);
  let pathname = decodeURIComponent(url.pathname);

  if (pathname === '/') pathname = '/index.html';

  // Serve MVT tiles: /tiles/{filename}/{z}/{x}/{y}.mvt
  const tileMatch = pathname.match(/^\/tiles\/([^/]+)\/(\d+)\/(\d+)\/(\d+)\.mvt$/);
  if (tileMatch) {
    const [, filename, z, x, y] = tileMatch;
    const pmtiles = getPMTiles(filename + '.pmtiles');
    if (!pmtiles) {
      res.writeHead(404);
      res.end('Tileset not found');
      return;
    }
    try {
      const tile = await pmtiles.getZxy(+z, +x, +y);
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Cache-Control', 'no-cache');
      if (!tile) {
        res.writeHead(204);
        res.end();
        return;
      }
      res.setHeader('Content-Type', 'application/vnd.mapbox-vector-tile');
      res.setHeader('Content-Length', tile.data.byteLength);
      res.writeHead(200);
      res.end(Buffer.from(tile.data));
    } catch (e) {
      console.error('Tile error:', e.message);
      res.writeHead(500);
      res.end('Tile read error');
    }
    return;
  }

  // Serve TileJSON: /tiles/{filename}.json
  const jsonMatch = pathname.match(/^\/tiles\/([^/]+)\.json$/);
  if (jsonMatch) {
    const filename = jsonMatch[1];
    const pmtiles = getPMTiles(filename + '.pmtiles');
    if (!pmtiles) {
      res.writeHead(404);
      res.end('Tileset not found');
      return;
    }
    try {
      const header = await pmtiles.getHeader();
      const metadata = await pmtiles.getMetadata();
      const tilejson = {
        tilejson: '3.0.0',
        tiles: [`http://localhost:${PORT}/tiles/${filename}/{z}/{x}/{y}.mvt`],
        minzoom: header.minZoom,
        maxzoom: header.maxZoom,
        bounds: [header.minLon, header.minLat, header.maxLon, header.maxLat],
        vector_layers: metadata.vector_layers || [],
      };
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Content-Type', 'application/json');
      res.writeHead(200);
      res.end(JSON.stringify(tilejson));
    } catch (e) {
      console.error('TileJSON error:', e.message);
      res.writeHead(500);
      res.end('TileJSON error');
    }
    return;
  }

  // List available tilesets
  if (pathname === '/tilesets') {
    const files = [
      ...fs.readdirSync(STATIC).filter(f => f.endsWith('.pmtiles')),
      ...fs.readdirSync(ROOT).filter(f => f.endsWith('.pmtiles')),
    ];
    const unique = [...new Set(files)];
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Content-Type', 'application/json');
    res.writeHead(200);
    res.end(JSON.stringify(unique));
    return;
  }

  // Log benchmark results
  if (pathname === '/log' && req.method === 'POST') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      const logPath = path.join(STATIC, 'benchmark-log.jsonl');
      fs.appendFileSync(logPath, body + '\n');
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.writeHead(200);
      res.end('ok');
    });
    return;
  }

  // Get benchmark log
  if (pathname === '/log' && req.method === 'GET') {
    const logPath = path.join(STATIC, 'benchmark-log.jsonl');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Content-Type', 'application/json');
    if (fs.existsSync(logPath)) {
      const lines = fs.readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean);
      res.writeHead(200);
      res.end('[' + lines.join(',') + ']');
    } else {
      res.writeHead(200);
      res.end('[]');
    }
    return;
  }

  // Serve GeoJSON from project root
  if (pathname === '/cameras.geojson') {
    const fp = path.join(ROOT, 'cameras.geojson');
    if (fs.existsSync(fp)) {
      serve(res, fp);
      return;
    }
  }

  // Serve static files from local-dev/
  const staticPath = path.join(STATIC, pathname);
  if (fs.existsSync(staticPath) && fs.statSync(staticPath).isFile()) {
    serve(res, staticPath);
    return;
  }

  res.writeHead(404);
  res.end('Not found');
});

server.listen(PORT, async () => {
  console.log(`\n  Local MapLibre dev server running at:`);
  console.log(`  http://localhost:${PORT}\n`);

  // List available tilesets
  const files = fs.readdirSync(STATIC).filter(f => f.endsWith('.pmtiles'));
  const rootFiles = fs.readdirSync(ROOT).filter(f => f.endsWith('.pmtiles'));
  const all = [...new Set([...files, ...rootFiles])];
  console.log(`  Tilesets:`);
  for (const f of all) {
    const name = f.replace('.pmtiles', '');
    console.log(`    /tiles/${name}.json`);
  }
  console.log();
});
