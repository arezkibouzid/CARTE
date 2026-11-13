#!/usr/bin/env node
// Smoke test des fonctions pures (format.ts + constants.ts) via esbuild (déjà dans node_modules).
import { buildSync } from 'esbuild';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');

async function loadTs(entry) {
  const res = buildSync({ entryPoints: [join(root, entry)], bundle: true, format: 'esm', write: false, platform: 'node' });
  return import('data:text/javascript;base64,' + Buffer.from(res.outputFiles[0].text).toString('base64'));
}

const format = await loadTs('src/lib/format.ts');
const constants = await loadTs('src/constants.ts');

const checks = [
  ['formatValue(null)', format.formatValue(null), 'Non renseigné'],
  ['formatValue(undefined)', format.formatValue(undefined), 'Non renseigné'],
  ['formatValue("")', format.formatValue(''), 'Non renseigné'],
  ['formatValue(1234567)', format.formatValue(1234567), '1\u202f234\u202f567'],
  ['formatValue(true)', format.formatValue(true), 'Oui'],
  ['formatKm(56.145)', format.formatKm(56.145), '56,145 km'],
  ['formatKm(null)', format.formatKm(null), 'Non renseigné'],
  ['NON_DISPONIBLE', constants.NON_DISPONIBLE, 'Non disponible'],
  ['rôles tiff', constants.ALLOWED_ROLES.tiff.join(','), 'fond_tiff,reseau_candidat'],
  ['rôle interdit tiff→réseau importé', constants.ALLOWED_ROLES.tiff.includes('reseau_importe'), false],
];

let failed = 0;
for (const [name, got, want] of checks) {
  const ok = got === want;
  if (!ok) failed++;
  console.log(`${ok ? 'OK ' : 'FAIL'} ${name} → ${JSON.stringify(got)}${ok ? '' : ` (attendu ${JSON.stringify(want)})`}`);
}
process.exit(failed ? 1 : 0);
