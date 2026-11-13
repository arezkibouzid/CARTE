#!/usr/bin/env node
// Vérifie que les chargeurs client pourront lire les fichiers du dépôt :
// - geotiff : tags d'emprise (ModelPixelScale/ModelTiepoint/GeoKey) des TIFF
// - shpjs : lecture .shp (+ .dbf si raisonnable) des shapefiles
// Usage: node scripts/smoke-data.mjs   (depuis interface/, lit ../ amiad-files et hackathon)
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { fromArrayBuffer } from 'geotiff';
import shp from 'shpjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const TIFFS = ['hackathon/Hackathon_250K.tif', 'hackathon/Hackathon_100K.tif', 'amiad-files/Scenario1 250K.tif', 'amiad-files/Scenario1 100K (1).tif'];
const SHAPES = [
  ['hackathon/Hackathon_OSM.shp', 'hackathon/Hackathon_OSM.dbf'],
  ['hackathon/Hackathon_BDTopo.shp', 'hackathon/Hackathon_BDTopo.dbf'],
];
const MAX_DBF = 300 * 1024 * 1024; // au-delà, on teste la géométrie seule (comme le fallback client)

let failed = 0;
const check = (name, ok, detail = '') => {
  if (!ok) failed++;
  console.log(`${ok ? 'OK ' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`);
};

// geotiff.js renvoie parfois les tags multi-valeurs en objet indexé {"0":…}
const numArray = (v) => (Array.isArray(v) ? v : v && typeof v === 'object' ? Object.values(v) : []);
const ab = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
const shpFiles = (files) => shp(files); // shpjs v6 : {shp, dbf?}

for (const rel of TIFFS) {
  const buf = readFileSync(join(root, rel));
  const tiff = await fromArrayBuffer(ab(buf));
  const image = await tiff.getImage();
  const fd = image.getFileDirectory();
  const w = image.getWidth(), h = image.getHeight();
  const scale = numArray(fd.ModelPixelScale);
  const tie = numArray(fd.ModelTiepoint);
  const geoKeys = numArray(fd.GeoKeyDirectory);
  let epsg;
  for (let i = 4; i + 3 < geoKeys.length; i += 4) {
    if (geoKeys[i] === 3072) { epsg = geoKeys[i + 3]; break; }
  }
  if (epsg === undefined) {
    for (let i = 4; i + 3 < geoKeys.length; i += 4) {
      if (geoKeys[i] === 2048) { epsg = geoKeys[i + 3]; break; }
    }
  }
  const hasGeo = scale.length >= 2 && tie.length >= 6;
  check(`geotiff ${rel}`, hasGeo, `${w}x${h}, samples=${image.getSamplesPerPixel()}, epsgTag=${epsg ?? 'non déclaré'}, origine=${hasGeo ? `${tie[3].toFixed(4)},${tie[4].toFixed(4)}` : 'ABSENTE'}, taille=${hasGeo ? `${(w * scale[0]).toFixed(4)}x${(-h * scale[1]).toFixed(4)} deg` : '-'}`);
}

for (const [shpPath, dbfPath] of SHAPES) {
  const shpBuf = readFileSync(join(root, shpPath));
  const geoOnly = await shpFiles({ shp: ab(shpBuf) });
  const nGeo = Array.isArray(geoOnly) ? geoOnly.length : geoOnly.features.length;
  check(`shpjs géométries ${shpPath}`, nGeo > 0, `${nGeo} entités`);
  const dbfSize = readFileSync(join(root, dbfPath)).length;
  if (dbfSize <= MAX_DBF) {
    const dbfBuf = readFileSync(join(root, dbfPath));
    const full = await shpFiles({ shp: ab(shpBuf), dbf: ab(dbfBuf) });
    const feats = Array.isArray(full) ? full : full.features;
    const withProps = feats.filter((f) => f.properties && Object.keys(f.properties).length > 0).length;
    check(`shpjs complet ${shpPath}+dbf`, feats.length > 0 && withProps > 0, `${feats.length} entités, ${withProps} avec attributs`);
  } else {
    console.log(`SKIP shpjs complet ${dbfPath} — dbf ${(dbfSize / 1e6).toFixed(0)} Mo > seuil ${(MAX_DBF / 1e6).toFixed(0)} Mo (fallback géométries seules côté client)`);
  }
}

process.exit(failed ? 1 : 0);
