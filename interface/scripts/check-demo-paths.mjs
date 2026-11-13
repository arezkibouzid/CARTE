#!/usr/bin/env node
// Sonde en HEAD tous les chemins /data/… référencés par src/data/demoProjects.ts.
// Usage: node scripts/check-demo-paths.mjs [baseURL]   (défaut http://localhost:5173)
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const base = (process.argv[2] ?? 'http://localhost:5173').replace(/\/$/, '');
const src = readFileSync(join(here, '../src/data/demoProjects.ts'), 'utf8');
const all = [...new Set([...src.matchAll(/'(\/data\/[^']+)'/g)].map((m) => m[1]))];
// ne garder que les fichiers (les resultPaths sont des dossiers, sondés élément par élément dans l'app)
const paths = all.filter((p) => /\.[a-z0-9]+$/i.test(p.split('/').pop() ?? ''));

let ok = 0;
const bad = [];
for (const p of paths) {
  const url = base + encodeURI(p).replace(/%2F/gi, '/');
  try {
    const res = await fetch(url, { method: 'HEAD' });
    if (res.ok) ok++;
    else bad.push(`${res.status}  ${p}`);
  } catch (e) {
    bad.push(`ERR  ${p} (${e.message})`);
  }
}
console.log(`${ok}/${paths.length} chemins OK`);
for (const b of bad) console.log(b);
process.exit(bad.length ? 1 : 0);
