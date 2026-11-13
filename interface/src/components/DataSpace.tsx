import { useRef, useState } from 'react';
import type { FeatureCollection } from 'geojson';
import type { DataSource, Project, SourceRole, SourceType } from '../types';
import { ALLOWED_ROLES, NON_DISPONIBLE, ROLE_COLORS, ROLE_LABELS, TYPE_LABELS } from '../constants';
import { countFeatures, fetchGeoJSON, loadShapefile, loadTiffImage } from '../data/loaders';
import { formatValue } from '../lib/format';

interface Props {
  project: Project;
  onUpdate: (fn: (p: Project) => Project) => void;
  /** Onglet « Carte » : n'affiche que les contrôles de couches. */
  layersOnly?: boolean;
}

export default function DataSpace({ project, onUpdate, layersOnly = false }: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<string | null>(null);

  function patchSource(id: string, patch: Partial<DataSource>) {
    onUpdate((p) => ({ ...p, sources: p.sources.map((s) => (s.id === id ? { ...s, ...patch } : s)) }));
  }

  async function loadSource(s: DataSource) {
    setBusy(s.id);
    patchSource(s.id, { loading: true, error: undefined });
    try {
      if (s.type === 'tiff') {
        if (!s.path && !s.url) throw new Error('Aucun fichier TIFF associé.');
        const image = await loadTiffImage(s.path!);
        onUpdate((p) => ({
          ...p,
          sources: p.sources.map((x) => (x.id === s.id ? { ...x, image, loaded: true, loading: false, error: undefined } : x)),
        }));
      } else if (s.type === 'shapefile' || s.type === 'osm') {
        let fc: FeatureCollection;
        try {
          fc = await loadShapefile(s.path!, s.dbfPath, s.zipPath);
        } catch (e) {
          // repli : géométries seules si le .dbf est trop gros ou illisible
          const { loadShapefileGeometryOnly } = await import('../data/loaders');
          fc = await loadShapefileGeometryOnly(s.path!);
          onUpdate((p) => ({
            ...p,
            sources: p.sources.map((x) => (x.id === s.id ? { ...x, geojson: fc, loaded: true, loading: false, crsNote: `${x.crsNote ?? ''} (géométries seules : .dbf non chargé, attributs non renseignés)`.trim() } : x)),
          }));
          return;
        }
        onUpdate((p) => ({
          ...p,
          sources: p.sources.map((x) => (x.id === s.id ? { ...x, geojson: fc, loaded: true, loading: false, featureCount: countFeatures(fc) } : x)),
        }));
      } else {
        const fc = await fetchGeoJSON(s.path!);
        onUpdate((p) => ({
          ...p,
          sources: p.sources.map((x) => (x.id === s.id ? { ...x, geojson: fc, loaded: true, loading: false, featureCount: countFeatures(fc) } : x)),
        }));
      }
    } catch (e) {
      onUpdate((p) => ({
        ...p,
        sources: p.sources.map((x) => (x.id === s.id ? { ...x, loading: false, loaded: false, error: String(e) } : x)),
      }));
    } finally {
      setBusy(null);
    }
  }

  function unloadSource(s: DataSource) {
    patchSource(s.id, { loaded: false, loading: false, geojson: undefined, image: undefined, error: undefined });
  }

  async function onImportFiles(files: FileList | null) {
    if (!files?.length) return;
    for (const f of Array.from(files)) {
      const lower = f.name.toLowerCase();
      let type: SourceType | null = null;
      if (lower.endsWith('.tif') || lower.endsWith('.tiff')) type = 'tiff';
      else if (lower.endsWith('.geojson') || lower.endsWith('.json')) type = 'geojson';
      else if (lower.endsWith('.zip')) type = 'shapefile';
      else if (lower.endsWith('.shp')) type = 'shapefile';
      if (!type) continue;
      const src: DataSource = {
        id: `imp-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        name: f.name,
        type,
        role: type === 'tiff' ? 'fond_tiff' : 'reseau_importe',
        origin: 'import',
        visible: true,
        loaded: false,
        loading: false,
        sample: 1,
        color: ROLE_COLORS[type === 'tiff' ? 'fond_tiff' : 'reseau_importe'],
        zipPath: lower.endsWith('.zip') ? URL.createObjectURL(f) : undefined,
        path: lower.endsWith('.zip') ? undefined : URL.createObjectURL(f),
        url: URL.createObjectURL(f),
        crsNote: 'Fichier importé (session navigateur uniquement).',
      };
      onUpdate((p) => ({ ...p, sources: [...p.sources, src] }));
    }
    if (fileRef.current) fileRef.current.value = '';
  }

  function removeSource(id: string) {
    onUpdate((p) => ({ ...p, sources: p.sources.filter((s) => s.id !== id) }));
  }

  const sources = project.sources;

  return (
    <div className="panel">
      <h2>{layersOnly ? 'Carte — couches' : 'Données'}</h2>
      <p className="muted small">
        Projet actif : <strong>{project.name}</strong>. Un TIFF importé reste un <em>fond cartographique</em> ; un TIFF ne
        peut jamais être déclaré « réseau importé » (rôles autorisés : {ALLOWED_ROLES.tiff.map((r) => ROLE_LABELS[r]).join(' / ')}).
      </p>

      {!layersOnly && (
        <div className="card">
          <h3>Importer un fichier (session navigateur, rien n’est écrit dans le dépôt)</h3>
          <div className="row">
            <input ref={fileRef} type="file" multiple accept=".tif,.tiff,.geojson,.json,.zip,.shp" onChange={(e) => onImportFiles(e.target.files)} />
          </div>
          <p className="muted small">TIFF → fond cartographique · GeoJSON → réseau/résultat · zip ou .shp → réseau importé (géométries seules si le .dbf manque).</p>
          <h3>Traitements non disponibles dans l’interface</h3>
          <div className="row">
            {['Extraction TIFF → réseau candidat', 'Vectorisation', 'entraînement', 'Calcul d’itinéraires', 'Contrôles automatiques', 'OSM en ligne', 'Géocodage / OCR de la consigne'].map((t) => (
              <button key={t} className="btn unavailable" disabled title="L’interface ne lance aucun calcul.">
                {t} — {NON_DISPONIBLE}
              </button>
            ))}
          </div>
        </div>
      )}

      {sources.length === 0 && <p className="muted">Aucune source dans ce projet.</p>}

      <table className="data">
        <thead>
          <tr>
            <th>Nom</th><th>Type</th><th>Rôle</th><th>État</th><th>Visible</th><th>Échantillon</th><th>Couleur</th><th></th>
          </tr>
        </thead>
        <tbody>
          {sources.map((s) => (
            <tr key={s.id}>
              <td>
                <div>{s.name}</div>
                <div className="muted small">{s.origin === 'depot' ? s.path : 'import (session)'}</div>
                {s.crsNote && <div className="muted small">{s.crsNote}</div>}
                {s.featureCount !== undefined && <div className="muted small">{s.featureCount.toLocaleString('fr-FR')} entités chargées</div>}
                {s.error && <div className="badge err">{s.error}</div>}
              </td>
              <td>{TYPE_LABELS[s.type]}</td>
              <td>
                <select
                  value={s.role}
                  onChange={(e) => patchSource(s.id, { role: e.target.value as SourceRole })}
                  disabled={!ALLOWED_ROLES[s.type].includes(s.role) ? false : false}
                >
                  {ALLOWED_ROLES[s.type].map((r) => (
                    <option key={r} value={r}>{ROLE_LABELS[r]}</option>
                  ))}
                  {!ALLOWED_ROLES[s.type].includes(s.role) && <option value={s.role}>{ROLE_LABELS[s.role]}</option>}
                </select>
              </td>
              <td>
                {s.loading && <span className="badge">chargement…</span>}
                {s.loaded && <span className="badge ok">chargé</span>}
                {!s.loaded && !s.loading && <span className="badge">non chargé</span>}
              </td>
              <td><input type="checkbox" checked={s.visible} onChange={(e) => patchSource(s.id, { visible: e.target.checked })} /></td>
              <td>
                <input
                  type="number" min={1} max={1000} value={s.sample}
                  style={{ width: 64 }}
                  onChange={(e) => patchSource(s.id, { sample: Math.max(1, parseInt(e.target.value || '1', 10) || 1) })}
                />
                <div className="muted small">1/N affiché</div>
              </td>
              <td><input type="color" value={s.color} onChange={(e) => patchSource(s.id, { color: e.target.value })} style={{ width: 34, height: 26, padding: 0, border: 'none' }} /></td>
              <td>
                {busy === s.id ? (
                  <button className="btn" disabled>…</button>
                ) : s.loaded ? (
                  <button className="btn" onClick={() => unloadSource(s)}>Décharger</button>
                ) : (
                  <button className="btn primary" onClick={() => loadSource(s)}>Charger</button>
                )}{' '}
                <button className="btn danger" onClick={() => removeSource(s.id)}>Retirer</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        L’échantillon 1/N ne s’applique qu’à l’affichage ; les données chargées restent intactes. Toute valeur absente
        s’affiche « {formatValue(null)} ».
      </p>
    </div>
  );
}
