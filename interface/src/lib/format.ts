import { NON_RENSEIGNE } from '../constants';

/** Toute valeur absente (null, undefined, chaîne vide) s'affiche « Non renseigné ». */
export function formatValue(v: unknown): string {
  if (v === null || v === undefined || v === '') return NON_RENSEIGNE;
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return v.toLocaleString('fr-FR');
    return v.toLocaleString('fr-FR', { maximumFractionDigits: 4 });
  }
  if (typeof v === 'boolean') return v ? 'Oui' : 'Non';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export function formatKm(v: unknown): string {
  if (typeof v !== 'number' || Number.isNaN(v)) return NON_RENSEIGNE;
  return `${v.toLocaleString('fr-FR', { minimumFractionDigits: 3, maximumFractionDigits: 3 })} km`;
}

export function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return NON_RENSEIGNE;
  return d.toLocaleDateString('fr-FR');
}

export function download(filename: string, content: string, mime = 'application/json') {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
