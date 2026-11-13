import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { extname, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = resolve(fileURLToPath(new URL('.', import.meta.url)), '..');

const MIME: Record<string, string> = {
  '.tif': 'image/tiff',
  '.tiff': 'image/tiff',
  '.shp': 'application/octet-stream',
  '.dbf': 'application/octet-stream',
  '.shx': 'application/octet-stream',
  '.cpg': 'text/plain',
  '.prj': 'text/plain',
  '.geojson': 'application/geo+json',
  '.json': 'application/json',
  '.csv': 'text/csv',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.zip': 'application/zip',
  '.md': 'text/markdown',
  '.txt': 'text/plain',
  '.pdf': 'application/pdf',
};

/**
 * Sert la racine du dépôt en lecture seule sous /data/.
 * GET/HEAD uniquement : aucune écriture possible côté serveur.
 */
function repoDataPlugin(): Plugin {
  return {
    name: 'amiad-repo-data-readonly',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.method !== 'GET' && req.method !== 'HEAD') return next();
        const url = (req.url ?? '').split('?')[0];
        if (!url.startsWith('/data/')) return next();
        const rel = normalize(decodeURIComponent(url.slice('/data/'.length)));
        if (rel.startsWith('..') || resolve(REPO_ROOT, rel).startsWith('..')) {
          res.statusCode = 403;
          res.end('Forbidden');
          return;
        }
        const abs = resolve(REPO_ROOT, rel);
        if (!abs.startsWith(REPO_ROOT) || !existsSync(abs) || !statSync(abs).isFile()) {
          res.statusCode = 404;
          res.end('Not found');
          return;
        }
        res.statusCode = 200;
        res.setHeader('Content-Type', MIME[extname(abs).toLowerCase()] ?? 'application/octet-stream');
        res.setHeader('Cache-Control', 'no-store');
        if (req.method === 'HEAD') return res.end();
        createReadStream(abs).pipe(res);
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), repoDataPlugin()],
  server: {
    port: 5173,
    fs: { allow: [REPO_ROOT] },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 4096,
  },
});
