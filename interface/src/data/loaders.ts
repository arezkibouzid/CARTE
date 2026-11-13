import { fromArrayBuffer } from 'geotiff';
import shp from 'shpjs';
import type { FeatureCollection } from 'geojson';
import type { TiffImage } from '../types';

/** Lit un GeoJSON servi en lecture seule sous /data/. */
export async function fetchGeoJSON(path: string): Promise<FeatureCollection> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status} pour ${path}`);
  return (await res.json()) as FeatureCollection;
}

/** Convertit un shapefile (.shp + .dbf du dépôt, ou .zip uploadé) en GeoJSON côté client. */
export async function loadShapefile(shpPath: string, dbfPath?: string, zipPath?: string): Promise<FeatureCollection> {
  if (zipPath) {
    const buf = await (await fetch(zipPath)).arrayBuffer();
    return normalizeFC(await shp(buf));
  }
  const shpBuf = await (await fetch(shpPath)).arrayBuffer();
  if (!dbfPath) throw new Error('Le .dbf accompagnant le .shp est requis.');
  const dbfBuf = await (await fetch(dbfPath)).arrayBuffer();
  // shpjs v6 : entrée par objet {shp, dbf} (shp.combine n'existe plus ; les @types v3 ne le savent pas)
  const shpFiles = shp as unknown as (files: { shp: ArrayBuffer; dbf?: ArrayBuffer } | ArrayBuffer) => Promise<unknown>;
  return normalizeFC(await shpFiles({ shp: shpBuf, dbf: dbfBuf }));
}

/** Charge un .shp seul (géométries sans attributs) — repli si le .dbf est illisible. */
export async function loadShapefileGeometryOnly(shpPath: string): Promise<FeatureCollection> {
  const buf = await (await fetch(shpPath)).arrayBuffer();
  const shpFiles = shp as unknown as (files: { shp: ArrayBuffer } | ArrayBuffer) => Promise<unknown>;
  return normalizeFC(await shpFiles({ shp: buf }));
}

export function normalizeFC(g: unknown): FeatureCollection {
  if (Array.isArray(g)) return { type: 'FeatureCollection', features: g as FeatureCollection['features'] };
  return g as FeatureCollection;
}

/**
 * Lit un TIFF/GeoTIFF et le rend côté client en image (dataURL) avec son emprise.
 * Aucun calcul : simple décodage + rééchantillonnage d'affichage. La lecture se
 * fait par bandes pour ne pas charger tout le raster en mémoire (gros TIFF palettés
 * compris : la palette ColorMap est appliquée quand samples === 1).
 */
export async function loadTiffImage(path: string, maxDim = 4096): Promise<TiffImage> {
  const buf = await (await fetch(path)).arrayBuffer();
  const tiff = await fromArrayBuffer(buf);
  const image = await tiff.getImage();
  const width = image.getWidth();
  const height = image.getHeight();
  const samples = image.getSamplesPerPixel();

  const scale = Math.min(1, maxDim / Math.max(width, height));
  const outW = Math.max(1, Math.round(width * scale));
  const outH = Math.max(1, Math.round(height * scale));

  const canvas = document.createElement('canvas');
  canvas.width = outW;
  canvas.height = outH;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Canvas indisponible');
  const imgData = ctx.createImageData(outW, outH);
  const data = imgData.data;

  const fd = image.getFileDirectory();
  const colorMap = fd.ColorMap as number[] | undefined; // TIFF paletté : 256*3 entrées RGB
  const BAND = 512; // lignes pleine résolution lues par appel

  for (let y0 = 0; y0 < height; y0 += BAND) {
    const h = Math.min(BAND, height - y0);
    const rasters = (await image.readRasters({ window: [0, y0, width, h], interleave: true })) as Uint8Array;
    // lignes de sortie concernées par cette bande
    const oy0 = Math.floor(y0 * scale);
    const oy1 = Math.min(outH, Math.ceil((y0 + h) * scale));
    for (let oy = oy0; oy < oy1; oy++) {
      const srcY = Math.min(height - 1, Math.floor(oy / scale)) - y0;
      for (let ox = 0; ox < outW; ox++) {
        const srcX = Math.min(width - 1, Math.floor(ox / scale));
        const src = (srcY * width + srcX) * samples;
        const dst = (oy * outW + ox) * 4;
        if (samples >= 3) {
          data[dst] = rasters[src];
          data[dst + 1] = rasters[src + 1];
          data[dst + 2] = rasters[src + 2];
          data[dst + 3] = samples >= 4 ? rasters[src + 3] : 255;
        } else if (colorMap && colorMap.length >= 768) {
          const idx = rasters[src] * 3;
          data[dst] = colorMap[idx] / 257; // ColorMap : 16 bits par composante
          data[dst + 1] = colorMap[idx + 1] / 257;
          data[dst + 2] = colorMap[idx + 2] / 257;
          data[dst + 3] = 255;
        } else {
          const g = rasters[src];
          data[dst] = g;
          data[dst + 1] = g;
          data[dst + 2] = g;
          data[dst + 3] = 255;
        }
      }
    }
  }
  ctx.putImageData(imgData, 0, 0);

  // Emprise depuis les tags GeoTIFF (nord en haut supposé).
  // geotiff.js peut renvoyer les tags multi-valeurs comme objets indexés {"0":…} : on normalise.
  const scaleTags = numArray(fd.ModelPixelScale);
  const tiepoint = numArray(fd.ModelTiepoint);
  let coordinates: [number, number][];
  let crsNote: string;
  const geoKeys = numArray(fd.GeoKeyDirectory);
  const epsg = geoKeys.length ? geoKeyEpsg(geoKeys) : undefined;
  if (scaleTags && tiepoint && tiepoint.length >= 6) {
    const sx = scaleTags[0];
    const sy = scaleTags[1];
    const west = tiepoint[3];
    const north = tiepoint[4];
    const east = west + width * sx;
    const south = north - height * sy;
    coordinates = [
      [west, north],
      [east, north],
      [east, south],
      [west, south],
    ];
    crsNote = epsg ? `EPSG:${epsg} (lu dans les tags GeoTIFF)` : 'CRS non déclaré dans les tags ; emprise déduite des tags GeoTIFF';
  } else {
    throw new Error('Tags GeoTIFF d’emprise absents : géoréférencement non lisible.');
  }

  return { url: canvas.toDataURL('image/png'), coordinates, width, height, crsNote };
}

/** geotiff.js renvoie parfois les tags multi-valeurs en objet indexé : on remet en tableau. */
function numArray(v: unknown): number[] {
  if (Array.isArray(v)) return v as number[];
  if (v && typeof v === 'object') return Object.values(v as Record<string, number>);
  return [];
}

function geoKeyEpsg(dir: number[]): number | undefined {
  // GeoKeyDirectory : [version, rev, minor, numKeys, ...groupes de 4 (id, tiffTagLocation, count, value)]
  for (let i = 4; i + 3 < dir.length; i += 4) {
    if (dir[i] === 3072) return dir[i + 3]; // ProjectedCSTypeGeoKey
  }
  for (let i = 4; i + 3 < dir.length; i += 4) {
    if (dir[i] === 2048) return dir[i + 3]; // GeographicTypeGeoKey (ex. 4326)
  }
  return undefined;
}

/** 1 entité sur N — affichage uniquement, les données chargées restent intactes. */
export function applySample(fc: FeatureCollection, n: number): FeatureCollection {
  if (!n || n <= 1) return fc;
  return { type: 'FeatureCollection', features: fc.features.filter((_, i) => i % n === 0) };
}

export function countFeatures(fc: FeatureCollection): number {
  return fc.features.length;
}
